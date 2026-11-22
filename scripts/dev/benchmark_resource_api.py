#!/usr/bin/env python3
"""Benchmark an actual LockBot v2 backend on a synthetic, loopback-only inventory."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from cryptography.fernet import Fernet

API = "/api/agent/v2"
QUERY = {
    "unit": "card",
    "model": "p800",
    "available": "true",
    "limit": 20,
    "fields": "node,device,status,hardware,available",
}
FAIRNESS = ("key-fairness", 20, 2, 20, 2)
FAIRNESS_SOURCE_LIMIT = 80

SCENARIOS = {
    "smoke": [("mixed", 20, 4, 20, 2), FAIRNESS],
    "weekly": [
        ("mixed", 500, 30, 1000, 5),
        ("poll-5s", 500, 5, 6000, 6),
        ("poll-1s", 500, 1, 30000, 6),
        FAIRNESS,
    ],
}


def inventory():
    yield "QUEUE", {"queue-fixture": "queue-fixture"}
    for group in range(25):
        kind = "DEVICE" if group % 2 else "QUEUE_DEVICE"
        model = "p800" if group % 3 == 0 else "p900"
        yield kind, {f"fixture-{group:02d}-{node}": [model] * 8 for node in range(4)}
    for group in range(8):
        yield "NODE", {f"whole-{group:02d}-{node}": f"whole-{group:02d}-{node}" for node in range(18)}


def seed_runtime(directory, count):
    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.bots.manager import bot_manager
    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.database import Base, SessionLocal, engine
    from lockbot.backend.app.resource_api.v2.binding import PersonalCredential, _grant
    from lockbot.backend.app.resource_api.v2.identity import identities
    from lockbot.backend.app.resource_auth.credentials import digest_credential, seal_credential

    assert Path(engine.url.database).resolve().is_relative_to(directory.resolve())
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    configs, bots, keys = {}, [], []
    resources = 0
    target = None
    with SessionLocal() as db:
        owner = User(username="benchmark", email="benchmark@example.invalid", password_hash="unused")
        db.add(owner)
        db.flush()
        for kind, nodes in inventory():
            bot = Bot(
                user_id=owner.id,
                uuid=str(uuid4()),
                name=f"fixture-{len(configs)}",
                bot_type=kind,
                platform="Infoflow",
                cluster_configs=json.dumps(nodes),
                webhook_url="",
                status="running",
                resource_auth_enabled=True,
                resource_api_notify_enabled=False,
            )
            db.add(bot)
            db.flush()
            refs = identities(db, bot)
            if kind == "QUEUE_DEVICE" and target is None:
                write_target = refs["fixture-00-0", 0]
                db.flush()
                target = write_target.ref
            resources += len(refs)
            bots.append(bot)
            configs[bot.id] = {
                "BOT_ID": bot.id,
                "BOT_TYPE": kind,
                "BOT_NAME": bot.name,
                "DATA_DIR": str(directory),
                "CLUSTER_CONFIGS": nodes,
            }
        for index in range(count):
            key = "lbv2_" + secrets.token_urlsafe(32)
            principal_id = uuid4().hex
            identity = PersonalCredential(
                public_id=principal_id,
                namespace="infoflow",
                platform="infoflow",
                external_user_id=f"fixture-{index}",
                credential_digest=digest_credential(key),
                credential_encrypted=seal_credential(principal_id, key),
                generation=0,
            )
            keys.append(key)
            db.add(identity)
            db.flush()
            for bot in bots:
                _grant(db, identity, bot, chat_id=None, now=now)
        db.commit()
    assert target is not None
    for bot_id, config in configs.items():
        bot_manager.start_bot(bot_id, config)
    return keys, target, {"groups": len(bots), "units": resources}


@contextmanager
def service(app):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 8
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.03)
            if not server.started:
                raise RuntimeError("isolated server did not start")
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=8)


def headers(key):
    return {"Authorization": "Bearer " + key}


def simulated_sources(app, keys, count):
    """Assign synthetic client addresses at the ASGI boundary of the fixture."""
    if count == 1:
        return app
    clients = {("Bearer " + key).encode(): f"192.0.2.{index % count + 1}" for index, key in enumerate(keys)}

    async def fixture(scope, receive, send):
        client = scope.get("client")
        if scope["type"] == "http" and client and client[0] == "127.0.0.1":
            address = clients.get(dict(scope["headers"]).get(b"authorization"))
            if address is not None:
                scope = {**scope, "client": (address, client[1])}
        await app(scope, receive, send)

    return fixture


def p95(values):
    return round(sorted(values)[math.ceil(0.95 * len(values)) - 1], 1) if values else None


async def mixed(base, keys, target, scenario):
    name, audience, interval, offered, command_rounds = scenario
    outcomes, write_outcomes, rate_limit_codes = Counter(), Counter(), Counter()
    read_times, write_times, bytes_read = [], [], []
    queue = asyncio.Queue(maxsize=128)
    limits = httpx.Limits(max_connections=68, max_keepalive_connections=68)
    async with (
        httpx.AsyncClient(base_url=base, trust_env=False, timeout=5, limits=limits) as reads,
        httpx.AsyncClient(base_url=base, trust_env=False, timeout=5) as writes,
    ):

        async def reader():
            while (item := await queue.get()) is not None:
                due, key = item
                try:
                    reply = await reads.get(API + "/resources", headers=headers(key), params=QUERY)
                    label = str(reply.status_code)
                    if reply.status_code == 429:
                        detail = reply.json().get("detail")
                        code = detail.get("code") if isinstance(detail, dict) else None
                        rate_limit_codes[code or "unspecified"] += 1
                    if reply.status_code == 200:
                        page = reply.json()
                        correct = page["rows"] and all(
                            row["status"] == "idle"
                            and row["available"] is True
                            and row["hardware"] == [{"model": "p800", "count": 1}]
                            for row in page["rows"]
                        )
                        label = "complete" if correct and page["complete"] else "partial"
                        if label == "complete":
                            read_times.append(1000 * (time.monotonic() - due))
                            bytes_read.append(len(reply.content))
                    outcomes[label] += 1
                except (httpx.RequestError, ValueError, KeyError):
                    outcomes["transport_or_invalid"] += 1
                finally:
                    queue.task_done()

        async def write(key, operation, label=None):
            data = {"operation": operation, "resource_refs": [target]}
            if operation in ("lock", "book"):
                data["duration"] = "5m"
            started = time.monotonic()
            try:
                reply = await writes.post(
                    API + "/commands",
                    json=data,
                    headers={**headers(key), "Idempotency-Key": uuid4().hex},
                )
                write_outcomes[f"{label or operation}:{reply.status_code}"] += 1
                return reply.status_code
            except httpx.RequestError:
                write_outcomes[f"{label or operation}:transport_error"] += 1
                return 0
            finally:
                write_times.append(1000 * (time.monotonic() - started))

        async def compete():
            loop_time = offered * interval / audience
            start = time.monotonic()
            for index in range(command_rounds):
                due = start + (index + 1) * loop_time / (command_rounds + 1)
                await asyncio.sleep(max(0, due - time.monotonic()))
                verdict = await asyncio.gather(*(write(key, "lock") for key in keys[:2]))
                if sorted(verdict) != [200, 409]:
                    return False
                winning = verdict.index(200)
                other = keys[1 - winning]
                if await write(other, "book") != 200 or await write(other, "free", "cancel") != 200:
                    return False
                if await write(keys[winning], "free", "release") != 200:
                    return False
            return True

        pool = [asyncio.create_task(reader()) for _ in range(64)]
        commands = asyncio.create_task(compete())
        started = time.monotonic()
        for index in range(offered):
            due = started + index * interval / audience
            await asyncio.sleep(max(0, due - time.monotonic()))
            try:
                queue.put_nowait((due, keys[index % audience]))
            except asyncio.QueueFull:
                outcomes["client_backlog_dropped"] += 1
        await queue.join()
        for _ in pool:
            queue.put_nowait(None)
        await asyncio.gather(*pool)
        correct_writes = await commands

    return {
        "scenario": name,
        "identities": audience,
        "offered": offered,
        "interval_seconds": interval,
        "outcomes": outcomes,
        "rate_limit_codes": rate_limit_codes,
        "read_p95_ms": p95(read_times),
        "reply_p95_bytes": p95(bytes_read),
        "write_outcomes": write_outcomes,
        "write_p95_ms": p95(write_times),
        "write_invariants": correct_writes,
    }


async def check_fairness(base, keys, target, scenario, chat, probe_group):
    """Run ordinary callers after a burst and while one key continues flooding."""
    flood_outcomes = Counter()
    flood_headers = headers(keys[-1])
    peers = keys[: scenario[1]]
    finished = asyncio.Event()
    async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=5) as client:

        async def burst(count):
            responses = await asyncio.gather(
                *(client.get(API + "/resources", headers=flood_headers, params=QUERY) for _ in range(count))
            )
            flood_outcomes.update(str(response.status_code) for response in responses)

        async def flood():
            while not finished.is_set():
                await burst(16)

        # Exhaust the small fixture source bucket if per-key protection regresses.
        for _ in range(10):
            await burst(16)
        continuing = asyncio.create_task(flood())
        try:
            result, group_reads = await asyncio.gather(
                mixed(base, peers, target, scenario),
                probe_group(base, chat),
            )
        finally:
            finished.set()
            await continuing

    result.update(
        {
            "source_ips": 1,
            "source_per_minute": FAIRNESS_SOURCE_LIMIT,
            "flood_outcomes": dict(flood_outcomes),
            "group_queries": {
                key: {"replies": data["replies"], "p95_ms": p95(data["latency_ms"])}
                for key, data in group_reads.items()
            },
        }
    )
    result["fairness_passed"] = (
        result["outcomes"].get("complete") == result["offered"]
        and bool(flood_outcomes["429"] or flood_outcomes["503"])
        and all(outcome in {"200", "429", "503"} for outcome in flood_outcomes)
        and group_reads.keys() == {"infoflow", "feishu"}
        and all(group["replies"] == 3 for group in group_reads.values())
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-root", type=Path, required=True)
    parser.add_argument("--profile", choices=SCENARIOS, default="smoke")
    parser.add_argument("--scenario", action="append", help="Run only the named scenarios from the selected profile.")
    parser.add_argument("--source-ips", type=int, default=1, help="Number of simulated fixture egress IPs (1–64).")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.source_ips <= 64:
        parser.error("--source-ips must be between 1 and 64")
    scenarios = SCENARIOS[args.profile]
    if args.scenario:
        known = {item[0] for item in scenarios}
        if not set(args.scenario) <= known:
            parser.error("--scenario must name a scenario from the selected profile")
        scenarios = [item for item in scenarios if item[0] in args.scenario]
    root = args.server_root.resolve()
    sys.path.insert(0, str(root / "python"))
    with tempfile.TemporaryDirectory(prefix="lockbot-benchmark-") as work:
        work = Path(work)
        os.environ.update(
            DATA_DIR=str(work),
            DATABASE_URL="sqlite:///" + str(work / "fixture.sqlite3"),
            ENCRYPTION_KEY=Fernet.generate_key().decode(),
            JWT_SECRET=secrets.token_hex(48),
            PLATFORM_URL="http://127.0.0.1",
            DEV_MODE="false",
            RESOURCE_API_AGENT_V2_ONLY="true",
            REDIS_URL="",
            RESOURCE_API_READ_BUDGET_PER_MINUTE="120",
            RESOURCE_API_WRITE_BUDGET_PER_MINUTE="30",
        )
        from lockbot.backend.app.bots.manager import bot_manager
        from lockbot.backend.app.main import create_app
        from lockbot.backend.app.resource_api.traffic import credential_budget
        from lockbot.backend.app.resource_api.v2 import resources

        assert Path(resources.__file__).resolve().is_relative_to(root)
        key_count = max(item[1] for item in scenarios) + int(FAIRNESS in scenarios)
        keys, target, geometry = seed_runtime(work, key_count)
        results = []
        try:
            for scenario in scenarios:
                credential_budget.reset()
                app = create_app()
                if scenario == FAIRNESS:
                    from benchmark_group_queries import group_queries, probe_group

                    app.state.agent_ingress_budget.resource_list_source_limit = FAIRNESS_SOURCE_LIMIT
                    with group_queries(work) as chat, service(app) as base:
                        result = asyncio.run(check_fairness(base, keys, target, scenario, chat, probe_group))
                else:
                    with service(simulated_sources(app, keys, args.source_ips)) as base:
                        result = asyncio.run(mixed(base, keys, target, scenario))
                print(json.dumps(result), flush=True)
                results.append(result)
        finally:
            bot_manager.shutdown_all()
            report = {"fixture": geometry, "profile": args.profile, "source_ips": args.source_ips, "results": results}
            args.output.write_text(json.dumps(report, indent=2) + "\n")

    success = all(
        item["write_invariants"]
        and item.get("fairness_passed", True)
        and (item["scenario"] != "mixed" or item["outcomes"]["complete"] >= math.ceil(item["offered"] * 0.99))
        for item in results
    )
    return 0 if success else 1


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    raise SystemExit(main())
