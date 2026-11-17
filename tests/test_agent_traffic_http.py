"""Exercise actual route limiters over a private loopback service."""

import json
import subprocess
import sys
from pathlib import Path


def test_shared_ip_quota_boundaries_over_real_http(tmp_path):
    script = Path(__file__).parent / "integration/http_contract_probe.py"
    output = tmp_path / "traffic-proof.json"
    result = subprocess.run(
        [sys.executable, str(script), "--traffic-control", "--output", str(output)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    proof = json.loads(output.read_text())
    assert proof["passed"] is True
    assert proof["traffic_controls"] == {
        "same_ip_independent_read_budgets": True,
        "thirty_writes_then_429_other_credential_unaffected": True,
        "fifty_pending_bindings_then_429": True,
    }
