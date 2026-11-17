"""A new case opens an actual run while stored review indices stay stable."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


def test_new_runs_do_not_open_plan_placeholders_or_semantic_copies():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the actual viewer functions")
    viewer = Path(__file__).resolve().parents[2] / "frontend/src/features/agent-traces/viewer.html"
    source = viewer.read_text()
    functions = []
    for name in ("defaultRun", "modelLabel"):
        functions.append(re.search(r"function " + name + r"\([^\n]*\) \{.*?\n\}", source, re.S)[0])
    script = "\n".join(functions) + """
const data = {cases: {
  example: [{model:'plan', state:'pending', source_run:'plan:example'},
    {model:'model-a', state:'pass', source_run:'matrix:old'},
    {model:'model-a', state:'fail', source_run:'matrix:new'},
    {model:'semantic', state:'pending', source_run:'semantic:note'}],
  other: [{model:'model-a', source_run:'matrix:only'}],
  create: [{model:'classic', direct:true}, {model:'new', source_run:'matrix:new'}],
  planned: [{model:'plan', state:'pending'}],
}};
const activeCase = 'example';
const before = JSON.stringify(data);
console.log(JSON.stringify({
  latest: defaultRun('example'), classic: defaultRun('create'), planned: defaultRun('planned'),
  label1:modelLabel(data.cases.example[1],1), label2:modelLabel(data.cases.example[2],2),
  other:modelLabel(data.cases.other[0],0,'other'), unchanged:JSON.stringify(data)===before,
}));
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout) == {
        "latest": 2,
        "classic": 0,
        "planned": 0,
        "label1": "model-a · 记录 1",
        "label2": "model-a · 记录 2",
        "other": "model-a",
        "unchanged": True,
    }
