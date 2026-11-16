"""Delegated provisioning uses the issuer's current Console permissions."""

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute

from backend.test_bots import _sample_bot
from backend.test_configuration_delegation import ISSUE, NODE, access, bearer, credentials

PROVISION = "/api/bot-provisioning"
CREATE = "bot:create"


def test_provisioning_preview_and_confirmed_apply_are_separate(client, admin_header, db_session):
    owner = db_session.query(User).filter_by(username="adminuser").one()
    issued = credentials(client, admin_header, [], [CREATE]).raise_for_status().json()
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
    assert result["console_path"] == f"/#/bots/{result['bot_id']}"
    assert "configure_infoflow_callback" in result["remaining_setup"]
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


def test_provisioning_credential_cannot_be_mixed_and_respects_feishu_and_revocation(client, auth_header, admin_header):
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
    denied = credentials(client, auth_header, [], [CREATE])
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "bot_provisioning_admin_required"
    grant = credentials(client, admin_header, [], [CREATE]).raise_for_status().json()
    key = bearer(grant["credential"])
    assert client.post(url, headers=key, json={**proposed, "platform": "Feishu"}).status_code == 403
    receipt = client.post(url, headers=key, json=proposed).raise_for_status().json()["receipt"]
    assert client.delete(f"{ISSUE}/{grant['id']}", headers=admin_header).status_code == 204
    result = client.post(
        PROVISION + "/apply", headers=key, json={"bot": proposed, "receipt": receipt, "confirmed": True}
    )
    assert result.status_code == 401


def test_provisioning_role_is_rechecked_when_applying(client, admin_header, db_session):
    grant = credentials(client, admin_header, [], [CREATE]).raise_for_status().json()
    key = bearer(grant["credential"])
    proposed = _sample_bot("delegated")
    preview = client.post(PROVISION + "/preview", headers=key, json=proposed).raise_for_status()
    db_session.query(User).filter_by(username="adminuser").one().role = "user"
    db_session.commit()
    response = client.post(
        PROVISION + "/apply",
        headers=key,
        json={"bot": proposed, "receipt": preview.json()["receipt"], "confirmed": True},
    )
    assert response.status_code == 403
    assert db_session.query(Bot).count() == 0
    rotate = client.post(f"{ISSUE}/{grant['id']}/rotate", headers=admin_header, json={})
    assert rotate.status_code == 403


def test_feishu_provisioning_requires_super_admin_and_claims_exactly_one_group(client, admin_header, db_session):
    owner = db_session.query(User).filter_by(username="adminuser").one()
    application = FeishuApplication(
        app_id="cli_provisioning_test",
        name="Provisioning test",
        tenant_key="test_tenant",
        bot_open_id="ou_test_bot",
        app_secret="",
        verification_token="",
    )
    db_session.add(application)
    db_session.commit()
    issued = credentials(client, admin_header, [], [CREATE]).raise_for_status().json()
    key = bearer(issued["credential"])
    proposed = {
        "name": "delegated-feishu",
        "bot_type": "QUEUE",
        "platform": "Feishu",
        "feishu_application_id": application.id,
        "group_id": "oc_provisioning_test",
        "webhook_url": "",
        "cluster_configs": ["node1"],
    }
    preview_url = PROVISION + "/preview"
    apply_url = PROVISION + "/apply"
    assert client.post(preview_url, headers=key, json=proposed).status_code == 403

    owner.role = "super_admin"
    db_session.commit()
    preview = client.post(preview_url, headers=key, json=proposed).raise_for_status().json()
    assert preview["platform"] == "feishu"
    assert preview["group_id"] == proposed["group_id"]
    competing = {**proposed, "name": "competing-feishu"}
    competing_preview = client.post(preview_url, headers=key, json=competing).raise_for_status().json()
    assert db_session.query(Bot).count() == db_session.query(FeishuGroupRoute).count() == 0

    body = {"bot": proposed, "receipt": preview["receipt"], "confirmed": True}
    created = client.post(apply_url, headers=key, json=body)
    replay = client.post(apply_url, headers=key, json=body)
    assert created.status_code == replay.status_code == 201
    assert created.json() == replay.json()
    conflict = client.post(
        apply_url, headers=key, json={"bot": competing, "receipt": competing_preview["receipt"], "confirmed": True}
    )
    assert conflict.status_code == 409
    assert client.post(preview_url, headers=key, json=competing).status_code == 409

    result = created.json()
    assert result["console_path"] == f"/#/bots/{result['bot_id']}"
    assert "configure_infoflow_callback" not in result["remaining_setup"]
    bot = db_session.query(Bot).one()
    route = db_session.query(FeishuGroupRoute).one()
    assert (bot.id, bot.platform, bot.user_id) == (result["bot_id"], "Feishu", owner.id)
    assert (route.application_id, route.chat_id, route.bot_id) == (application.id, proposed["group_id"], bot.id)
    audit = db_session.query(AuditLog).filter_by(action="bot.create").one()
    assert (audit.operator_id, audit.target_id) == (owner.id, bot.id)
    assert json.loads(audit.detail) == {"platform": "Feishu", "bot_type": "QUEUE", "credential_id": issued["id"]}


