# ruff: noqa: F811
"""Deployment-owned signer registries never grant an unknown source a global key."""

import json
import os

import jwt
import pytest
from lockbot.backend.app.bots.metrics_access import issue_metrics_access
from lockbot.backend.app.bots.metrics_signers import MAX_CONFIG_BYTES, MAX_SOURCES, select_metrics_signer
from lockbot.backend.app.bots.models import Bot
from lockbot.metrics.gateway import Gateway, Rejected

from backend.test_metrics_access import api, source  # noqa: F401

A = "https://metrics-a.example.invalid"
B = "https://metrics-b.example.invalid/prometheus"


def private_file(path, body):
    path.write_bytes(body)
    path.chmod(0o600)
    return path


@pytest.fixture
def registry(tmp_path, monkeypatch):
    a = private_file(tmp_path / "a.key", b"a" * 64)
    b = private_file(tmp_path / "b.key", b"b" * 64)
    config = {"version": 1, "sources": [{"audience": A, "key_file": str(a)}, {"audience": B, "key_file": str(b)}]}
    path = private_file(tmp_path / "signers.json", json.dumps(config).encode())
    monkeypatch.setenv("METRICS_GRANT_SIGNERS_FILE", str(path))
    legacy = private_file(tmp_path / "legacy.key", b"legacy-must-not-be-used" * 3)
    monkeypatch.setenv("METRICS_GRANT_SECRET_FILE", str(legacy))
    return path, config, a, b


def write_registry(registry):
    path, config, *_keys = registry
    path.write_text(json.dumps(config))


def source_at(endpoint):
    return {**source(), "endpoint": endpoint}


def gateway(endpoint, issuer, bot_uuid, key):
    return Gateway({"audience": endpoint, "issuers": {issuer: {"key_file": str(key), "bots": {bot_uuid: ["host-a"]}}}})


def test_two_sources_validate_own_grants_and_reject_cross_source_tokens(api, db_session, registry):
    _path, _config, key_a, key_b = registry
    bot = db_session.get(Bot, api["bot_id"])
    grant_a = issue_metrics_access(db_session, bot, source_at(A))
    grant_b = issue_metrics_access(db_session, bot, source_at(B))
    claims = jwt.decode(grant_a["token"], b"a" * 64, algorithms=["HS256"], audience=A)
    source_a = gateway(A, claims["iss"], bot.uuid, key_a)
    source_b = gateway(B, claims["iss"], bot.uuid, key_b)
    assert source_a.grant_reader(grant_a["token"])[1] == frozenset({"host-a"})
    assert source_b.grant_reader(grant_b["token"])[1] == frozenset({"host-a"})
    for reader, grant in ((source_a, grant_b), (source_b, grant_a)):
        with pytest.raises(Rejected) as error:
            reader.grant_reader(grant["token"])
        assert error.value.status == 401
    # Replacing aud while keeping another source's signer is also rejected.
    forged = jwt.encode({**claims, "aud": B}, b"a" * 64, algorithm="HS256")
    with pytest.raises(Rejected):
        source_b.grant_reader(forged)


def test_unknown_candidate_source_cannot_use_legacy_signer(client, api, registry):
    path = f"/api/bots/{api['bot_id']}/utilization-access"
    assert client.post(path, headers=api["auth_header"], json=source_at(A)).status_code == 200
    rejected = client.post(path, headers=api["auth_header"], json=source_at("https://unknown.example.invalid"))
    assert rejected.status_code == 503 and "token" not in rejected.json()
    assert str(registry[0]) not in rejected.text


@pytest.mark.parametrize("setting", ["", "missing", "relative"])
def test_present_but_unreadable_registry_never_falls_back(registry, monkeypatch, setting):
    path = "missing.json" if setting == "relative" else str(registry[0].parent / "missing.json") if setting else ""
    monkeypatch.setenv("METRICS_GRANT_SIGNERS_FILE", path)
    assert select_metrics_signer(A) is None


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400, 0o666])
def test_unsafe_registry_or_key_permissions_reject_issuance(registry, mode):
    path, _config, key, _other = registry
    path.chmod(mode)
    assert select_metrics_signer(A) is None
    path.chmod(0o600)
    key.chmod(mode)
    assert select_metrics_signer(A) is None


def test_untrusted_file_owner_is_rejected(registry, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: registry[0].stat().st_uid + 12345)
    assert select_metrics_signer(A) is None


def test_registry_and_key_symlinks_are_rejected(registry, monkeypatch):
    path, config, key, _other = registry
    link = path.with_name("registry-link.json")
    link.symlink_to(path)
    monkeypatch.setenv("METRICS_GRANT_SIGNERS_FILE", str(link))
    assert select_metrics_signer(A) is None
    monkeypatch.setenv("METRICS_GRANT_SIGNERS_FILE", str(path))
    key_link = path.with_name("key-link")
    key_link.symlink_to(key)
    config["sources"][0]["key_file"] = str(key_link)
    write_registry(registry)
    assert select_metrics_signer(A) is None


