"""Shared interface feedback locates current UI without weakening trial namespaces."""

import json

import pytest
from test_trace_dataset_ui import INTERACTIONS, VUE, function, run


def test_shared_ui_comments_are_separate_from_current_and_historical_trial_comments():
    source = VUE.read_text()
    start, end = source.index("function isUiComment("), source.index("async function save()")
    observed = run(
        source[start:end]
        + """
function computed(getter){return {get value(){return getter();}};}
const current={value:{case_id:'one',dataset_id:'batch-b',run_id:'run-b'}};
const showResolved={value:false};
const comments={value:[
 {id:'shared-ui-a',case_id:'one',scope:'run',anchor:'ui:chat-tab',run_id:'run-a',raw:true},
 {id:'shared-ui-case',case_id:'one',scope:'case',anchor:'ui:counts',raw:true},
 {id:'resolved-ui',case_id:'one',scope:'run',anchor:'ui:counts',status:'resolved',raw:true},
 {id:'other-case-ui',case_id:'other',scope:'run',anchor:'ui:counts',raw:true},
 {id:'case',case_id:'one',scope:'case',anchor:'case'},
 {id:'current-b',case_id:'one',scope:'run',anchor:'run',run_id:'run-b',dataset_id:'batch-b',raw:true},
 {id:'old-b',case_id:'one',scope:'run',anchor:'message:0:0:text',run_id:'older-b',dataset_id:'batch-b',raw:true},
 {id:'trial-a',case_id:'one',scope:'run',anchor:'tool:0:0:0:input',run_id:'run-a',dataset_id:'batch-a',raw:true},
 {id:'legacy',case_id:'one',scope:'run',anchor:'run',run_id:'legacy',raw:true},
]};
const ids=group=>group.value.map(comment=>comment.id);
const selected={ui:ids(uiComments),cases:ids(caseComments),
 current:ids(currentComments),history:ids(historicalComments)};
current.value={case_id:'one',run_id:'legacy'};
console.log(JSON.stringify({selected,legacy:{ui:ids(uiComments),current:ids(currentComments),history:ids(historicalComments)}}));
"""
    )
    assert observed["selected"] == {
        "ui": ["shared-ui-a", "shared-ui-case"],
        "cases": ["case"],
        "current": ["current-b"],
        "history": ["old-b"],
    }
    assert observed["legacy"] == {
        "ui": ["shared-ui-a", "shared-ui-case"],
        "current": ["legacy"],
        "history": [],
    }
    assert "界面评论 · 所有批次共用" in source


@pytest.mark.parametrize(
    "anchor,run_id,dataset,target,tab,tools",
    [
        ("ui:chat-tab", "origin-a", None, "chat-tab", "events", 0),
        ("ui:payload-tab", None, None, "payload", "events", 0),
        ("ui:not-a-field", "current-b", "batch-b", None, "events", 0),
        ("ui:case-nav", None, None, None, "events", 0),  # Missing UI anchor never falls back to a trial.
        ("message:0:0:text", "origin-a", None, None, "events", 0),
        ("message:0:0:text", "current-b", "batch-a", None, "events", 0),
        ("message:0:0:text", "current-b", "batch-b", "message", "chat", 0),
        ("tool:0:0:0:input", "current-b", "batch-a", None, "events", 0),
        ("tool:0:0:0:input", "current-b", "batch-b", "case-title", "events", 1),
    ],
)
def test_locator_only_ignores_trial_identity_for_known_current_ui(anchor, run_id, dataset, target, tab, tools):
    message = {"anchor": anchor, "run_id": run_id, "dataset_id": dataset}
    observed = run(
        function(INTERACTIONS, "locateReview")
        + """
const datasetId='batch-b',activeCase='one',activeIndex=2,scrolled=[],opened=[];
let activeTab='events',draws=0;
function element(id,anchor){return {dataset:{reviewAnchor:anchor},closest(){return null;},
 scrollIntoView(){scrolled.push(id);},classList:{add(){},remove(){}}};}
const elements={'chat-tab':element('chat-tab'), 'case-title':element('case-title')};
const document={querySelectorAll(){return [element('payload','ui:payload-tab'),
 element('message','message:0:0:text')];}};
function select(id){return elements[id];} function reviewContext(){return {run_id:'current-b',dataset_id:'batch-b'};}
function draw(){draws++;} function setTimeout(){} function openStep(){opened.push(true);}
const data={cases:{one:[{}, {}, {sessions:[[{role:'tools',steps:[['actual tool']],evidence:[{}]}]]}]}};
locateReview(MESSAGE);
console.log(JSON.stringify({scrolled,tab:activeTab,tools:opened.length,draws,activeIndex}));
""".replace("MESSAGE", json.dumps(message))
    )
    assert observed["scrolled"] == ([target] if target else [])
    assert observed["tab"] == tab
    assert observed["tools"] == tools
    assert observed["activeIndex"] == 2
    assert observed["draws"] == int(tab == "chat")


def test_shared_ui_locator_message_does_not_carry_origin_trial_identity():
    observed = run(
        function(VUE, "isUiComment")
        + function(VUE, "locate")
        + """
const reviewOpen={value:true},sent=[];function send(message){sent.push(message);}
locate({anchor:'ui:chat-tab',run_id:'origin-a'});
locate({anchor:'message:0:0:text',run_id:'origin-a',dataset_id:'batch-a'});
console.log(JSON.stringify(sent));
"""
    )
    assert observed == [
        {"type": "lockbot-trace-locate", "anchor": "ui:chat-tab"},
        {"type": "lockbot-trace-locate", "anchor": "message:0:0:text", "run_id": "origin-a", "dataset_id": "batch-a"},
    ]


def test_new_ui_selection_retains_exact_anchor_and_is_validated_with_current_trial():
    observed = run(
        function(INTERACTIONS, "requestReview")
        + """
const frame={},parent=frame,window={},messages=[],activeCase='one';
function reviewContext(scope){return {scope,run_id:'current-b',dataset_id:'batch-b'};}
function parentMessage(message){messages.push(message);}
requestReview('ui:chat-tab','Selected text','选中文字');
console.log(JSON.stringify(messages));
"""
    )
    assert observed[0]["anchor"] == "ui:chat-tab"
    assert observed[0]["subject"] == "interface"
    assert observed[0]["context"] == {"scope": "run", "run_id": "current-b", "dataset_id": "batch-b"}
