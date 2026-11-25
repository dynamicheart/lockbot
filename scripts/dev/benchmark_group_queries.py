"""Synthetic Infoflow and Feishu callbacks for the resource API benchmark."""

import asyncio
import base64
import hashlib
import json
import time
from contextlib import contextmanager
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
import requests

INFOFLOW_TOKEN = "fixture-infoflow-token"
INFOFLOW_KEY = b"k" * 32
INFOFLOW_GROUP = 25101
INFOFLOW_WEBHOOK = "https://infoflow.example.invalid/reply"
FEISHU_OPEN_ID = "ou_fixturebot"
FEISHU_GROUP = "oc_fixturegroup"
FEISHU_TOKEN = "fixture-feishu-token"
FEISHU_APP = "cli_fixture"


@contextmanager
def group_queries(work):
    from lockbot.backend.app.bots import encryption
    from lockbot.backend.app.bots.manager import bot_manager
    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.database import SessionLocal
    from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute
    from lockbot.backend.app.feishu.service import bot_application_config
    from lockbot.backend.app.platform_applications.service import runtime_guard
    from lockbot.core.platforms.feishu import FeishuClient
    from sqlalchemy import select

    received = {"infoflow": Queue(), "feishu": Queue()}

    def infoflow_send(_session, request, **_):
        assert request.url == INFOFLOW_WEBHOOK and request.method == "POST"
        payload = json.loads(request.body)["message"]
        observed = "".join(item["content"] for item in payload["body"] if item["type"] == "TEXT")
        assert payload["header"]["toid"] == [INFOFLOW_GROUP]
        assert "fixture-00-0" in observed
        received["infoflow"].put(time.time())
        return SimpleNamespace(status_code=200, text='{"errcode":0}')

    def feishu_send(_actor, payload):
        assert payload["receive_id"] == FEISHU_GROUP
        assert "fixture-feishu-node" in payload["content"]
        received["feishu"].put(time.time())
        return [(200, "fixture-accepted")]

    with SessionLocal() as db:
        infoflow = db.scalar(select(Bot).where(Bot.name == "fixture-1"))
        assert infoflow.bot_type == "QUEUE_DEVICE"
        infoflow_id = infoflow.id
        bot_manager.stop_bot(infoflow_id)
        infoflow.group_id = str(INFOFLOW_GROUP)
        infoflow.webhook_url = encryption.encrypt(INFOFLOW_WEBHOOK)
        infoflow.aes_key = encryption.encrypt(base64.b64encode(INFOFLOW_KEY).decode())
        infoflow.token = encryption.encrypt(INFOFLOW_TOKEN)
        feishu = Bot(
            user_id=infoflow.user_id,
            uuid=str(uuid4()),
            name="fixture-feishu",
            bot_type="QUEUE",
            platform="Feishu",
            group_id=FEISHU_GROUP,
            cluster_configs='{"fixture-feishu-node": "fixture"}',
            webhook_url="",
            status="running",
        )
        application = FeishuApplication(
            app_id=FEISHU_APP,
            name="fixture-feishu-app",
            tenant_key="tenant",
            bot_open_id=FEISHU_OPEN_ID,
            app_secret=encryption.encrypt("fixture-feishu-app-secret"),
            verification_token=encryption.encrypt(FEISHU_TOKEN),
            transport="http",
        )
        db.add_all((feishu, application))
        db.flush()
        db.add(FeishuGroupRoute(application_id=application.id, bot_id=feishu.id, chat_id=FEISHU_GROUP))
        db.commit()
        app_id, feishu_id = application.id, feishu.id

        def common(row):
            return {
                "BOT_ID": row.id,
                "BOT_NAME": row.name,
                "BOT_TYPE": row.bot_type,
                "DATA_DIR": str(work),
                "PLATFORM": row.platform,
                "CLUSTER_CONFIGS": json.loads(row.cluster_configs),
                "_AUTHORIZED_GROUP_IDS": [row.group_id],
                "_IDENTITY_REVISION": row.identity_revision,
                "_LIFECYCLE_BOT_UUID": row.uuid,
            }

        infoflow_config = {
            **common(infoflow),
            "WEBHOOK_URL": INFOFLOW_WEBHOOK,
            "TOKEN": INFOFLOW_TOKEN,
            "AESKEY": base64.b64encode(INFOFLOW_KEY).decode(),
            "_MESSAGING_CREDENTIAL_GUARD": runtime_guard(infoflow),
        }
        feishu_config = {**common(feishu), **bot_application_config(feishu, db)}

    bot_manager.start_bot(infoflow_id, infoflow_config)
    bot_manager.start_bot(feishu_id, feishu_config)
    with (
        patch.object(requests.Session, "send", infoflow_send),
        patch.object(FeishuClient, "send", feishu_send),
        patch.object(FeishuClient, "warm_user_name", lambda *args: None),
    ):
        yield infoflow_id, app_id, received