@pytest.mark.parametrize("kind", ["audience", "path", "hardlink", "secret"])
def test_duplicate_audiences_or_reused_signing_keys_are_rejected(registry, kind):
    path, config, key_a, key_b = registry
    if kind == "audience":
        config["sources"][1]["audience"] = A
    elif kind == "path":
        config["sources"][1]["key_file"] = str(key_a)
    elif kind == "hardlink":
        link = path.with_name("hardlink.key")
        os.link(key_a, link)
        config["sources"][1]["key_file"] = str(link)
    else:
        key_b.write_bytes(key_a.read_bytes())
    write_registry(registry)
    assert select_metrics_signer(A) is None and select_metrics_signer(B) is None


@pytest.mark.parametrize(
    "audience",
    [
        "https://Metrics-a.example.invalid",
        A + ":443",
        A + "/",
        A + "/a/../b",
        A + "/a/./b",
        A + "/a//b",
        A + "/%70rometheus",
        A + "?",
        A + "#fragment",
        "https://user:password@metrics-a.example.invalid",
        "https://metrics-a.example.invalid\\evil",
        "https://metrics-a.example.invalid\n",
        "https://metrics-a.example.invalid.",
        "http://127.000.000.001",
        "http://0x7f000001",
    ],
)
def test_noncanonical_or_ambiguous_audiences_are_not_aliases(registry, audience):
    assert select_metrics_signer(audience) is None
    registry[1]["sources"][0]["audience"] = audience
    write_registry(registry)
    assert select_metrics_signer(A) is None


@pytest.mark.parametrize(
    "kind", ["oversized", "too_many", "duplicate_field", "unknown_field", "version", "short_key", "long_key"]
)
def test_malformed_or_unbounded_registry_is_rejected(registry, kind):
    path, config, key, _other = registry
    if kind == "oversized":
        path.write_bytes(b" " * (MAX_CONFIG_BYTES + 1))
    elif kind == "duplicate_field":
        path.write_text('{"version":1,"version":1,"sources":[]}')
    elif kind == "short_key":
        key.write_bytes(b"x" * 31)
    elif kind == "long_key":
        key.write_bytes(b"x" * 4097)
    else:
        if kind == "too_many":
            config["sources"] *= MAX_SOURCES + 1
        elif kind == "unknown_field":
            config["token"] = "not-supported"
        else:
            config["version"] = True
        write_registry(registry)
    assert select_metrics_signer(A) is None


def test_rotation_and_revocation_are_seen_without_restart_or_cache(api, db_session, registry):
    _path, config, key_a, _key_b = registry
    bot = db_session.get(Bot, api["bot_id"])
    first = issue_metrics_access(db_session, bot, source_at(A))
    claims = jwt.decode(first["token"], b"a" * 64, algorithms=["HS256"], audience=A)
    reader = gateway(A, claims["iss"], bot.uuid, key_a)
    private_file(key_a.with_suffix(".new"), b"rotated-key-material" * 4).replace(key_a)
    second = issue_metrics_access(db_session, bot, source_at(A))
    assert reader.grant_reader(second["token"])[1] == frozenset({"host-a"})
    with pytest.raises(Rejected):
        reader.grant_reader(first["token"])
    config["sources"] = config["sources"][1:]
    write_registry(registry)
    assert issue_metrics_access(db_session, bot, source_at(A)) is None
    assert issue_metrics_access(db_session, bot, source_at(B)) is not None
    # Already-issued grants remain valid until expiry unless the gateway also revokes.
    assert reader.grant_reader(second["token"])[1] == frozenset({"host-a"})
    reader.config["issuers"][claims["iss"]]["bots"] = {}
    with pytest.raises(Rejected) as error:
        reader.grant_reader(second["token"])
    assert error.value.status == 403


def test_legacy_mode_preserves_exact_audience_and_existing_key_mode(api, db_session, registry, monkeypatch):
    monkeypatch.delenv("METRICS_GRANT_SIGNERS_FILE")
    legacy = registry[0].parent / "legacy.key"
    legacy.chmod(0o400)
    endpoint = "https://Metrics.EXAMPLE.invalid:443/proxy/"
    bot = db_session.get(Bot, api["bot_id"])
    grant = issue_metrics_access(db_session, bot, source_at(endpoint))
    claims = jwt.decode(grant["token"], legacy.read_bytes(), algorithms=["HS256"], audience=endpoint.rstrip("/"))
    assert claims["aud"] == endpoint.rstrip("/")


def test_agent_context_and_public_config_never_expose_registry_paths_or_keys(client, api, registry):
    path = f"/api/bots/{api['bot_id']}"
    assert client.put(path, headers=api["auth_header"], json={"utilization_config": source_at(A)}).status_code == 200
    response = client.get("/api/agent/context", headers=api["headers"])
    assert response.status_code == 200
    payload = response.json()["utilization"]
    assert payload["configuration"]["endpoint"] == A and payload["access"]["scope"] == "metrics:read"
    for secret in (str(registry[0]), str(registry[2]), "a" * 64, "b" * 64, '"key_file"', '"sources"'):
        assert secret not in response.text
    candidate = {**source_at(A), "key_file": "/etc/untrusted-request.key"}
    assert client.post(path + "/utilization-access", headers=api["auth_header"], json=candidate).status_code == 422
