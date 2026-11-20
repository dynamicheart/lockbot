"""Partial retests replace only their model families and retain original comment anchors."""

from test_trace_dataset_ui import INTERACTIONS, VIEWER, function, run


def test_partial_refresh_preserves_other_models_cases_and_source_comment_coordinates():
    observed = run(
        function(INTERACTIONS, "buildCurrentBoard")
        + function(INTERACTIONS, "reviewContext")
        + """
const old={dataset_id:'a',title:'full',schema:1,visibility:'raw',cases:{
 one:[{model:'M',state:'pass'},{model:'N',state:'pass'},
 {model:'M · 阶段 1 语义复核',state:'pass'},{model:'M · 阶段 2 语义复核',state:'pass'}],
 untouched:[{model:'N',state:'pass'}]},review_runs:{one:['m','n','m1','m2'],untouched:['u']}};
const fresh={dataset_id:'b',schema:1,cases:{one:[{model:'M',state:'fail'},
 {model:'M · 阶段 1 语义复核',state:'fail'}],newcase:[{model:'M',state:'pass'}]},
 review_runs:{one:['new-m','new-m1'],newcase:['new-c']}};
const inputs=[{data:old,metadata:{dataset_id:'a',title:'full'}},
 {data:fresh,metadata:{dataset_id:'b',title:'partial (undeployed)'}}];
const before=JSON.stringify(inputs);
const data=buildCurrentBoard(inputs), activeCase='one',activeIndex=2;
const datasetId=null,online=true,imported=false;
function caseName(key){return key;}
console.log(JSON.stringify({cases:data.cases,ids:data.review_runs,targets:data.review_targets,
 updated:data.updated_cases,context:reviewContext(),caseContext:reviewContext('case'),
 untouched:data.cases.untouched===old.cases.untouched,unchanged:before===JSON.stringify(inputs)}));
"""
    )
    assert observed["ids"] == {"one": ["new-m", "new-m1", "n"], "untouched": ["u"], "newcase": ["new-c"]}
    assert observed["cases"]["one"][0]["state"] == "fail"  # Never prefer a green older result.
    assert observed["updated"] == ["one", "newcase"]
    assert observed["context"]["dataset_id"] == "a"
    assert observed["context"]["run_index"] == 1  # Source index, not merged display index 2.
    assert observed["context"]["run_id"] == "n"
    assert observed["caseContext"]["run_index"] is None
    assert observed["unchanged"]


def test_call_colors_use_recorded_outcomes_and_keep_diagnostics_neutral():
    observed = run(
        function(VIEWER, "isFailed")
        + function(INTERACTIONS, "callStatusDot")
        + """
function node(tag,text,cls){return {cls,setAttribute(){}};}
const step=(tool,status)=>[tool,'','',''+status];
console.log(JSON.stringify([
 callStatusDot(step('run_lockbot','已返回'),{output:{result:{}}}),
 callStatusDot(step('run_lockbot','已返回'),{output:{error:'http_429'}}),
 callStatusDot(step('run_lockbot','运行中'),{}),
 callStatusDot(step('run_lockbot',''),{}),
 callStatusDot(step('native_client_diagnostic','已返回'),{output:{artifact:{}}}),
 callStatusDot(step('run_lockbot','已返回'),{output:{exit_code:2}})
].map(dot=>dot.cls)));
"""
    )
    assert observed == [
        "call-status complete",
        "call-status failed",
        "call-status running",
        "call-status unknown",
        "call-status unknown",
        "call-status failed",
    ]
