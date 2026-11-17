"""Registered private datasets retain legacy evidence and feedback identities."""

# ruff: noqa: F811 -- imported pytest fixture
import hashlib
import json
import os

import pytest

from backend.test_trace_review import review_api  # noqa: F401

ROOT = "/api/dev/trace-review"


def private_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False))
    path.chmod(0o600)


def publish(directory, source, *, title="new run", case_id=None, register=True):
    raw = {**source, "title": title}
    if case_id:
        raw["cases"] = {case_id: source["cases"]["lock-tail"]}
    data = json.dumps(raw, ensure_ascii=False).encode()
    latest = json.dumps({**raw, "visibility": "sanitized"}, ensure_ascii=False).encode()
    dataset_id = hashlib.sha256(data).hexdigest()
    folder = directory / "datasets"
    folder.mkdir(mode=0o700, exist_ok=True)
    target = folder / dataset_id
    target.mkdir(mode=0o700)
    for name, content in (("raw.json", data), ("latest.json", latest)):
        (target / name).write_bytes(content)
        (target / name).chmod(0o600)
    metadata = {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "raw_sha256": dataset_id,
        "latest_sha256": hashlib.sha256(latest).hexdigest(),
        "raw_size_bytes": len(data),
        "latest_size_bytes": len(latest),
        "title": title,
        "created_at": "2026-11-18T00:00:00+00:00",
        "version": source.get("version"),
        "case_count": len(raw["cases"]),
        "run_count": sum(map(len, raw["cases"].values())),
        "source_matrix_sha256": "e" * 64,
    }
    private_json(target / "metadata.json", metadata)
    if register:
        index = folder / "index.json"
        ids = json.loads(index.read_text())["datasets"] if index.exists() else []
        private_json(index, {"schema_version": 1, "datasets": [*ids, dataset_id]})
    return dataset_id, target, metadata


def dataset_run(client, headers, dataset_id, case="lock-tail"):
    response = client.get(ROOT + "/snapshot", headers=headers, params={"raw": True, "dataset_id": dataset_id})
    assert response.status_code == 200, response.text
    return response.json()["review_runs"][case][0]


