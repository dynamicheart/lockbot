"""Display-only trace polish preserves raw identity and honest verdict provenance."""

import re
from html.parser import HTMLParser

from test_trace_dataset_ui import INTERACTIONS, VIEWER, function, run


def label_functions():
    return "\n".join(function(VIEWER, name) for name in ("displayModelName", "modelLabel", "trialStatus", "badge"))


def test_readable_models_disambiguate_collisions_without_changing_raw_identities():
    observed = run(
        label_functions()
        + """
const statusNames={pass:'检查通过',fail:'检查未通过',pending:'待核对'};
const activeCase='one';
const data={cases:{one:[
 {model:'training13-codex · official:gpt-6.1-sol',source_run:'matrix:first'},
 {model:'GPT-6.1 Sol',source_run:'matrix:second'},
 {model:'training13-codex · official:gpt-6.1-sol · 阶段 2 语义复核',source_run:'semantic:exact'},
 {model:'ducc · MiniMax-M3'}, {model:'ducc · GLM-5.3'}, {model:'ducc · Kimi K3'},
 {model:'ducc · DeepSeek-V4.1-Flash'}, {model:'Unknown client · exact-model-id'}]}};
const before=JSON.stringify(data);
console.log(JSON.stringify({labels:data.cases.one.map((row,index)=>modelLabel(row,index)),unchanged:before===JSON.stringify(data)}));
"""
    )
    assert observed == {
        "labels": [
            "GPT-6.1 Sol · 记录 1",
            "GPT-6.1 Sol · 记录 2",
            "GPT-6.1 Sol · 复核 2",
            "MiniMax M3",
            "GLM 5.3",
            "Kimi K3",
            "DeepSeek V4.1 Flash",
            "Unknown client · exact-model-id",
        ],
        "unchanged": True,
    }


def test_sidebar_removes_ratios_and_preserves_all_record_indices_and_raw_tooltips():
    observed = run(
        label_functions()
        + function(INTERACTIONS, "renderCaseNavigation")
        + """
const statusNames={pass:'检查通过',pending:'待核对'};let activeCase='one',activeIndex=0;
const data={cases:{one:Array.from({length:10},(_,index)=>({model:'ducc · GLM-5.3',
 state:index<5?'pass':'pending',source_run:index<5?'matrix:'+index:'semantic:'+index,sessions:[]}))}};
const before=JSON.stringify(data),selected=[],roots=[];
const expandedCases=new Set(['one']),sidebar={scrollTop:15};
function node(tag,text,cls){return {tag,text,cls,children:[],dataset:{},append(...items){this.children.push(...items);},
 setAttribute(){}};}
function choice(text,meta,pressed,action){const value=node('button',text);if(meta)value.append(meta);
 value.onclick=action;return value;}
function select(){return {replaceChildren(...items){roots.push(...items);}};}
const document={querySelector(){return sidebar;}};
function scenarioGroups(){return [['Group',['one']]];}function caseName(){return 'Case';}
function icon(){return node('svg');}function pickCase(){}function pickRun(index){selected.push(index);}
renderCaseNavigation();
const nodes=[];function visit(element){nodes.push(element);for(const child of element.children)visit(child);}
roots.forEach(visit);
const buttons=nodes.filter(element=>element.dataset.navKind==='run');buttons[0].onclick();buttons[9].onclick();
console.log(JSON.stringify({texts:nodes.map(element=>element.text).filter(Boolean),selected,runs:buttons.length,
 titles:buttons.map(button=>button.title),unchanged:before===JSON.stringify(data),metas:nodes.filter(n=>n.cls==='meta').length}));
"""
    )
    assert observed["runs"] == 10
    assert observed["selected"] == [0, 9]
    assert observed["titles"] == ["ducc · GLM-5.3"] * 10
    assert observed["unchanged"] and observed["metas"] == 0
    assert not any(re.search(r"\d+/\d+", text) for text in observed["texts"])


def test_badges_distinguish_automatic_checks_from_independent_phase_reviews():
    observed = run(
        label_functions()
        + """
const statusNames={pass:'检查通过',fail:'检查未通过',pending:'待核对',timeout:'超时'};
function node(tag,text,cls){return {tag,text,cls};}
const automatic={state:'pass',source_run:'matrix:source',model:'same raw model'};
const semantic={state:'fail',semantic_status:'pass',overall_review_status:'fail',source_run:'semantic:source',
 model:'same raw model'};
console.log(JSON.stringify({automatic:badge(automatic),semantic:badge(semantic),
 uncertain:trialStatus({state:'pending',semantic_status:'uncertain',source_run:'semantic:other'}),
 reviewed:trialStatus({state:'pending',semantic_status:'pass',source_run:'semantic:pass'}),
 failure:trialStatus({state:'fail',source_run:'matrix:fail'}),unknown:trialStatus({state:'pass'})}));
"""
    )
    assert observed["automatic"]["text"] == "通过"
    assert "独立复核" in observed["automatic"]["title"]
    assert observed["semantic"]["text"] == "未通过"
    assert observed["uncertain"] == "待确认"
    assert observed["reviewed"] == "通过"
    assert observed["failure"] == "未通过"
    assert observed["unknown"] == "通过"


def test_compact_mobile_controls_keep_accessible_labels_and_existing_anchor_ids():
    class Elements(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = {}

        def handle_starttag(self, _tag, attrs):
            value = dict(attrs)
            if "id" in value:
                self.ids[value["id"]] = value

    elements = Elements()
    elements.feed(VIEWER.read_text())
    assert "dataset-select" not in elements.ids
    assert elements.ids["load-file"]["aria-label"] == "打开本地 JSON 轨迹文件"
    assert all(anchor in elements.ids for anchor in ("case-title", "run-title", "chat-tab", "event-tab", "case-nav"))
    css = VIEWER.with_name("viewer.css").read_text()
    mobile = css[css.index("@media (max-width: 720px)") :]
    assert ".brand-description, #dataset-caption, .control-full { display: none; }" in mobile
    assert "#dataset-select { width: clamp(112px, 34vw, 180px); max-width: 100%; }" in mobile
    assert ".brand { white-space: nowrap;" in css
