"""Bounded real-HTTP surrogate for indefinitely polling custom resource clients.

Exercises one actual 60-second quota window, independent same-IP credentials,
contending acquisitions and queue/group policy. No CLI, model, real IM or real
resource is involved. The unchanged single-process backend is the authority.
"""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path


def probe(output):
    import httpx
    from cryptography.fernet import Fernet

    started = time.monotonic()
    evidence = []
    progress = output.with_suffix(".progress.json")

    def stage(name, **values):
        temporary = progress.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"stage": name, "elapsed_seconds": round(time.monotonic() - started, 3), **values})
        )
        temporary.replace(progress)

    with tempfile.TemporaryDirectory(prefix="lockbot-polling-contract-") as temporary:
        root = Path(temporary)
        private = root / "credentials.json"
        credentials = {
            "password": secrets.token_urlsafe(24),
            "jwt_secret": secrets.token_urlsafe(48),
            "encryption_key": Fernet.generate_key().decode(),
        }
        private.write_text(json.dumps(credentials))
        private.chmod(0o600)
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            port = bound.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        source = Path(__file__).resolve().parents[2]
        environment = dict(
            os.environ,
            PYTHONPATH=str(source / "python"),
            DATA_DIR=str(root / "data"),
            DEV_MODE="false",
            ALLOW_REGISTER="false",
            PLATFORM_URL=origin,
        )
        for name in (
            "DATABASE_URL",
            "LOCKBOT_SKIP_STATE_OWNERSHIP",
            "RESOURCE_API_READ_BUDGET_PER_MINUTE",
            "RESOURCE_API_WRITE_BUDGET_PER_MINUTE",
        ):
            environment.pop(name, None)
        with (root / "server.log").open("w") as log:
            child = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("http_contract_probe.py")),
                    "--serve",
                    str(port),
                    str(private),
                ],
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        try:
            with httpx.Client(base_url=origin, timeout=15, trust_env=False) as client:
                for _ in range(100):
                    if child.poll() is not None:
                        raise RuntimeError("Isolated service exited")
                    try:
                        if client.get("/api/health").status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Isolated service readiness timed out")

                def request(method, path, *, who="setup", expected=200, **kwargs):
                    before = time.monotonic()
                    response = client.request(method, path, **kwargs)
                    event = {
                        "actor": who,
                        "method": method,
                        "path": path,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "duration_seconds": round(time.monotonic() - before, 4),
                        "status": response.status_code,
                    }
                    if "Retry-After" in response.headers:
                        event["retry_after"] = response.headers["Retry-After"]
                    evidence.append(event)
                    assert response.status_code == expected, (
                        f"{method} {path}: {response.status_code}, expected {expected}"
                    )
                    return response

                login = request(
                    "POST", "/api/auth/login", json={"username": "probe-admin", "password": credentials["password"]}
                ).json()
                admin = {"Authorization": "Bearer " + login["access_token"]}

                def bot(kind, nodes, rules=None):
                    response = request(
                        "POST",
                        "/api/bots",
                        expected=201,
                        headers=admin,
                        json={
                            "name": "polling-contract-" + kind.lower(),
                            "bot_type": kind,
                            "platform": "fakeim",
                            "webhook_url": "https://example.invalid/unused",
                            "cluster_configs": nodes,
                            "config_overrides": rules or {},
                        },
                    ).json()
                    assert response["status"] == "running"
                    request("PUT", f"/api/bots/{response['id']}/resource-auth", headers=admin, json={"enabled": True})
                    return response["id"]

                def enroll(name, bot_id):
                    binding = request("POST", "/api/resource-auth/bindings", expected=201, json={}).json()
                    request(
                        "POST",
                        f"/api/bots/webhook/{bot_id}?signature=fake-valid-signature",
                        json={
                            "envelope": {
                                "actor": name,
                                "conversation": "test-only",
                                "text": "bind " + binding["bind_code"],
                            }
                        },
                    )
                    assert (
                        request("GET", "/api/resource-auth/bindings/" + binding["binding_id"]).json()["status"]
                        == "confirmed"
                    )
                    return {"Authorization": "Bearer " + binding["credential"]}

                node_id = bot("NODE", ["n1", "n2"])
                queue_id = bot("QUEUE", ["q1", "q2"], {"FORBID_RELOCK": True, "MAX_RESOURCES_PER_USER": 1})
                headers = {
                    name: enroll(name, node_id) for name in ("quota-a", "quota-b", "holder", "racer-a", "racer-b")
                }
                queue_headers = {
                    name: enroll(name, queue_id) for name in ("queue-holder", "queue-first", "queue-second")
                }

                def read(name, bot_id=node_id, *, queue=False):
                    return request(
                        "GET",
                        f"/api/bots/{bot_id}/resources",
                        who=name,
                        headers=(queue_headers if queue else headers)[name],
                    ).json()

                def command(name, text, bot_id=node_id, *, queue=False):
                    return request(
                        "POST",
                        f"/api/bots/{bot_id}/commands",
                        who=name,
                        headers=(queue_headers if queue else headers)[name],
                        json={"command": text},
                    ).json()

                def resource(snapshot, key):
                    return next(item for item in snapshot["resources"] if item["id"] == key)

                def owners(snapshot, key):
                    return [row["external_user_id"] for row in resource(snapshot, key)["holders"]]

                limited = threading.Event()
                window_result = {}

                def poll_across_real_window():
                    first_poll = time.monotonic()
                    for _ in range(120):
                        snapshot = read("quota-a")
                        assert snapshot["context"]["bot_rules"]["request_budget"] == {
                            "read_per_minute": 120,
                            "write_per_minute": 30,
                        }
                    response = request(
                        "GET", f"/api/bots/{node_id}/resources", who="quota-a", expected=429, headers=headers["quota-a"]
                    )
                    delay = int(response.headers["Retry-After"])
                    assert 1 <= delay <= 61
                    cooldown = time.monotonic()
                    window_result.update(success_before_limit=120, retry_after=delay)
                    stage("honoring_retry_after", retry_after_seconds=delay)
                    limited.set()
                    # This is intentional client pacing, not a shortened/mocked clock.
                    time.sleep(delay)
                    recovered = read("quota-a")
                    assert recovered["health"] == "running"
                    window_result.update(
                        recovered=True,
                        waited_seconds=round(time.monotonic() - cooldown, 3),
                        window_elapsed_seconds=round(time.monotonic() - first_poll, 3),
                    )
                    assert window_result["waited_seconds"] >= delay
                    assert window_result["window_elapsed_seconds"] >= 60
                    # The client continues after recovering, rather than stopping its loop at a local cap.
                    read("quota-a")
                    window_result["continued_after_recovery"] = True
                    return window_result

                with ThreadPoolExecutor(max_workers=4) as pool:
                    quota = pool.submit(poll_across_real_window)
                    assert limited.wait(20), "Quota actor did not reach the genuine limiter"
                    for _ in range(15):
                        read("quota-b")
                    same_ip_independent = True
                    command("holder", "lock n1 10m")
                    target_seen_idle = threading.Barrier(2)
                    loop_started = threading.Barrier(3)
                    release = threading.Event()

                    def wait_and_acquire(name):
                        polls = 0
                        loop_started.wait(timeout=10)
                        deadline = time.monotonic() + 15
                        while time.monotonic() < deadline:
                            snapshot = read(name)
                            polls += 1
                            if release.is_set() and not owners(snapshot, "n1"):
                                target_seen_idle.wait(timeout=10)
                                reply = command(name, "lock n1 10m")
                                return {
                                    "polls": polls,
                                    "owned_in_response": name in owners(reply["resource_snapshot"], "n1"),
                                }
                            time.sleep(0.05)
                        raise AssertionError("Resource never became available to the bounded poller")

                    racers = {name: pool.submit(wait_and_acquire, name) for name in ("racer-a", "racer-b")}
                    loop_started.wait(timeout=10)
                    time.sleep(0.2)
                    command("holder", "unlock n1")
                    release.set()
                    races = {name: future.result(timeout=20) for name, future in racers.items()}
                    snapshot = read("quota-b")
                    winners = owners(snapshot, "n1")
                    assert len(winners) == 1 and winners[0] in racers
                    assert sum(row["owned_in_response"] for row in races.values()) == 1
                    assert all(row["polls"] >= 2 for row in races.values())
                    command(winners[0], "unlock n1")

                    # Identical custom-client writes are not idempotent: the same
                    # actor's second lock is a legitimate renewal, never a second holder.
                    duplicate_barrier = threading.Barrier(2)

                    def same_actor_lock():
                        duplicate_barrier.wait(timeout=10)
                        return command("racer-a", "lock n1 10m")

                    repeated = [pool.submit(same_actor_lock) for _ in range(2)]
                    for future in repeated:
                        future.result(timeout=15)
                    snapshot = read("quota-b")
                    holder = resource(snapshot, "n1")["holders"]
                    assert len(holder) == 1 and holder[0]["external_user_id"] == "racer-a"
                    assert 1190 <= holder[0]["remaining_seconds"] <= 1200
                    old_expiry = datetime.fromisoformat(holder[0]["expires_at"])
                    renewed = command("racer-a", "lock n1 10m")["resource_snapshot"]
                    assert (
                        datetime.fromisoformat(resource(renewed, "n1")["holders"][0]["expires_at"]) - old_expiry
                    ).total_seconds() == 600
                    command("racer-a", "unlock n1")

                    # Queue order and configured group policy apply to custom clients,
                    # including clients that ignore the Skill and call POST directly.
                    command("queue-holder", "lock q1 10m", queue_id, queue=True)
                    command("queue-first", "book q1 2m", queue_id, queue=True)
                    command("queue-second", "book q1 2m", queue_id, queue=True)
                    repeated_booking = command("queue-first", "book q1 2m", queue_id, queue=True)["resource_snapshot"]
                    booked = resource(repeated_booking, "q1")["booking_list"]
                    assert [row["external_user_id"] for row in booked] == ["queue-first", "queue-second"]
                    command("queue-holder", "unlock q1", queue_id, queue=True)
                    for _ in range(3):
                        denied = command("queue-second", "lock q1", queue_id, queue=True)["resource_snapshot"]
                        assert owners(denied, "q1") == []
                    fair_barrier = threading.Barrier(2)

                    def claim(name):
                        fair_barrier.wait(timeout=10)
                        return command(name, "lock q1", queue_id, queue=True)

                    claims = [pool.submit(claim, name) for name in ("queue-first", "queue-second")]
                    for future in claims:
                        future.result(timeout=15)
                    queued = read("queue-first", queue_id, queue=True)
                    assert owners(queued, "q1") == ["queue-first"]
                    expiry = resource(queued, "q1")["holders"][0]["expires_at"]
                    refused_renewal = command("queue-first", "lock q1 2m", queue_id, queue=True)["resource_snapshot"]
                    assert resource(refused_renewal, "q1")["holders"][0]["expires_at"] == expiry
                    cap = command("queue-first", "lock q2 2m", queue_id, queue=True)["resource_snapshot"]
                    assert owners(cap, "q2") == []
                    request(
                        "POST",
                        f"/api/bots/{queue_id}/commands",
                        who="queue-second",
                        expected=403,
                        headers=queue_headers["queue-second"],
                        json={"command": "take q1"},
                    )
                    assert owners(read("queue-second", queue_id, queue=True), "q1") == ["queue-first"]
                    stage("waiting_for_rate_window", other_checks_passed=True)
                    window = quota.result(timeout=70)

                proof = {
                    "synthetic_only": True,
                    "transport": "real loopback TCP",
                    "custom_clients_use_cli": False,
                    "clock_mocked": False,
                    "rate_limits_changed": False,
                    "bounded_surrogate": (
                        "Two successful polling segments across one real 60-second window; "
                        "finite resource-release and acquisition barriers."
                    ),
                    "same_ip_independent_actors": same_ip_independent,
                    "quota": window,
                    "release_race": races,
                    "single_holder_after_race": True,
                    "identical_same_actor_lock_is_renewal": True,
                    "explicit_renewal_adds_seconds": 600,
                    "queue_order_preserved_under_repeated_and_concurrent_claims": True,
                    "duplicate_book_keeps_one_queue_position": True,
                    "group_relock_prohibition_enforced": True,
                    "group_resource_cap_enforced": True,
                    "preemption_api_denied": True,
                    "requests": evidence,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
                output.write_text(json.dumps(proof, indent=2) + "\n")
                output.chmod(0o600)
                stage("completed", requests=len(evidence))
                print(
                    json.dumps(
                        {
                            key: value
                            for key, value in proof.items()
                            if key not in {"requests", "bounded_surrogate", "release_race"}
                        }
                    )
                )
        finally:
            child.terminate()
            try:
                child.wait(10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    probe(arguments.output)
