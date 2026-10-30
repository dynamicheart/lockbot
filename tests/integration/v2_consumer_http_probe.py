"""Real HTTP V2 consumer acceptance on private fictional state, never a deployed service."""

import argparse
import importlib.util
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def serve(settings_file, descriptor):
    """Test-only local identity seam; actual allocator/history endpoints stay unchanged."""
    import ipaddress

    root = Path(settings_file).parent
    settings = json.loads(Path(settings_file).read_text())
    import lockbot.backend.app.config as config

    config.JWT_SECRET, config.ENCRYPTION_KEY = settings["jwt_secret"], settings["encryption_key"]
    connect = socket.socket.connect

    def local_connect(sock, address):
        if isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback:
            return connect(sock, address)
        (root / "external-attempt").write_text("blocked")
        raise RuntimeError("External connections disabled for this acceptance")

    socket.socket.connect = local_connect
    import requests

    def no_outbound(*_args, **_kwargs):
        (root / "external-attempt").write_text("blocked")
        raise RuntimeError("Outbound HTTP disabled for this acceptance")

    requests.sessions.Session.request = no_outbound
    from lockbot.core.platforms.infoflow import InfoflowAdapter

    InfoflowAdapter.send = lambda *_args, **_kwargs: [(200, "stubbed")]
    InfoflowAdapter.send_notification = lambda *_args, **_kwargs: ("accepted", "stubbed")
    InfoflowAdapter.send_broadcast = lambda *_args, **_kwargs: ("accepted", "stubbed")
    from lockbot.backend.app.feishu.connectors import connectors

    connectors.start = lambda *_args, **_kwargs: None
    connectors.shutdown = lambda *_args, **_kwargs: None
    from lockbot.backend.app.main import create_app
    from lockbot.backend.app.resource_auth import service
    from pydantic import BaseModel

    app = create_app()

    class FixtureBinding(BaseModel):
        bot_id: int
        actor: str
        code: str

    @app.post("/_fixture/bind")
    def bind(body: FixtureBinding):
        service.handle_chat_binding(
            platform="infoflow", external_user_id=body.actor, bot_id=body.bot_id, action="bind", argument=body.code
        )
        return {"bound": True}

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", workers=1, access_log=False, log_level="warning"))
    server.run(sockets=[socket.socket(fileno=descriptor)])