def test_cli_is_separately_downloadable_and_provisioning_is_documented_as_configuration_api(client):
    script = client.get(PROVISION + "/lockbot_provision.py").raise_for_status()
    assert script.headers["Content-Disposition"] == 'attachment; filename="lockbot_provision.py"'
    compile(script.content, "lockbot_provision.py", "exec")
    schema = client.get("/api/public-openapi.json").raise_for_status().json()
    for path in ("/preview", "/apply"):
        assert schema["paths"][PROVISION + path]["post"]["security"] == [{"ConfigurationKey": []}]


def test_downloaded_cli_previews_requires_confirmation_and_applies(client, admin_header, tmp_path):
    script = tmp_path / "lockbot_provision.py"
    script.write_bytes(client.get(PROVISION + "/lockbot_provision.py").raise_for_status().content)
    proposed = tmp_path / "bot.json"
    proposed.write_text(json.dumps(_sample_bot("cli-created")))
    issued = credentials(client, admin_header, [], [CREATE]).raise_for_status().json()
    requests = []

    class Bridge(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            result = client.post(
                self.path,
                content=self.rfile.read(int(self.headers["Content-Length"])),
                headers={"Authorization": self.headers["Authorization"], "Content-Type": "application/json"},
            )
            self.send_response(result.status_code)
            self.end_headers()
            self.wfile.write(result.content)

    with ThreadingHTTPServer(("127.0.0.1", 0), Bridge) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_port}"

        def run(command, *args, check=True):
            return subprocess.run(
                [sys.executable, str(script), "--service", url, command, "--config", str(proposed), *args],
                env={**os.environ, "NO_PROXY": "127.0.0.1", "LOCKBOT_PROVISIONING_KEY": issued["credential"]},
                capture_output=True,
                text=True,
                check=check,
                timeout=10,
            )

        try:
            receipt_file = tmp_path / "provisioning-review.json"
            receipt_args = ("--receipt-file", str(receipt_file))
            preview = json.loads(run("preview", *receipt_args).stdout)
            assert preview["name"] == "cli-created"
            assert "receipt" not in preview
            assert receipt_file.stat().st_mode & 0o077 == 0
            receipt_setup = receipt_file.read_bytes()
            assert run("preview", *receipt_args, check=False).returncode == 1
            assert receipt_file.read_bytes() == receipt_setup
            requests.clear()
            reviewed = json.loads(run("review", *receipt_args).stdout)
            assert reviewed == {**preview, "expired": False}
            assert requests == []
            mismatch = tmp_path / "changed.json"
            mismatch.write_text(json.dumps(_sample_bot("different")))
            assert run("apply", *receipt_args, "--config", str(mismatch), "--confirm", check=False).returncode == 1
            assert requests == []
            assert run("apply", *receipt_args, check=False).returncode == 2
            assert client.get("/api/bots", headers=admin_header).raise_for_status().json() == []
            result = json.loads(run("apply", *receipt_args, "--confirm").stdout)
            assert result["console_url"] == f"{url}/#/bots/{result['bot_id']}"
            assert json.loads(run("apply", *receipt_args, "--confirm").stdout) == result
        finally:
            server.shutdown()
            worker.join(timeout=5)
