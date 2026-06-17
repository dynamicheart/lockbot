"""
Parallel SSH command executor.

Reads node IPs from config, pings, checks SSH, and executes commands in parallel.
Based on the original cmd.py from yjb_xpu_smi toolkit.
"""

import multiprocessing
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from shlex import quote


def ping(ip):
    try:
        result = subprocess.run(
            ["ping", "-c", "1", ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except Exception:
        return False


def ssh_check(ip, timeout=2):
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={timeout}",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                ip,
                "exit",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 1,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False


def check_remote_host(ip):
    if not ping(ip):
        return ip, False, "unreachable"
    if not ssh_check(ip):
        return ip, False, "ssh_failed"
    return ip, True, "ok"


def _execute_one(args):
    ip, cmd, timeout = args
    try:
        env = os.environ.copy()
        env["LANG"] = "C"
        env["LC_ALL"] = "C"
        output = subprocess.check_output(
            cmd,
            shell=True,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            encoding="utf-8",
            env=env,
        )
        return ip, "ok", output.strip()
    except subprocess.TimeoutExpired:
        return ip, "timeout", ""
    except subprocess.CalledProcessError as e:
        return ip, "error", e.output.strip() if e.output else ""


def run_on_nodes(nodes: dict, command: list[str], *, max_workers=16, timeout=15):
    """Run a command on all reachable nodes via SSH.

    Args:
        nodes: {node_key: {"ip": "x.x.x.x", ...}}
        command: command args to execute remotely
        max_workers: parallelism for SSH check
        timeout: per-node command timeout in seconds

    Returns:
        list of (node_key, ip, status, output)
        status: "ok" | "unreachable" | "ssh_failed" | "timeout" | "error"
    """
    ips = [(nk, info["ip"]) for nk, info in nodes.items()]
    cmd_str = " ".join(quote(c) for c in command)

    # Phase 1: check reachability
    reachable = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(check_remote_host, ip): (nk, ip) for nk, ip in ips}
        for future in futures:
            nk, ip = futures[future]
            _, ok, reason = future.result()
            if ok:
                reachable[nk] = ip
            else:
                yield nk, ip, reason, ""

    if not reachable:
        return

    # Phase 2: execute commands in parallel
    args_list = [(ip, f"ssh {quote(ip)} {cmd_str}", timeout) for ip in reachable.values()]
    nk_list = list(reachable.keys())

    num_procs = min(len(args_list), max_workers)
    with multiprocessing.Pool(num_procs) as pool:
        results = pool.map(_execute_one, args_list)

    for nk, (ip, status, output) in zip(nk_list, results, strict=False):
        yield nk, ip, status, output
