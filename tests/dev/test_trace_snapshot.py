import json
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

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


def test_follow_emits_each_finished_case_before_matrix_completion(tmp_path):
    models = ["glm-5.3", "kimi-k3"]
    plan = {
        "simulation_only": True,
        "artifacts": {"current": {"package_version": "0123456789abcdef"}},
        "cases": ["cli:reset-personal-key"],
        "runners": [{"client": "fixture", "model": model} for model in models],
    }
    (tmp_path / "run.json").write_text(json.dumps(plan))
    progress = tmp_path / "progress.jsonl"
    callback = tmp_path / "transfer.py"
    delivered = tmp_path / "delivered.jsonl"
    callback.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "state = json.loads(Path(sys.argv[2]).read_text())\n"
        "with Path(sys.argv[1]).open('a') as output:\n"
        "    output.write(str(len(state['cases']['case-1'])) + '\\n')\n"
    )
    transport = tmp_path / "transfer.json"
    transport.write_text(json.dumps([sys.executable, str(callback), str(delivered)]))
    argv = [
        sys.executable,
        str(SCRIPT),
        str(tmp_path),
        str(tmp_path / "latest.json"),
        "--follow",
        "--on-update",
        str(transport),
    ]
    with subprocess.Popen(argv) as publisher:
        try:
            for number, model in enumerate(models, 1):
                with progress.open("a") as log:
                    for phase in range(1, 4):
                        row = {
                            "client": "fixture",
                            "model": model,
                            "scenario": "reset-personal-key",
                            "status": "completed",
                            "observable_pass": True,
                            "phase": phase,
                        }
                        log.write(json.dumps(row) + "\n")
                deadline = monotonic() + 8
                while monotonic() < deadline:
                    if delivered.exists() and len(delivered.read_text().splitlines()) == number:
                        break
                    sleep(0.05)
                else:
                    raise AssertionError("Publisher failed to deliver a finished case")
                assert not (tmp_path / "matrix.json").exists()
            (tmp_path / "matrix.json").write_text("{}")
            assert publisher.wait(timeout=8) == 0
        finally:
            if publisher.poll() is None:
                publisher.terminate()
    assert delivered.read_text().splitlines() == ["1", "2"]
