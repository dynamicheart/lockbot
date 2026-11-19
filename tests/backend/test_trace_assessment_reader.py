"""Optional assessment sidecars cannot move feedback identities or hide original evidence."""

# ruff: noqa: F811
import sys
from pathlib import Path

import pytest

from backend.test_trace_review import review_api  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/trace-viewer"))
import publish_dataset as publisher  # noqa: E402
from test_trace_assessment_overlay import saved_review, source_and_review  # noqa: E402

ROOT = "/api/dev/trace-review"


@pytest.mark.parametrize("release_case", [False, True])
def test_registered_model_review_keeps_comment_and_run_ids_and_source_bytes(review_api, tmp_path, release_case):
    client, headers, _, _, directory, _ = review_api
    options = {"suite": "cli-release", "scenario": "alias-set", "release_fixture_version": 6} if release_case else {}
    source, record, report = source_and_review(tmp_path, **options)
    first = publisher.export_dataset(source, directory, minimal_summary=True, release_assessments=release_case)
    identity = first["dataset"]["dataset_id"]
    folder = directory / "datasets" / identity
    original = {name: (folder / name).read_bytes() for name in ("raw.json", "latest.json", "metadata.json")}
    params = {"raw": True, "dataset_id": identity}
    before = client.get(ROOT + "/catalog", headers=headers, params=params).json()
    case = record["target"]["case_id"]
    run_id = before["review_runs"][case][0]
    assert before["assessments"][record["target"]["source_run"]]["semantic"] == "not_reviewed"
    comment = client.post(
        ROOT + "/comments",
        headers=headers,
        json={
            "case_id": case,
            "run_index": 0,
            "run_id": run_id,
            "raw": True,
            "dataset_id": identity,
            "anchor": "run",
            "quote": "",
            "body": "Keep this run's comment anchored.",
        },
    )
    assert comment.status_code == 201, comment.text
    sidecar = saved_review(tmp_path, record)
    second = publisher.export_dataset(
        source,
        directory,
        minimal_summary=True,
        assessment_reviews=[(sidecar, report)],
        release_assessments=release_case,
    )
    assert second["dataset"] == first["dataset"]
    after = client.get(ROOT + "/catalog", headers=headers, params=params).json()
    assert after["review_runs"] == before["review_runs"]
    assert after["cases"] == before["cases"]
    assessment = after["assessments"][record["target"]["source_run"]]
    assert assessment["semantic"] == "pass" and assessment["human_gate"] == "not_required"
    assert assessment["reviews"][0]["reviewer"]["kind"] == "model"
    trial = client.get(
        ROOT + "/trial", headers=headers, params={**params, "case_id": case, "run_index": 0, "run_id": run_id}
    )
    assert trial.status_code == 200
    assert trial.json()["trial"]["state"] == ("pass" if release_case else "pending")
    assert {name: (folder / name).read_bytes() for name in original} == original
    comments = client.get(ROOT + "/comments", headers=headers).json()
    assert any(item["id"] == comment.json()["id"] and item["run_id"] == run_id for item in comments["comments"])
    assert client.get(ROOT + "/catalog", params=params).status_code == 401


def test_tampered_or_missing_registered_assessment_never_hides_original_trace(review_api, tmp_path):
    client, headers, _, _, directory, _ = review_api
    source, _, _ = source_and_review(tmp_path)
    result = publisher.export_dataset(source, directory, minimal_summary=True)
    identity = result["dataset"]["dataset_id"]
    params = {"raw": True, "dataset_id": identity}
    before = client.get(ROOT + "/catalog", headers=headers, params=params).json()
    record_path = directory / "assessments" / (result["assessment_overlay"]["id"] + ".json")
    original = record_path.read_bytes()
    record_path.write_bytes(original + b" ")
    response = client.get(ROOT + "/catalog", headers=headers, params=params)
    assert response.status_code == 200
    assert response.json()["review_runs"] == before["review_runs"]
    assert response.json()["cases"] == before["cases"]
    assert response.json().get("assessments") is None and response.json()["assessment_error"] is True
    record_path.unlink()
    response = client.get(ROOT + "/snapshot", headers=headers, params=params)
    assert response.status_code == 200 and response.json()["assessment_error"] is True
    assert response.json()["cases"][record_target(before)][0]["sessions"]


def record_target(catalog):
    return next(iter(catalog["cases"]))


def test_cli_whole_run_assessment_retains_original_comment_coordinates(review_api, tmp_path):
    from test_trace_cli_run_assessment import review, source_run

    client, headers, _, _, directory, _ = review_api
    source, target = source_run(tmp_path)
    initial = publisher.export_dataset(source, directory, minimal_summary=True)
    dataset = initial["dataset"]["dataset_id"]
    params = {"raw": True, "dataset_id": dataset}
    before = client.get(ROOT + "/catalog", headers=headers, params=params).json()
    case = target["scenario"]
    run_id = before["review_runs"][case][0]
    comment = client.post(
        ROOT + "/comments",
        headers=headers,
        json={
            "case_id": case,
            "run_index": 0,
            "run_id": run_id,
            "raw": True,
            "dataset_id": dataset,
            "anchor": "run",
            "quote": "",
            "body": "Keep the original multi-phase run comment.",
        },
    )
    assert comment.status_code == 201
    record, report = review(tmp_path, source, target)
    publisher.export_dataset(
        source,
        directory,
        minimal_summary=True,
        cli_assessments=True,
        assessment_reviews=[(saved_review(tmp_path, record), report)],
    )
    after = client.get(ROOT + "/catalog", headers=headers, params=params).json()
    assert after["review_runs"] == before["review_runs"] and after["cases"] == before["cases"]
    entry = after["assessments"][record["target"]["source_run"]]
    assert entry["schema_version"] == 2 and entry["semantic"] == "pass"
    assert len(entry["phase_evidence"]) == 2 and entry["human_gate"] == "not_required"
    comments = client.get(ROOT + "/comments", headers=headers).json()["comments"]
    assert any(item["id"] == comment.json()["id"] and item["run_id"] == run_id for item in comments)
