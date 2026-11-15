"""Delegated provisioning uses the issuer's current Console permissions."""

import json

from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot

from backend.test_bots import _sample_bot
from backend.test_configuration_delegation import ISSUE, NODE, access, bearer, credentials

PROVISION = "/api/bot-provisioning"
CREATE = "bot:create"


def test_provisioning_preview_and_confirmed_apply_are_separate(client, auth_header, db_session):
    owner = db_session.query(User).filter_by(username="testuser").one()
    owner.max_running_bots = 0
    db_session.commit()
    issued = credentials(client, auth_header, [], [CREATE]).raise_for_status().json()
    key = bearer(issued["credential"])
    proposed = _sample_bot("agent-created")
    proposal = client.post(PROVISION + "/preview", headers=key, json=proposed).raise_for_status()
    assert proposal.headers["Cache-Control"] == "no-store"
    assert not any(proposed[field] in proposal.text for field in ("webhook_url", "aes_key", "token"))
    assert db_session.query(Bot).count() == 0
    body = {"bot": proposed, "receipt": proposal.json()["receipt"], "confirmed": True}
    assert client.post(PROVISION + "/apply", headers=key, json={**body, "confirmed": False}).status_code == 422
    other = {**body, "bot": {**proposed, "token": "modified"}}
    assert client.post(PROVISION + "/apply", headers=key, json=other).status_code == 409
    created = client.post(PROVISION + "/apply", headers=key, json=body)
    replay = client.post(PROVISION + "/apply", headers=key, json=body)
    assert created.status_code == replay.status_code == 201
    assert created.json() == replay.json()
    result = created.json()
    assert result["console_path"] == f"/bots/{result['bot_id']}"
    assert result["status"] == "stopped"
    assert result["remaining_setup"] == ["configure_infoflow_callback", "check_bot_start"]
    assert client.get(access(result["bot_id"]), headers=key).status_code == 403
    rows = db_session.query(AuditLog).filter_by(action="bot.create").all()
    assert len(rows) == 1
    assert rows[0].operator_id == owner.id
    assert rows[0].target_id == result["bot_id"]
    assert json.loads(rows[0].detail) == {
        "platform": "Infoflow",
        "bot_type": "NODE",
        "credential_id": issued["id"],
    }


def test_provisioning_credential_cannot_be_mixed_and_respects_feishu_and_revocation(client, auth_header):
    assert credentials(client, auth_header, [], [NODE]).status_code == 422
    assert credentials(client, auth_header, [], [NODE, CREATE]).status_code == 422
    assert credentials(client, bearer("lbr_" + "a" * 48), [], [CREATE]).status_code == 401
    existing = client.post("/api/bots", headers=auth_header, json=_sample_bot("existing")).raise_for_status().json()
    assert credentials(client, auth_header, [existing["id"]], [CREATE]).status_code == 422
    edit_key = credentials(client, auth_header, [existing["id"]], [NODE]).raise_for_status().json()["credential"]
    proposed = _sample_bot("delegated")
    url = PROVISION + "/preview"
    assert client.post(url, headers=bearer(edit_key), json=proposed).status_code == 403
    assert client.post(url, headers=auth_header, json=proposed).status_code == 401
    grant = credentials(client, auth_header, [], [CREATE]).raise_for_status().json()
    key = bearer(grant["credential"])
    assert client.post(url, headers=key, json={**proposed, "platform": "Feishu"}).status_code == 403
    receipt = client.post(url, headers=key, json=proposed).raise_for_status().json()["receipt"]
    assert client.delete(f"{ISSUE}/{grant['id']}", headers=auth_header).status_code == 204
    result = client.post(
        PROVISION + "/apply", headers=key, json={"bot": proposed, "receipt": receipt, "confirmed": True}
    )
    assert result.status_code == 401
