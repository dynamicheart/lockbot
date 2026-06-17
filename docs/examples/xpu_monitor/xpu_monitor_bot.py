"""
XPU Monitor Bot

Queries XPU/GPU utilization via SSH, reports status as markdown tables,
and optionally sends @lockbot opkick to release idle resources.

Kick strategy (pessimistic / reactive):
  - Low-frequency check of LockBot lock state (cheap HTTP, no SSH)
  - Only trigger XPU utilization check when resource saturation is high
    (e.g., only 1 or 0 nodes idle — like a full charging station)
  - When idle users found on a saturated cluster:
    1. Notify the user first ("your XPU is idle, N min grace period")
    2. After grace period, re-check. Still idle → opkick
  - If cluster is NOT saturated, do nothing (resources are available anyway)

Modes:
  - Query-only: just report XPU status (enable_opkick=false)
  - Full: pessimistic check + notify + grace + opkick

Requirements: requests, flask (for webhook-triggered mode)

Usage:
  # One-shot report
  python xpu_monitor_bot.py --report

  # Continuous monitoring (pessimistic kick when saturated)
  python xpu_monitor_bot.py --loop

  # Flask webhook server (triggered by IM AT message)
  python xpu_monitor_bot.py --serve --port 8777
"""

import argparse
import contextlib
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG = {}


def load_config(path="config.json"):
    global CONFIG
    if not os.path.exists(path):
        path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(path) as f:
        CONFIG = json.load(f)
    return CONFIG


# ── XPU Query ─────────────────────────────────────────────────────────────────


def _ssh_xpu_query(ip, timeout=15):
    """SSH to a node and run xpu-smi, return raw output line."""
    # We inline the query command rather than calling the shell script
    cmd = (
        "ip=$(hostname -I | awk '{print $1}');"
        "output=$(xpu-smi 2>/dev/null);"
        "if echo $output | grep -q 'No running processes found'; then status=FREE; else status=BUSY; fi;"
        "output2=$(xpu-smi -m 2>/dev/null);"
        "mems=($(echo \"$output2\" | awk '{print $18}'));"
        "utils=($(echo \"$output2\" | awk '{print $20}'));"
        "mem_sum=0; util_sum=0; count=${#mems[@]};"
        "for ((i=0; i<count; i++)); do mem_sum=$((mem_sum + mems[i])); util_sum=$((util_sum + utils[i])); done;"
        'avg_mem=$(echo "scale=2; $mem_sum / $count" | bc);'
        'avg_util=$(echo "scale=2; $util_sum / $count" | bc);'
        'echo "${ip}: ${status} | Mem: ${avg_mem} MiB | Util: ${avg_util} %"'
    )
    try:
        env = os.environ.copy()
        env["LANG"] = "C"
        env["LC_ALL"] = "C"
        result = subprocess.check_output(
            f"ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null {ip} bash -c '{cmd}'",
            shell=True,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            encoding="utf-8",
            env=env,
        )
        return ip, "ok", result.strip()
    except subprocess.TimeoutExpired:
        return ip, "timeout", ""
    except subprocess.CalledProcessError as e:
        return ip, "error", e.output.strip() if e.output else ""
    except Exception:
        return ip, "error", ""


def query_all_nodes():
    """Query XPU status from all configured nodes. Returns list of parsed results."""
    nodes = CONFIG.get("nodes", {})
    timeout = CONFIG.get("ssh_cmd_timeout", 15)
    max_workers = CONFIG.get("max_workers", 16)

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_ssh_xpu_query, info["ip"], timeout): (nk, info) for nk, info in nodes.items()}
        for future in futures:
            nk, info = futures[future]
            ip, status, output = future.result()
            row = {
                "node_key": nk,
                "ip": ip,
                "hostname": info.get("hostname", nk),
                "status": "UNKNOWN",
                "mem": "-",
                "util": "-",
                "util_pct": None,
            }
            if status == "ok" and output:
                m = re.match(
                    r"[\d.]+:\s+(\w+)\s+\|\s+Mem:\s+([^|]+)\|\s+Util:\s+([^%]+)%",
                    output,
                )
                if m:
                    row["status"] = m.group(1)
                    row["mem"] = m.group(2).strip()
                    row["util"] = m.group(3).strip() + "%"
                    with contextlib.suppress(ValueError):
                        row["util_pct"] = float(m.group(3).strip())
            elif status == "timeout":
                row["status"] = "TIMEOUT"
            else:
                row["status"] = "ERROR"
            results.append(row)

    return results


# ── LockBot API ───────────────────────────────────────────────────────────────


def query_locked_users():
    """Query current lock state from LockBot API."""
    url = CONFIG.get("lockbot_api_url")
    key = CONFIG.get("lockbot_api_key")
    if not url or not key:
        return None
    resp = requests.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ── Opkick ────────────────────────────────────────────────────────────────────


