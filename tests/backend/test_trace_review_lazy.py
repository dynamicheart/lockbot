"""Lazy catalogs never rewrite evidence or bypass current authorization/integrity checks."""

# ruff: noqa: F811 -- imported pytest fixture
import copy
import hashlib
import json
import os

import pytest
from lockbot.backend.app import trace_review

from backend.test_trace_review import review_api  # noqa: F401
from backend.test_trace_review_datasets import ROOT, private_json, publish


def rich_source(source):
    result = copy.deepcopy(source)
    result.update(labels={"lock-tail": "Exact target"}, groups={"lock-tail": "Allocation"})
    result["criteria"] = {"lock-tail": "PRIVATE_LARGE_CRITERIA" * 5000}
    for trial in result["cases"]["lock-tail"]:
        trial.update(
            state="pass",
            source_run="immutable-original:case:0",
            package_version="package-123",
            review_phase=2,
            semantic_status="pass",
            overall_review_status="uncertain",
            action_safety_status="pass",
            elapsed_seconds=12.5,
            metrics={"tool_calls": 3, "tokens": 456, "huge": "PRIVATE_METRIC" * 5000},
            note="PRIVATE_LARGE_NOTE" * 10000,
            checks={"private": "PRIVATE_LARGE_CHECK" * 5000},
        )
    return result


def get_catalog(client, headers, dataset_id=None, *, raw=True):
    query = {"raw": raw}
    if dataset_id is not None:
        query["dataset_id"] = dataset_id
    return client.get(ROOT + "/catalog", headers=headers, params=query)


def get_trial(client, headers, run_id, dataset_id=None, *, case_id="lock-tail", run_index=0, raw=True):
    query = {"raw": raw, "case_id": case_id, "run_index": run_index, "run_id": run_id}
    if dataset_id is not None:
        query["dataset_id"] = dataset_id
    return client.get(ROOT + "/trial", headers=headers, params=query)


def test_catalog_is_lightweight_and_trial_retains_original_evidence_and_ids(review_api):
    client, headers, _, source, directory, _ = review_api
    original = rich_source(source)
    dataset_id, folder, _ = publish(directory, original)
    before = {name: (folder / name).read_bytes() for name in ("raw.json", "latest.json", "metadata.json")}
    full = client.get(ROOT + "/snapshot", headers=headers, params={"raw": True, "dataset_id": dataset_id})
    response = get_catalog(client, headers, dataset_id)
    assert response.status_code == 200, response.text
    catalog = response.json()
    assert catalog["schema"] == 1 and catalog["lazy"] is True
    assert catalog["dataset_id"] == dataset_id
    assert catalog["version"] == original["version"] and catalog["visibility"] == "raw"
    assert catalog["labels"] == original["labels"] and catalog["groups"] == original["groups"]
    assert catalog["review_runs"] == full.json()["review_runs"]
    assert "criteria" not in catalog
    assert "PRIVATE_" not in response.text and "lock 19" not in response.text
    expected = {
        key: value
        for key, value in original["cases"]["lock-tail"][0].items()
        if key not in {"sessions", "note", "checks", "metrics"}
    }
    expected.update(sessions=[], lazy=True, metrics={"tool_calls": 3, "tokens": 456})
    assert catalog["cases"]["lock-tail"][0] == expected
    run_id = catalog["review_runs"]["lock-tail"][0]
    trial = get_trial(client, headers, run_id, dataset_id)
    assert trial.status_code == 200, trial.text
    assert trial.json() == {
        "dataset_id": dataset_id,
        "case_id": "lock-tail",
        "run_index": 0,
        "run_id": run_id,
        "trial": original["cases"]["lock-tail"][0],
    }
    assert (
        json.dumps(trial.json()["trial"], sort_keys=True).encode()
        == json.dumps(original["cases"]["lock-tail"][0], sort_keys=True).encode()
    )
    assert {name: (folder / name).read_bytes() for name in before} == before
    assert hashlib.sha256(before["raw.json"]).hexdigest() == dataset_id
    assert len(response.content) < len(full.content) // 100
    print(f"lazy catalog bytes={len(response.content)}; full snapshot bytes={len(full.content)}")
    for reply in (response, trial):
        assert reply.headers["Cache-Control"] == "no-store"
        assert reply.headers["X-Content-Type-Options"] == "nosniff"


def test_registered_source_is_hashed_once_then_guarded_and_parsed_cache_is_bounded(review_api, monkeypatch):
    client, headers, _, source, directory, _ = review_api
    sources = [publish(directory, source, title=f"source {index}") for index in range(5)]
    data = (sources[0][1] / "raw.json").read_bytes()
    calls = []
    original_sha = hashlib.sha256

    def counted(content=b"", *args, **kwargs):
        if content == data:
            calls.append(1)
        return original_sha(content, *args, **kwargs)

    monkeypatch.setattr(trace_review.hashlib, "sha256", counted)
    first_id = sources[0][0]
    catalog = get_catalog(client, headers, first_id).json()
    run_id = catalog["review_runs"]["lock-tail"][0]
    assert get_trial(client, headers, run_id, first_id).status_code == 200
    assert get_catalog(client, headers, first_id).status_code == 200
    assert calls == [1]
    for dataset_id, _, _ in sources[1:]:
        assert get_catalog(client, headers, dataset_id).status_code == 200
    assert get_catalog(client, headers, first_id).status_code == 200
    assert calls == [1, 1]  # Fifth distinct source evicts the least recently used source.