def probe(output):
    import requests
    from cryptography.fernet import Fernet

    spec = importlib.util.spec_from_file_location("v2_http_consumer", REPO / "scripts/lease_history/report_v2.py")
    consumer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(consumer)
    proof = {
        "real_loopback_http": True,
        "production_requests": 0,
        "platform_delivery_stubbed": True,
        "sdk_receivers_started": 0,
    }
    children = []
    with tempfile.TemporaryDirectory(prefix="lockbot-v2-http-") as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        private = root / "settings.json"
        private.write_text(
            json.dumps({"jwt_secret": secrets.token_urlsafe(48), "encryption_key": Fernet.generate_key().decode()})
        )
        private.chmod(0o600)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(REPO / "python"),
            "DATA_DIR": str(root / "data"),
            "ALLOW_REGISTER": "true",
            "DEV_MODE": "false",
            "RESOURCE_API_WRITE_BUDGET_PER_MINUTE": "120",
        }
        client = requests.Session()
        client.trust_env = False
        port, process = None, None
        log = (root / "server.log").open("wb")

        def start():
            nonlocal port, process
            with socket.socket() as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", port or 0))
                port = listener.getsockname()[1]
                listener.listen(128)
                process = subprocess.Popen(
                    [sys.executable, __file__, "--serve", str(private), str(listener.fileno())],
                    cwd=root,
                    env=env,
                    pass_fds=(listener.fileno(),),
                    stdout=log,
                    stderr=log,
                )
            children.append(process)
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Isolated service exited before readiness")
                try:
                    if client.get(f"http://127.0.0.1:{port}/api/health", timeout=0.2).status_code == 200:
                        return
                except requests.RequestException:
                    pass
                time.sleep(0.1)
            raise RuntimeError("Isolated service readiness timed out")

        def stop():
            nonlocal process
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                process = None

        def call(method, path, *, headers=None, body=None, expected=200):
            response = client.request(method, f"http://127.0.0.1:{port}" + path, headers=headers, json=body, timeout=10)
            if response.status_code != expected:
                raise RuntimeError(f"Isolated fixture HTTP step failed with status {response.status_code}")
            return response.json() if response.content else None

        try:
            start()
            password = secrets.token_urlsafe(24)
            call(
                "POST",
                "/api/auth/register",
                body={"username": "fixture-owner", "email": "fixture@example.invalid", "password": password},
                expected=201,
            )
            jwt = call("POST", "/api/auth/login", body={"username": "fixture-owner", "password": password})[
                "access_token"
            ]
            owner = {"Authorization": "Bearer " + jwt}
            bot = call(
                "POST",
                "/api/bots",
                headers=owner,
                body={
                    "name": "fixture-queue",
                    "bot_type": "QUEUE",
                    "webhook_url": "https://example.invalid/stubbed",
                    "group_id": "1234",
                    "cluster_configs": {"node-a": "gpu", "node-b": "gpu"},
                    "resource_api_notify_enabled": False,
                },
                expected=201,
            )
            bot_id, original_uuid = bot["id"], bot["uuid"]
            database = sqlite3.connect(root / "data" / "lockbot.db")
            try:
                assert database.execute("SELECT COUNT(*) FROM feishu_applications").fetchone()[0] == 0
            finally:
                database.close()
            proof["no_feishu_application_configured"] = True
            key = call("POST", f"/api/bots/{bot_id}/api-key", headers=owner)["api_key"]
            key_file = root / "op.key"
            key_file.write_text(key)
            key_file.chmod(0o600)

            def actor(name):
                binding = call("POST", "/api/resource-auth/bindings", body={"label": "fixture"}, expected=201)
                call("POST", "/_fixture/bind", body={"bot_id": bot_id, "actor": name, "code": binding["bind_code"]})
                return {"Authorization": "Bearer " + binding["credential"]}

            alice, bob = actor("fixture-alice"), actor("fixture-bob")
            command_count = 0

            def command(text, headers=alice):
                nonlocal command_count
                call(
                    "POST",
                    f"/api/bots/{bot_id}/commands",
                    headers=headers,
                    body={"command": text, "purpose": "fixture-private-note", "agent_label": "fixture-private-label"},
                )
                command_count += 1

            since = datetime.now(timezone.utc).timestamp()
            command("lock node-a 2m")
            command("lock node-a 3m")
            command("book node-a 1m", bob)
            command("free node-a")
            until = datetime.now(timezone.utc).timestamp()
            selected = consumer.scope(f"http://127.0.0.1:{port}", bot_id, since, until, page_size=1)
            pages = []

            def frozen_fetch(scope, token, cursor):
                page = consumer.fetch_page(scope, token, cursor)
                pages.append(page)
                if len(pages) == 1:
                    # Change allocator state after capture: the cursor must retain its original checkpoint/as_of.
                    command("lock node-b 0.02m")
                return page

            exported = consumer.collect(selected, consumer.private_key(key_file), max_pages=20, fetch=frozen_fetch)
            report = consumer.analyze(exported)
            assert report["coverage_complete"] and len(pages) >= 2
            assert all(
                page["as_of"] == pages[0]["as_of"] and page["checkpoint"] == pages[0]["checkpoint"] for page in pages
            )
            event_kinds = {event["kind"] for page in pages for frame in page["items"] for event in frame["events"]}
            assert {"acquired", "renewed", "booked", "released"} <= event_kinds
            assert not any(
                event["resource"] == "node-b" for page in pages for frame in page["items"] for event in frame["events"]
            )
            assert all(
                value not in json.dumps(exported)
                for value in ("fixture-private-note", "fixture-private-label", "holding_id")
            )
            proof.update(
                frozen_pages=len(pages),
                frozen_as_of_checkpoint_preserved=True,
                late_append_excluded=True,
                grant_renew_book_release_present=True,
                initial_report_verified=True,
                public_projection=True,
            )
            # The real scheduler expires the short logical lease; no test clock seam is needed.
            current = consumer.scope(
                selected["origin"], bot_id, since, datetime.now(timezone.utc).timestamp(), page_size=100
            )
            full_export = consumer.collect(current, consumer.private_key(key_file), max_pages=5)
            full_report = consumer.analyze(full_export)
            assert full_report["coverage_complete"]
            assert all(row["external_user_id"] == "fixture-alice" for row in full_report["subject_totals"])
            assert (
                full_export["pages"][0]["checkpoint"]["contract"]["commit_sequence"]
                > pages[0]["checkpoint"]["contract"]["commit_sequence"]
            )

            def cli_report(export, filename, expected):
                path = root / filename
                path.write_text(json.dumps(consumer.public_export(export)))
                path.chmod(0o600)
                selected_scope = export["scope"]

                def instant(value):
                    return datetime.fromtimestamp(value, timezone.utc).isoformat()

                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        str(REPO / "scripts/lease_history/report_v2.py"),
                        "--input",
                        str(path),
                        "--bot-id",
                        str(bot_id),
                        "--since",
                        instant(selected_scope["start"]),
                        "--until",
                        instant(selected_scope["end"]),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env={"PATH": os.environ.get("PATH", "")},
                )
                assert result.returncode == expected, "Isolated consumer CLI returned an unexpected status"
                return json.loads(result.stdout)

            assert (
                cli_report(full_export, "verified.json", 0)["subject_grant_seconds"]
                == full_report["subject_grant_seconds"]
            )
            frames = [frame for page in full_export["pages"] for frame in page["items"]]
            assert any(
                event["kind"] == "expired" and event["resource"] == "node-b"
                for frame in frames
                for event in frame["events"]
            )

            def event_frame(kind, resource):
                return next(
                    frame
                    for frame in frames
                    if any(event["kind"] == kind and event["resource"] == resource for event in frame["events"])
                )

            acquired = event_frame("acquired", "node-a")
            released = event_frame("released", "node-a")
            short = event_frame("acquired", "node-b")
            short_lease = next(lease for lease in short["state"]["allocations"] if lease["resource"] == "node-b")
            expected_seconds = released["timestamp"] - acquired["timestamp"]
            expected_seconds += max(0, short_lease["deadline"] - short["timestamp"])
            actual_seconds = full_report["subject_grant_seconds"]["node_exclusive_subject_grant_seconds"]
            assert expected_seconds > 0 and abs(actual_seconds - expected_seconds) < 0.000001
            proof.update(
                real_scheduler_expiry=True,
                independently_checked_commit_interval=True,
                waiting_actor_has_no_grant_time=True,
                real_cli_verified_report=True,
                known_window_report_verified=True,
                verified_subject_grant_seconds=full_report["subject_grant_seconds"],
            )
            stop()
            start()
            restored = call("GET", f"/api/bots/{bot_id}", headers=owner)
            restarted = consumer.scope(
                selected["origin"], bot_id, since, datetime.now(timezone.utc).timestamp(), page_size=100
            )
            unknown = consumer.collect(restarted, consumer.private_key(key_file), max_pages=5)
            unknown_report = consumer.analyze(unknown)
            assert restored["uuid"] == original_uuid
            assert not unknown_report["coverage_complete"] and unknown_report["subject_grant_seconds"] is None
            assert cli_report(unknown, "unknown.json", 2)["subject_grant_seconds"] is None
            proof.update(
                restart_identity_preserved=True,
                restart_report_unknown=True,
                real_cli_restart_unknown=True,
                synthetic_resource_commands=command_count,
                external_connection_attempts=int((root / "external-attempt").exists()),
            )
            assert not (root / "external-attempt").exists()
        finally:
            stop()
            client.close()
            log.close()
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
            proof["surviving_child_processes"] = sum(child.poll() is None for child in children)
            assert proof["surviving_child_processes"] == 0
    proof["private_fixture_removed"] = not root.exists()
    assert proof["private_fixture_removed"]
    Path(output).write_text(json.dumps(proof, indent=2))
    return proof


def guarded_probe(output):
    """Fence the consumer process as well as the service; never inherit an outbound proxy."""
    names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy")
    previous = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    os.environ["NO_PROXY"] = "*"
    connect = socket.socket.connect
    blocked = []

    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] == "127.0.0.1":
            return connect(sock, address)
        blocked.append(True)
        raise RuntimeError("Acceptance consumer may connect only to loopback")

    socket.socket.connect = local_connect
    try:
        proof = probe(output)
        assert not blocked
        proof["consumer_external_connections"] = len(blocked)
        Path(output).write_text(json.dumps(proof, indent=2))
        return proof
    finally:
        socket.socket.connect = connect
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", nargs=2, metavar=("PRIVATE_SETTINGS", "SOCKET_FD"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.serve:
        serve(args.serve[0], int(args.serve[1]))
    else:
        if args.output is None:
            parser.error("Provide a sanitized output file")
        guarded_probe(args.output)
