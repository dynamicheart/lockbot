"""Datasource handoff must reject secrets/ambiguous mappings and preserve files."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills/lockbot-datasource-setup/scripts/generate_config.py"
spec = importlib.util.spec_from_file_location("datasource_handoff", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
BASE = {"endpoint": "https://metrics.example.invalid", "node_mapping": {"node-a": "source-a"}}


@pytest.mark.parametrize(
    "changes",
    [
        {"endpoint": "https://user:password@example.invalid"},
        {"endpoint": "https://example.invalid?token=secret"},
        {"endpoint": "https://example.invalid/#secret"},
        {"credential": "do-not-include"},
        {"interval_seconds": True},
        {"profile": "json"},
        {"device_mapping": {"unmapped:dev0": "uuid-a"}},
        {"device_mapping": {"node-a:dev0": "uuid-a", "node-a:dev1": "uuid-a"}},
    ],
)
def test_reject_secret_or_ambiguous_configuration(changes):
    with pytest.raises(ValueError):
        module.generate({**BASE, **changes})


def test_cli_delivers_private_files_and_refuses_overwrite(tmp_path):
    source = tmp_path / "input.json"
    source.write_text(json.dumps(BASE))
    out = tmp_path / "handoff"
    command = [sys.executable, str(SCRIPT), "--input", str(source), "--output-dir", str(out)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    files = {p.name: p.read_bytes() for p in out.iterdir()}
    assert set(files) == {"utilization-config.json", "node-mapping.json", "handoff.md"}
    config = json.loads(files["utilization-config.json"])
    assert config["nodeMapping"] == BASE["node_mapping"]
    assert config["accessMode"] == "independent"
    assert all(p.stat().st_mode & 0o077 == 0 for p in out.iterdir())
    assert subprocess.run(command, capture_output=True).returncode == 2
    assert files == {p.name: p.read_bytes() for p in out.iterdir()}


def test_cli_rejects_duplicate_json_keys_without_delivering_files(tmp_path):
    source = tmp_path / "input.json"
    source.write_text('{"endpoint":"https://a.invalid","endpoint":"https://b.invalid","node_mapping":{"node-a":"a"}}')
    out = tmp_path / "handoff"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(source), "--output-dir", str(out)], capture_output=True
    )
    assert result.returncode == 2
    assert not out.exists()