def test_dataset_index_and_snapshot_keep_legacy_digest_and_files_exact(review_api):
    client, headers, payload, source, directory, _ = review_api
    legacy = (directory / "raw.json").read_bytes()
    first, _, metadata = publish(directory, source)
    second, _, _ = publish(directory, source, title="another run")
    response = client.get(ROOT + "/datasets", headers=headers)
    assert response.status_code == 200
    assert response.json()["datasets"][0] == metadata
    assert len(response.json()["datasets"]) == 2
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get(ROOT + "/datasets").status_code == 401
    assert client.get(ROOT + "/datasets").headers["Cache-Control"] == "no-store"
    assert (
        client.get(ROOT + "/snapshot", headers=headers, params={"dataset_id": first, "raw": True}).json()["dataset_id"]
        == first
    )
    first_run = dataset_run(client, headers, first)
    second_run = dataset_run(client, headers, second)
    assert len({first_run, second_run, payload["run_id"]}) == 3
    expected = hashlib.sha256(
        json.dumps(
            {
                "raw": True,
                "version": source["version"],
                "case_id": "lock-tail",
                "run_index": 0,
                "trial": source["cases"]["lock-tail"][0],
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    legacy_response = client.get(ROOT + "/snapshot", headers=headers, params={"raw": True, "run": first}).json()
    assert legacy_response["review_runs"]["lock-tail"][0] == expected == payload["run_id"]
    assert "dataset_id" not in legacy_response
    assert (directory / "raw.json").read_bytes() == legacy
    assert client.post(ROOT + "/datasets", headers=headers, json={}).status_code == 405


def test_dataset_feedback_separates_trials_but_preserves_case_and_arbitrary_ui_targets(review_api):
    client, headers, payload, source, directory, _ = review_api
    first, _, _ = publish(directory, source)
    second, _, _ = publish(directory, source, title="second")
    run_a, run_b = dataset_run(client, headers, first), dataset_run(client, headers, second)
    path = ROOT + "/comments"
    legacy = client.post(path, headers=headers, json=payload).json()
    a = client.post(path, headers=headers, json={**payload, "dataset_id": first, "run_id": run_a}).json()
    b = client.post(path, headers=headers, json={**payload, "dataset_id": second, "run_id": run_b}).json()
    case_payload = {key: value for key, value in payload.items() if key not in {"run_index", "run_id"}}
    case = client.post(
        path, headers=headers, json={**case_payload, "dataset_id": first, "scope": "case", "anchor": "case"}
    ).json()
    ui = client.post(
        path,
        headers=headers,
        json={**payload, "dataset_id": first, "run_id": run_a, "anchor": "ui:payload-tab", "subject": "interface"},
    ).json()
    assert "dataset_id" not in case and "dataset_id" not in ui and "dataset_id" not in legacy
    assert a["dataset_id"] == first and b["dataset_id"] == second
    selected = client.get(
        path, headers=headers, params={"case_id": "lock-tail", "run_index": 0, "dataset_id": first}
    ).json()
    assert selected["run_id"] == run_a
    assert {row["id"] for row in selected["comments"]} == {a["id"], case["id"], ui["id"]}
    assert {row["id"] for row in client.get(path, headers=headers).json()["comments"]} == {
        legacy["id"],
        a["id"],
        b["id"],
        case["id"],
        ui["id"],
    }
    assert client.post(path, headers=headers, json={**payload, "dataset_id": first, "run_id": run_b}).status_code == 409
    assert (
        client.patch(
            path + "/" + a["id"], headers=headers, params={"dataset_id": second}, json={"status": "resolved"}
        ).status_code
        == 404
    )
    assert (
        client.patch(
            path + "/" + a["id"], headers=headers, params={"dataset_id": first}, json={"status": "resolved"}
        ).status_code
        == 200
    )
    assert (
        client.patch(
            path + "/" + ui["id"], headers=headers, params={"dataset_id": second}, json={"status": "resolved"}
        ).status_code
        == 200
    )


def test_case_feedback_validates_new_dataset_case_but_is_stored_globally(review_api):
    client, headers, payload, source, directory, _ = review_api
    dataset_id, _, _ = publish(directory, source, case_id="new-only-case")
    body = {key: value for key, value in payload.items() if key not in {"run_index", "run_id"}}
    body.update(scope="case", anchor="case", case_id="new-only-case", dataset_id=dataset_id)
    response = client.post(ROOT + "/comments", headers=headers, json=body)
    assert response.status_code == 201
    assert "dataset_id" not in response.json()
    listed = client.get(ROOT + "/comments", headers=headers, params={"case_id": "new-only-case"}).json()["comments"]
    assert listed == [response.json()]


@pytest.mark.parametrize("dataset_id", ["../raw.json", "A" * 64, "f" * 63, "f" * 65, "not-a-hash", "f" * 64])
def test_unknown_or_invalid_dataset_selectors_never_read_unregistered_files(review_api, dataset_id):
    client, headers, payload, source, directory, _ = review_api
    publish(directory, source, register=False)
    for path in ("/snapshot", "/comments"):
        response = client.get(ROOT + path, headers=headers, params={"raw": True, "dataset_id": dataset_id})
        assert response.status_code == 404
        assert response.headers["Cache-Control"] == "no-store"
    assert client.get(ROOT + "/datasets", headers=headers).json() == {"datasets": []}


def test_dataset_directory_is_unpublished_until_the_registry_is_written_last(review_api):
    client, headers, _, source, directory, _ = review_api
    dataset_id, folder, _ = publish(directory, source, register=False)
    query = {"raw": True, "dataset_id": dataset_id}
    assert client.get(ROOT + "/snapshot", headers=headers, params=query).status_code == 404
    private_json(folder.parent / "index.json", {"schema_version": 1, "datasets": []})
    assert client.get(ROOT + "/snapshot", headers=headers, params=query).status_code == 404
    private_json(folder.parent / "index.json", {"schema_version": 1, "datasets": [dataset_id]})
    assert client.get(ROOT + "/snapshot", headers=headers, params=query).status_code == 200


@pytest.mark.parametrize(
    "unsafe",
    [
        "tampered_raw",
        "tampered_latest",
        "file_symlink",
        "dataset_symlink",
        "catalog_symlink",
        "index_symlink",
        "metadata_symlink",
        "directory",
        "fifo",
        "permissions",
        "dataset_permissions",
        "catalog_permissions",
        "oversized",
        "metadata_id",
        "metadata_size",
        "metadata_depth",
        "missing",
        "registry_invalid",
        "registry_overflow",
    ],
)
def test_dataset_reads_fail_closed_for_unsafe_published_artifacts(review_api, monkeypatch, unsafe):
    import lockbot.backend.app.trace_review as module

    client, headers, _, source, directory, _ = review_api
    dataset_id, folder, metadata = publish(directory, source)
    artifact = folder / "raw.json"
    query = {"raw": True, "dataset_id": dataset_id}
    private_config = directory / "private-config.json"
    private_config.write_text("PRIVATE_CONFIGURATION_MUST_NOT_BE_SERVED")
    private_config.chmod(0o600)
    if unsafe.startswith("tampered"):
        artifact = folder / ("latest.json" if unsafe == "tampered_latest" else "raw.json")
        query["raw"] = unsafe != "tampered_latest"
        artifact.write_bytes(artifact.read_bytes().replace(b"same-model", b"evil-model"))
    elif unsafe.endswith("symlink"):
        artifact = {
            "file_symlink": artifact,
            "metadata_symlink": folder / "metadata.json",
            "index_symlink": folder.parent / "index.json",
            "dataset_symlink": folder,
            "catalog_symlink": folder.parent,
        }[unsafe]
        if artifact.is_dir():
            moved = directory / "moved"
            artifact.rename(moved)
            artifact.symlink_to(moved, target_is_directory=True)
        else:
            artifact.unlink()
            artifact.symlink_to(private_config)
    elif unsafe in {"directory", "fifo", "missing"}:
        artifact.unlink()
        if unsafe == "directory":
            artifact.mkdir(mode=0o600)
        elif unsafe == "fifo":
            os.mkfifo(artifact, mode=0o600)
    elif unsafe.endswith("permissions"):
        {"permissions": artifact, "dataset_permissions": folder, "catalog_permissions": folder.parent}[unsafe].chmod(
            0o755 if unsafe != "permissions" else 0o644
        )
    elif unsafe == "oversized":
        with artifact.open("r+b") as stream:
            stream.truncate(32 * 1024 * 1024 + 1)
    elif unsafe == "metadata_depth":
        (folder / "metadata.json").write_text("[" * 1100 + "]" * 1100)
    elif unsafe.startswith("metadata"):
        metadata["dataset_id" if unsafe == "metadata_id" else "raw_size_bytes"] = "x"
        private_json(folder / "metadata.json", metadata)
    elif unsafe == "registry_invalid":
        private_json(folder.parent / "index.json", {"schema_version": 1, "datasets": ["../../private-config.json"]})
    else:
        monkeypatch.setattr(module, "MAX_DATASETS", 0)
    response = client.get(ROOT + "/snapshot", headers=headers, params=query)
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert "PRIVATE_CONFIGURATION" not in response.text
    if not unsafe.startswith("tampered"):
        assert client.get(ROOT + "/datasets", headers=headers).status_code == 503


@pytest.mark.parametrize("limit", ["MAX_DATASET_METADATA_BYTES", "MAX_DATASET_INDEX_BYTES"])
def test_dataset_metadata_and_index_reads_are_bounded(review_api, monkeypatch, limit):
    import lockbot.backend.app.trace_review as module

    client, headers, _, source, directory, _ = review_api
    dataset_id, _, _ = publish(directory, source)
    monkeypatch.setattr(module, limit, 1)
    assert client.get(ROOT + "/datasets", headers=headers).status_code == 503
    assert client.get(ROOT + "/snapshot", headers=headers, params={"dataset_id": dataset_id}).status_code == 503


def test_dataset_index_metadata_response_has_an_explicit_total_cap(review_api, monkeypatch):
    import lockbot.backend.app.trace_review as module

    client, headers, _, source, directory, _ = review_api
    dataset_id, _, _ = publish(directory, source)
    monkeypatch.setattr(module, "MAX_DATASET_LIST_BYTES", 1)
    assert client.get(ROOT + "/datasets", headers=headers).status_code == 503
    # An existing bookmark still selects one bounded, registered dataset.
    assert client.get(ROOT + "/snapshot", headers=headers, params={"dataset_id": dataset_id}).status_code == 200


def test_dataset_routes_recheck_current_admin_role_and_validation_is_not_cached(review_api, db_session):
    client, headers, payload, source, directory, user = review_api
    dataset_id, _, _ = publish(directory, source)
    response = client.post(ROOT + "/comments", headers=headers, json={**payload, "dataset_id": "bad"})
    assert response.status_code == 422 and response.headers["Cache-Control"] == "no-store"
    user.role = "admin"
    db_session.commit()
    for path in ("/datasets", "/snapshot", "/comments"):
        response = client.get(ROOT + path, headers=headers, params={"dataset_id": dataset_id})
        assert response.status_code == 403 and response.headers["Cache-Control"] == "no-store"