def test_cached_data_still_requires_the_current_superadmin(review_api, db_session):
    client, headers, _, source, directory, user = review_api
    dataset_id, _, _ = publish(directory, source)
    run_id = get_catalog(client, headers, dataset_id).json()["review_runs"]["lock-tail"][0]
    assert get_trial(client, headers, run_id, dataset_id).status_code == 200
    user.role = "admin"
    db_session.commit()
    for endpoint in ("catalog", "trial"):
        query = {"dataset_id": dataset_id, "raw": True, "case_id": "lock-tail", "run_index": 0, "run_id": run_id}
        assert client.get(ROOT + "/" + endpoint, params=query).status_code == 401
        response = client.get(ROOT + "/" + endpoint, headers=headers, params=query)
        assert response.status_code == 403 and response.headers["Cache-Control"] == "no-store"


def test_catalog_and_trial_keep_dataset_and_visibility_scopes_separate(review_api):
    client, headers, payload, source, directory, _ = review_api
    first, _, _ = publish(directory, source, title="first")
    other = copy.deepcopy(source)
    other["cases"]["lock-tail"][0]["sessions"][0][0]["text"] = "different original prompt"
    second, _, _ = publish(directory, other, title="second")
    raw = get_catalog(client, headers, first).json()
    sanitized = get_catalog(client, headers, first, raw=False).json()
    different = get_catalog(client, headers, second).json()
    ids = [entry["review_runs"]["lock-tail"][0] for entry in (raw, sanitized, different)]
    assert len({*ids, payload["run_id"]}) == 4
    assert get_trial(client, headers, ids[0], second).status_code == 409
    assert get_trial(client, headers, ids[0], first, raw=False).status_code == 409
    assert get_trial(client, headers, ids[2], second).json()["trial"] == other["cases"]["lock-tail"][0]
    assert get_trial(client, headers, ids[0], first).json()["trial"] == source["cases"]["lock-tail"][0]
    assert get_trial(client, headers, ids[0], first, case_id="missing").status_code == 404
    for index in (-1, 2, 10001):
        assert get_trial(client, headers, ids[0], first, run_index=index).status_code == 404


@pytest.mark.parametrize(
    "change",
    ["corrupt", "restore_mtime", "replace", "symlink", "hardlink", "permissions", "missing", "metadata", "unregister"],
)
def test_cached_source_mutation_is_never_served_as_stale_content(review_api, change):
    client, headers, _, source, directory, _ = review_api
    dataset_id, folder, metadata = publish(directory, source)
    catalog = get_catalog(client, headers, dataset_id)
    assert catalog.status_code == 200
    run_id = catalog.json()["review_runs"]["lock-tail"][0]
    target = folder / "raw.json"
    original = target.read_bytes()
    details = target.stat()
    corrupt = original.replace(b"same-model", b"evil-model")
    if change in {"corrupt", "restore_mtime"}:
        target.write_bytes(corrupt)
        if change == "restore_mtime":
            os.utime(target, ns=(details.st_atime_ns, details.st_mtime_ns))
            assert target.stat().st_mtime_ns == details.st_mtime_ns
    elif change == "replace":
        replacement = folder / "replacement"
        replacement.write_bytes(corrupt)
        replacement.chmod(0o600)
        os.utime(replacement, ns=(details.st_atime_ns, details.st_mtime_ns))
        replacement.replace(target)
    elif change in {"symlink", "hardlink"}:
        elsewhere = directory / "original-copy"
        target.rename(elsewhere)
        if change == "symlink":
            target.symlink_to(elsewhere)
        else:
            os.link(elsewhere, target)
    elif change == "permissions":
        target.chmod(0o644)
    elif change == "missing":
        target.unlink()
    elif change == "metadata":
        private_json(folder / "metadata.json", {**metadata, "raw_sha256": "e" * 64})
    else:
        private_json(folder.parent / "index.json", {"schema_version": 1, "datasets": []})
    expected = 404 if change == "unregister" else 503
    for response in (get_catalog(client, headers, dataset_id), get_trial(client, headers, run_id, dataset_id)):
        assert response.status_code == expected
        assert response.headers["Cache-Control"] == "no-store"
        assert "sessions" not in response.text


@pytest.mark.parametrize("selector", ["../raw.json", "F" * 64, "f" * 63, "f" * 64, None])
def test_missing_unregistered_and_path_selectors_fail_closed(review_api, selector):
    client, headers, _, source, directory, _ = review_api
    unregistered, _, _ = publish(directory, source, register=False)
    selected = unregistered if selector is None else selector
    assert get_catalog(client, headers, selected).status_code == 404
    assert get_trial(client, headers, "0" * 64, selected).status_code == 404


def test_legacy_mutable_root_is_not_cached_and_comments_keep_original_validation(review_api):
    client, headers, payload, source, directory, _ = review_api
    assert get_catalog(client, headers).json()["review_runs"]["lock-tail"][0] == payload["run_id"]
    assert get_trial(client, headers, payload["run_id"]).json()["trial"] == source["cases"]["lock-tail"][0]
    source["cases"]["lock-tail"][0]["sessions"][0][0]["text"] = "changed mutable prompt"
    (directory / "raw.json").write_text(json.dumps(source))
    assert get_trial(client, headers, payload["run_id"]).status_code == 409
    current = get_catalog(client, headers).json()["review_runs"]["lock-tail"][0]
    assert current != payload["run_id"]
    assert client.post(ROOT + "/comments", headers=headers, json=payload).status_code == 409
    assert get_trial(client, headers, current).json()["trial"] == source["cases"]["lock-tail"][0]
