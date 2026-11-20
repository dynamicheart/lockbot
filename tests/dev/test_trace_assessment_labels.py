"""The shipped viewer distinguishes model judgment from human approval without extra runs."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/trace-viewer"))
import assessment_overlay as assessments  # noqa: E402
import publish_dataset as publisher  # noqa: E402
from test_trace_assessment_overlay import exported, source_and_review  # noqa: E402
from test_trace_dataset_ui import INTERACTIONS, VIEWER, function, run  # noqa: E402
from test_trace_render_labels import label_functions  # noqa: E402


@pytest.mark.parametrize("release_case", [False, True])
def test_short_statuses_keep_original_failures_and_never_claim_human_approval(tmp_path, release_case):
    options = {"suite": "cli-release", "scenario": "alias-set", "release_fixture_version": 6} if release_case else {}
    source, review, report = source_and_review(tmp_path, **options)
    board, result, original = exported(tmp_path, source)
    data = json.loads(original["raw.json"])
    data["assessments"] = publisher.previous_assessments(board, result["dataset"]["dataset_id"])
    if release_case:
        assessments.add_assessments(data, source, release_assessments=True)
    pending = json.loads(json.dumps(data))
    assessments.attach_review(data, source, review, report)
    passed = json.loads(json.dumps(data))
    finding = {"severity": "P2" if release_case else "P3", "blocking": release_case, "text": "Unsupported assurance."}
    correction = assessments.bind_review(
        source,
        review["target"],
        report,
        reviewer=review["reviewer"],
        status="findings",
        findings=[finding],
        supersedes=review["review_id"],
    )
    assessments.attach_review(data, source, correction, report)
    observed = run(
        label_functions()
        + function(VIEWER, "assessmentPanel")
        + function(INTERACTIONS, "buildCurrentBoard")
        + "\nconst variants="
        + json.dumps([pending, passed, data])
        + ";\n"
        + """
let data=variants[0];
function node(tag,text,cls){return {tag,text,cls,children:[],append(...items){this.children.push(...items);}};}
function contents(value){return [value.text,...value.children.flatMap(contents)].filter(Boolean);}
const texts=[],labels=[],tones=[];
for(const value of variants){data=value;const trial=Object.values(data.cases)[0][0];
 const before=JSON.stringify(data),badgeValue=badge(trial),panel=assessmentPanel(trial);
 labels.push(trialStatus(trial));tones.push(badgeValue.cls);
 texts.push(contents(panel));
 if(before!==JSON.stringify(data))throw new Error('display mutated trace identity');}
data=variants[1];const trial=Object.values(data.cases)[0][0];
const preserved=['fail','timeout','blocked'].map(state=>trialStatus({...trial,state}));
const original=JSON.stringify(variants),board=buildCurrentBoard(variants.map((value,index)=>({data:value,
 metadata:{dataset_id:String(index).repeat(64),title:'Recorded run'}})));
console.log(JSON.stringify({labels,tones,texts,preserved,rows:Object.values(board.cases)[0].length,
 unchanged:original===JSON.stringify(variants)}));
"""
    )
    assert observed["labels"] == ["待复核", "通过", "有问题" if release_case else "有建议"]
    assert observed["tones"] == ["badge blocked", "badge pass", "badge fail" if release_case else "badge pending"]
    assert observed["preserved"] == ["未通过", "超时", "受阻"]
    assert observed["rows"] == 1 and observed["unchanged"]
    assert all("人工确认：不需要" in value for value in observed["texts"])
    assert not any("人工审核通过" in text for values in observed["texts"] for text in values)
    assert any("Unsupported assurance." in text for text in observed["texts"][-1])
    assert ("原始导出状态：通过" if release_case else "原始导出状态：待确认") in observed["texts"][-1]
    if release_case:
        assert "模型复核：有问题（Astra）" in observed["texts"][-1]
    assert any(text.startswith("报告 SHA-256：") for text in observed["texts"][-1])
    assert any("请求模型 gpt-6-astra" in text for text in observed["texts"][-1])