def send_opkick(target_user, node_key):
    """Send opkick via LockBot REST API."""
    api_url = CONFIG.get("lockbot_api_url", "")
    api_key = CONFIG.get("lockbot_api_key", "")
    if not api_url or not api_key:
        print(f"[warn] Cannot opkick {target_user} on {node_key}: no API config")
        return False

    # Derive opkick URL from locked-users URL
    base_url = api_url.replace("/locked-users", "")
    url = f"{base_url}/opkick"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"target_user": target_user, "node_key": node_key}

    try:
        resp = requests.post(url, json=body, headers=headers, timeout=10)
        resp.raise_for_status()
        print(f"[opkick] Kicked {target_user} from {node_key}: {resp.json()}")
        return True
    except Exception as e:
        print(f"[opkick] Failed: {e}")
        return False


# ── Report (Markdown table) ───────────────────────────────────────────────────


def format_report(results, max_rows=None):
    """Format query results as markdown table(s)."""
    max_rows = max_rows or CONFIG.get("report_max_rows_per_msg", 12)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = "| Node | Status | Avg Mem | Avg Util |\n|------|--------|---------|----------|"

    chunks = []
    for i in range(0, len(results), max_rows):
        batch = results[i : i + max_rows]
        rows = []
        for r in batch:
            color = "green" if r["status"] == "FREE" else "red"
            rows.append(f'| {r["hostname"]} | <font color="{color}">{r["status"]}</font> | {r["mem"]} | {r["util"]} |')
        title = f"##### XPU Status Report ({now_str})"
        if len(results) > max_rows:
            part = (i // max_rows) + 1
            total = (len(results) + max_rows - 1) // max_rows
            title += f" - Part {part}/{total}"
        chunks.append(title + "\n" + header + "\n" + "\n".join(rows))

    return chunks


def send_report(results, toid=None):
    """Send markdown report via webhook."""
    webhook_url = CONFIG.get("report_webhook_url") or CONFIG.get("infoflow_webhook_url")
    if not webhook_url:
        # Print to stdout if no webhook
        for chunk in format_report(results):
            print(chunk)
        return

    for chunk in format_report(results):
        payload = {"message": {"header": {}, "body": [{"type": "MD", "content": chunk}]}}
        if toid:
            payload["message"]["header"]["toid"] = toid
        try:
            requests.post(webhook_url, json=payload, timeout=10)
        except Exception as e:
            print(f"[error] Failed to send report: {e}")


# ── Notify user ───────────────────────────────────────────────────────────────


def send_idle_warning(target_user, node_key, grace_minutes):
    """Notify a user that their resource is idle and will be released."""
    webhook_url = CONFIG.get("report_webhook_url") or CONFIG.get("infoflow_webhook_url")
    group_id = CONFIG.get("infoflow_group_id")
    if not webhook_url or not group_id:
        print(f"[warn] {target_user} on {node_key} is idle (no webhook to notify)")
        return

    msg = (
        f"⚠️ @{target_user} 你在 {node_key} 上的 XPU 资源空闲，"
        f"当前集群资源紧张。如 {grace_minutes} 分钟内仍无使用将自动释放。"
    )
    payload = {
        "message": {
            "header": {"toid": group_id},
            "body": [
                {"type": "AT", "atuserids": [target_user]},
                {"type": "TEXT", "content": msg},
            ],
        }
    }
    try:
        requests.post(webhook_url, json=payload, timeout=10)
        print(f"[notify] Warned {target_user} on {node_key}, grace={grace_minutes}min")
    except Exception as e:
        print(f"[error] Failed to notify {target_user}: {e}")


# ── Monitor Loop (pessimistic/reactive strategy) ──────────────────────────────

# States for each (user, node):
#   None → not tracked
#   "notified" → warning sent, waiting for grace period
#   "kicked" → opkick sent (debounce)


def monitor_loop():
    """Pessimistic monitoring loop.

    Only checks XPU utilization when cluster is saturated (few/no idle nodes).
    When saturated + idle user found: notify first, then kick after grace.
    """
    enable_opkick = CONFIG.get("enable_opkick", False)
    check_interval = CONFIG.get("check_interval_seconds", 300)  # default 5min (infrequent)
    grace_minutes = CONFIG.get("grace_minutes", 5)
    saturation_threshold = CONFIG.get("saturation_threshold", 1)  # kick if idle_nodes <= this

    # Track state per (user_id, node_key)
    warned_at: dict[tuple[str, str], float] = {}  # when warning was sent

    while True:
        if not enable_opkick:
            # Query-only mode: report and sleep
            results = query_all_nodes()
            send_report(results)
            time.sleep(check_interval)
            continue

        # Step 1: Cheap check — query LockBot lock state (HTTP only, no SSH)
        try:
            lock_data = query_locked_users()
        except Exception as e:
            print(f"[error] Failed to query locks: {e}")
            time.sleep(check_interval)
            continue

        if not lock_data:
            time.sleep(check_interval)
            continue

        nodes = lock_data.get("nodes", {})
        bot_type = lock_data.get("bot_type", "NODE")

        # Count idle vs occupied nodes
        idle_count = 0
        occupied_nodes = {}  # node_key -> [user_ids]
        for node_key, node_data in nodes.items():
            if node_key not in CONFIG.get("nodes", {}):
                continue
            if bot_type == "DEVICE":
                users = []
                for dev in node_data:
                    for u in dev.get("current_users", []):
                        users.append(u["user_id"])
                if not users:
                    idle_count += 1
                else:
                    occupied_nodes[node_key] = users
            else:
                cu = node_data.get("current_users", [])
                if not cu:
                    idle_count += 1
                else:
                    occupied_nodes[node_key] = [u["user_id"] for u in cu]

        # Step 2: If cluster is NOT saturated, no action needed
        if idle_count > saturation_threshold:
            # Clean up old warnings — pressure relieved
            warned_at.clear()
            time.sleep(check_interval)
            continue

        # Step 3: Cluster is saturated — now SSH to check XPU utilization
        #          (only query occupied nodes, not all)
        print(f"[check] Cluster saturated (idle={idle_count}), checking utilization...")
        nodes_to_check = {nk: CONFIG["nodes"][nk] for nk in occupied_nodes if nk in CONFIG.get("nodes", {})}

        xpu_results = []
        with ThreadPoolExecutor(max_workers=CONFIG.get("max_workers", 16)) as executor:
            timeout = CONFIG.get("ssh_cmd_timeout", 15)
            futures = {executor.submit(_ssh_xpu_query, info["ip"], timeout): nk for nk, info in nodes_to_check.items()}
            for future in futures:
                nk = futures[future]
                ip, status, output = future.result()
                util_pct = None
                if status == "ok" and output:
                    m = re.match(
                        r"[\d.]+:\s+(\w+)\s+\|\s+Mem:\s+([^|]+)\|\s+Util:\s+([^%]+)%",
                        output,
                    )
                    if m:
                        with contextlib.suppress(ValueError):
                            util_pct = float(m.group(3).strip())
                xpu_results.append((nk, util_pct))

        # Step 4: Process results — notify or kick
        now = time.time()
        for node_key, util_pct in xpu_results:
            if util_pct is None:
                continue
            if util_pct >= 5.0:
                # Active — clear any warnings for users on this node
                for uid in occupied_nodes.get(node_key, []):
                    warned_at.pop((uid, node_key), None)
                continue

            # Idle (< 5% util) on a saturated cluster
            for user_id in occupied_nodes.get(node_key, []):
                key = (user_id, node_key)

                if key not in warned_at:
                    # First detection: notify, don't kick yet
                    send_idle_warning(user_id, node_key, grace_minutes)
                    warned_at[key] = now
                elif now - warned_at[key] > grace_minutes * 60:
                    # Grace period expired, still idle → kick
                    print(f"[kick] {user_id} on {node_key} still idle after grace period")
                    send_opkick(user_id, node_key)
                    del warned_at[key]
                # else: within grace period, wait

        time.sleep(check_interval)


# ── Flask webhook server ──────────────────────────────────────────────────────


def serve(port=8777):
    """Run as Flask server, triggered by IM AT messages to report status."""
    from flask import Flask
    from flask import request as flask_request

    app = Flask(__name__)

    @app.route("/", methods=["POST"])
    def handle():
        # Support Infoflow callback verification
        echostr = flask_request.form.get("echostr")
        if echostr:
            return echostr

        # Parse incoming message to get group_id for reply
        try:
            data = flask_request.get_json(force=True)
        except Exception:
            data = {}

        toid = None
        if data and "message" in data:
            toid = data["message"].get("header", {}).get("toid")

        results = query_all_nodes()
        send_report(results, toid=toid)
        return "ok"

    app.run(host="0.0.0.0", port=port)


# ── CLI ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="XPU Monitor Bot")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--report", action="store_true", help="One-shot report")
    parser.add_argument("--loop", action="store_true", help="Continuous monitoring loop")
    parser.add_argument("--serve", action="store_true", help="Flask webhook server")
    parser.add_argument("--port", type=int, default=8777, help="Server port")
    args = parser.parse_args()

    load_config(args.config)

    if args.report:
        results = query_all_nodes()
        send_report(results)
    elif args.loop:
        monitor_loop()
    elif args.serve:
        serve(args.port)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
