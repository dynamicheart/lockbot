import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "tools/trace-viewer/publish_snapshot.py"


def test_snapshot_waits_for_complete_case_and_excludes_private_input(tmp_path):
    plan = {
        "simulation_only": True,
        "artifacts": {"current": {"package_version": "0123456789abcdef"}},
        "cases": ["cli:reset-personal-key"],
        "runners": [{"client": "private-provider", "model": "private-model"}],
    }
    (tmp_path / "run.json").write_text(json.dumps(plan))
    rows = [
        {
            "client": "private-provider",
            "model": "private-model",
            "scenario": "reset-personal-key",
            "status": "completed",
            "observable_pass": True,
            "phase": phase,
            "model_turns": 3,
            "evidence": "/private/file",
            "response": "lbr_secretinputfromprivatetrace",
        }
        for phase in range(1, 4)
    ]
    progress = tmp_path / "progress.jsonl"
    progress.write_text("\n".join(map(json.dumps, rows)))
    output = tmp_path / "latest.json"
    argv = [sys.executable, str(SCRIPT), str(tmp_path), str(output)]
    subprocess.run(argv, check=True)
    assert not output.exists()

    with progress.open("a") as trace:
        trace.write("\n")
    subprocess.run(argv, check=True)
    raw = output.read_text()
    for forbidden in ("lbr_", "/private/file", "private-provider", "private-model", "response"):
        assert forbidden not in raw
    case = json.loads(raw)["cases"]["case-1"][0]
    assert case["state"] == "pass"
    assert case["sessions"] == []
    assert [stage["turns"] for stage in case["metrics"]["phases"]] == [3, 3, 3]
