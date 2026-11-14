"""Host timezone changes presentation, never the lease instant or remaining time."""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from lockbot.backend.app.bots import metrics_access
from lockbot.backend.app.bots.models import Bot

from backend.test_metrics_access import signing_key, source
from backend.test_resource_api_resources import api  # noqa: F401

# Importing the shared API fixture is intentional.
# ruff: noqa: F811


@pytest.mark.parametrize("host_timezone", ["UTC", "Asia/Shanghai", "America/Los_Angeles"])
def test_core_and_snapshot_instants_do_not_follow_host_timezone(host_timezone):
    script = """
import json,time
from datetime import datetime,timezone
from lockbot.core.utils import remaining_duration
from lockbot.backend.app.resource_api.snapshot import build_resources,compute_next_event
captured=datetime(2030,1,1,15,59,30,tzinfo=timezone.utc)
start=int(captured.timestamp())-30
time.time=lambda: captured.timestamp()
state={'n1':{'status':'exclusive','current_users':[{'user_id':'alice','start_time':start,'duration':75}],'booking_list':[]}}
rows=build_resources(captured_at=captured,state=state,bot_type='QUEUE',platform='infoflow',caller_id='alice')
next_at,kind=compute_next_event(captured_at=captured,resources=rows,caller_id='alice',early_notify=False,time_alert_seconds=0)
print(json.dumps({'captured_at':captured.isoformat(),'holder':rows[0]['holders'][0],'next_event_at':next_at,'kind':kind,'remaining_core':remaining_duration(start,75)}))
"""
    env = {**os.environ, "TZ": host_timezone}
    output = subprocess.check_output([sys.executable, "-c", script], env=env, text=True)
    result = json.loads(output)
    assert result["captured_at"] == "2030-01-01T15:59:30+00:00"
    assert result["holder"]["expires_at"] == result["next_event_at"] == "2030-01-01T16:00:15+00:00"
    assert result["holder"]["remaining_seconds"] == result["remaining_core"] == 45
    assert result["kind"] == "own_lock_expiry"
    for utc_text in [result["next_event_at"], result["next_event_at"].replace("+00:00", "Z")]:
        instant = datetime.fromisoformat(utc_text.replace("Z", "+00:00"))
        local = instant.astimezone(timezone(timedelta(hours=8)))
        assert local.isoformat() == "2030-01-02T00:00:15+08:00"
        assert local.timestamp() == instant.timestamp()


@pytest.mark.parametrize("offset", [None, timezone.utc, timezone(timedelta(hours=8))])
def test_metrics_parent_expiry_caps_the_same_instant_with_any_utc_offset(
    api, db_session, tmp_path, monkeypatch, offset
):
    signing_key(tmp_path, monkeypatch)
    bot = db_session.get(Bot, api["bot_id"])
    now = datetime(2030, 1, 1, 15, 59, 30, tzinfo=timezone.utc)
    expected = now + timedelta(seconds=45)
    value = expected.replace(tzinfo=None) if offset is None else expected.astimezone(offset)
    monkeypatch.setattr(metrics_access, "time", SimpleNamespace(time=lambda: now.timestamp()))
    grant = metrics_access.issue_metrics_access(db_session, bot, source(), expires_at=value)
    assert grant["expires_at"] == int(expected.timestamp())
    decoded = jwt.decode(
        grant["token"],
        b"x" * 64,
        algorithms=["HS256"],
        audience=source()["endpoint"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert decoded["exp"] == int(expected.timestamp())
    assert decoded["exp"] - now.timestamp() == 45
