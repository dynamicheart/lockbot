"""Historical feedback keeps original source identities and current authorization."""

# ruff: noqa: F811 -- imported pytest fixture
import copy
import json
import sqlite3

import pytest

from backend.test_trace_review import review_api  # noqa: F401
from backend.test_trace_review_datasets import ROOT, dataset_run, private_json, publish


def create(client, headers, payload, **fields):
    response = client.post(ROOT + "/comments", headers=headers, json={**payload, **fields})
    assert response.status_code == 201, response.text
    return response.json()


def history(client, headers, **params):
    return client.get(
        ROOT + "/comments", headers=headers, params={"case_id": "lock-tail", "include_history": True, **params}
    )


def stored(directory):
    with sqlite3.connect(directory / "review-comments.sqlite3") as db:
        return list(db.execute("SELECT id, payload FROM comments ORDER BY rowid"))


def test_history_locates_original_published_answers_without_rebinding_or_persisting_metadata(review_api):
    client, headers, payload, source, directory, _ = review_api
    first, old_folder, _ = publish(directory, source)
    newer = copy.deepcopy(source)
    newer["cases"]["lock-tail"][0]["sessions"][0][2]["text"] = "A different answer"
    second, new_folder, _ = publish(directory, newer, title="newer")
    run_a, run_b = dataset_run(client, headers, first), dataset_run(client, headers, second)
    a = create(client, headers, payload, dataset_id=first, run_id=run_a)
    b = create(client, headers, payload, dataset_id=second, run_id=run_b)
    case = create(client, headers, payload, scope="case", anchor="case", run_id=None, run_index=None)
    ui = create(client, headers, payload, anchor="ui:counts")
    other, _, _ = publish(directory, source, title="other case", case_id="other")
    create(
        client, headers, payload, case_id="other", dataset_id=other, run_id=dataset_run(client, headers, other, "other")
    )
    before = stored(directory)
    files_before = [(folder / "raw.json").read_bytes() for folder in (old_folder, new_folder)]
    selected = {"dataset_id": second, "run_index": 0}
    default = client.get(ROOT + "/comments", headers=headers, params={"case_id": "lock-tail", **selected})
    assert default.json() == {"run_id": run_b, "comments": [b, case, ui]}
    response = history(client, headers, **selected)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "run_id": run_b,
        "comments": [
            dict(a, source_available=True),
            dict(b, source_available=True),
            dict(case, source_available=False),
            dict(ui, source_available=False),
        ],
    }
    original = client.get(
        ROOT + "/trial",
        headers=headers,
        params={key: a[key] for key in ("dataset_id", "case_id", "run_id", "run_index", "raw")},
    )
    assert original.status_code == 200
    assert original.json()["trial"] == source["cases"]["lock-tail"][0]
    assert original.json()["dataset_id"] == first
    assert stored(directory) == before
    assert [(folder / "raw.json").read_bytes() for folder in (old_folder, new_folder)] == files_before
    resolved = client.patch(
        ROOT + "/comments/" + a["id"], headers=headers, params={"dataset_id": first}, json={"status": "resolved"}
    )
    assert resolved.status_code == 200
    assert resolved.json()["run_id"] == run_a
    assert history(client, headers, **selected).json()["comments"][0]["status"] == "resolved"


def test_changed_legacy_source_retains_feedback_but_cannot_locate_or_save_against_new_answer(review_api):
    client, headers, payload, source, directory, _ = review_api
    record = create(client, headers, payload)
    source["cases"]["lock-tail"][0]["model"] = "replacement"
    (directory / "raw.json").write_text(json.dumps(source))
    before = stored(directory)
    assert history(client, headers).json()["comments"] == [dict(record, source_available=False)]
    for response in (
        client.get(
            ROOT + "/trial",
            headers=headers,
            params={key: record[key] for key in ("case_id", "run_id", "run_index", "raw")},
        ),
        client.post(ROOT + "/comments", headers=headers, json=payload),
    ):
        assert response.status_code == 409
        assert response.json()["code"] == "trace_changed"
        assert isinstance(response.json()["detail"], str)
        assert response.headers["Cache-Control"] == "no-store"
    assert stored(directory) == before


@pytest.mark.parametrize("unavailable", ["unpublished", "corrupt", "missing"])
def test_history_does_not_expose_unpublished_or_unverifiable_source_comments(review_api, unavailable):
    client, headers, payload, source, directory, _ = review_api
    first, folder, _ = publish(directory, source)
    second, _, _ = publish(directory, source, title="current")
    create(client, headers, payload, dataset_id=first, run_id=dataset_run(client, headers, first))
    current = create(client, headers, payload, dataset_id=second, run_id=dataset_run(client, headers, second))
    before = stored(directory)
    assert len(history(client, headers, dataset_id=second).json()["comments"]) == 2
    if unavailable == "unpublished":
        private_json(folder.parent / "index.json", {"schema_version": 1, "datasets": [second]})
    elif unavailable == "corrupt":
        (folder / "raw.json").write_text("{}")
    else:
        (folder / "raw.json").unlink()
    assert history(client, headers, dataset_id=second).json()["comments"] == [dict(current, source_available=True)]
    assert history(client, headers, dataset_id=first).status_code in {404, 503}
    assert stored(directory) == before


def test_history_uses_each_comments_visibility_and_original_run_index(review_api):
    client, headers, payload, source, directory, _ = review_api
    dataset, _, _ = publish(directory, source)
    snapshot = client.get(ROOT + "/snapshot", headers=headers, params={"dataset_id": dataset, "raw": False}).json()
    record = create(
        client,
        headers,
        payload,
        dataset_id=dataset,
        raw=False,
        run_index=1,
        run_id=snapshot["review_runs"]["lock-tail"][1],
    )
    row = history(client, headers, dataset_id=dataset, raw=True, run_index=0).json()["comments"][0]
    assert row == dict(record, source_available=True)
    query = {key: row[key] for key in ("dataset_id", "case_id", "run_id", "run_index", "raw")}
    assert client.get(ROOT + "/trial", headers=headers, params=query).status_code == 200
    assert client.get(ROOT + "/trial", headers=headers, params={**query, "raw": True}).status_code == 409


def test_history_requires_case_and_current_superadmin_permission(review_api, db_session):
    client, headers, payload, _, _, user = review_api
    create(client, headers, payload)
    response = client.get(ROOT + "/comments", headers=headers, params={"include_history": True})
    assert response.status_code == 422
    assert history(client, {}).status_code == 401
    assert history(client, headers).status_code == 200
    user.role = "admin"
    db_session.commit()
    response = history(client, headers)
    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "no-store"


def test_store_capacity_conflict_has_distinct_code_and_does_not_change_existing_records(review_api):
    client, headers, payload, _, directory, _ = review_api
    record = create(client, headers, payload)
    with sqlite3.connect(directory / "review-comments.sqlite3") as db:
        db.executemany(
            "INSERT INTO comments (id, payload) VALUES (?, ?)",
            [(str(index), json.dumps(dict(record, id=str(index)))) for index in range(999)],
        )
    before = stored(directory)
    response = client.post(ROOT + "/comments", headers=headers, json=payload)
    assert response.status_code == 409
    assert response.json() == {
        "code": "comment_store_full",
        "detail": "Comment store is full; ask the administrator to archive it",
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert stored(directory) == before