async def probe_group(base, chat, *, rounds=3):
    from lockbot.backend.app.database import SessionLocal
    from lockbot.backend.app.feishu.models import FeishuReceivedMessage
    from lockbot.core.msg_utils import AESCipher

    infoflow_id, app_id, received = chat
    observations = {"infoflow": [], "feishu": []}

    async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=5) as client:

        async def infoflow(index):
            started = time.time()
            rn = str(index + 100)
            timestamp = str(int(started))
            sig = hashlib.md5(f"{rn}{timestamp}{INFOFLOW_TOKEN}".encode()).hexdigest()
            msg = {
                "message": {
                    "header": {"fromuserid": "fixture-reader", "toid": INFOFLOW_GROUP},
                    "body": [{"type": "AT", "atuserids": ["fixture-bot"]}, {"type": "TEXT", "content": "query"}],
                }
            }
            response = await client.post(
                f"/api/bots/webhook/{infoflow_id}",
                params={"signature": sig, "rn": rn, "timestamp": timestamp},
                content=AESCipher(INFOFLOW_KEY).encrypt(json.dumps(msg)),
            )
            assert (response.status_code, response.text) == (200, "command succeed"), response.status_code
            observations["infoflow"].append((received["infoflow"].get_nowait() - started) * 1000)

        async def feishu(index):
            started = time.time()
            message_id = f"om_fixture{index}"
            msg = {
                "header": {
                    "app_id": FEISHU_APP,
                    "tenant_key": "tenant",
                    "token": FEISHU_TOKEN,
                    "event_type": "im.message.receive_v1",
                },
                "event": {
                    "sender": {
                        "sender_type": "user",
                        "tenant_key": "tenant",
                        "sender_id": {"open_id": "ou_fixturereader"},
                    },
                    "message": {
                        "message_id": message_id,
                        "create_time": str(int(started * 1000)),
                        "chat_type": "group",
                        "chat_id": FEISHU_GROUP,
                        "message_type": "text",
                        "mentions": [{"key": "@_user_1", "id": {"open_id": FEISHU_OPEN_ID}}],
                        "content": json.dumps({"text": "@_user_1 query"}),
                    },
                },
            }
            response = await client.post(f"/api/feishu/events/{app_id}", json=msg)
            assert response.status_code == 200 and response.json() == {"accepted": True}, response.status_code
            captured = await asyncio.to_thread(received["feishu"].get, True, 5)
            observations["feishu"].append((captured - started) * 1000)

        for index in range(rounds):
            await asyncio.gather(infoflow(index), feishu(index))
            await asyncio.sleep(0.4)

    completed = 0
    for _ in range(10):
        with SessionLocal() as db:
            completed = db.query(FeishuReceivedMessage).filter_by(application_id=app_id, state="completed").count()
        if completed == rounds:
            break
        await asyncio.sleep(0.05)
    assert completed == rounds, completed
    return {key: {"replies": len(times), "latency_ms": sorted(times)} for key, times in observations.items()}
