"""Selected case and model labels retain their own review scope and source."""

import json

import pytest
from test_trace_dataset_ui import INTERACTIONS, VIEWER, VUE, function, run


@pytest.mark.parametrize(
    "selection,other_count,expected_scope,expected_anchor,expected_subject,expected_model,source,index",
    [
        ("case-name", 1, "case", "case", "scenario", "", "b", None),
        ("case-row", 1, "case", "case", "scenario", "", "b", None),
        ("case-name", 2, "case", "case", "scenario", "", "a", None),
        ("case-title", 1, "case", "case", "scenario", "", "c", None),
        ("model-0", 2, "run", "run", "general", "Other 0", "b", 4),
        ("model-1", 2, "run", "run", "general", "Other 1", "a", 7),
        ("tool-output", 1, "run", "tool:0:0:0:output", "general", "Active 1", "c", 9),
        ("ui", 1, "run", "ui:chat-tab", "interface", "Active 1", "c", 9),
    ],
)
def test_selection_emits_exact_case_or_model_context_without_navigating(
    selection, other_count, expected_scope, expected_anchor, expected_subject, expected_model, source, index
):
    code = "\n".join(
        function(INTERACTIONS, name)
        for name in (
            "renderCaseNavigation",
            "reviewContext",
            "requestReview",
            "anchoredElement",
            "showSelectionComment",
            "submitSelectionComment",
        )
    )
    observed = run(
        code
        + function(VIEWER, "choice")
        + function(VUE, "validContext")
        + r"""
class Element {
 constructor(tag,text,cls){this.tag=tag;this.text=text;this.cls=cls;this.dataset={};this.children=[];
  this.attributes={};this.style={};this.classList={contains(){return false;}};this.nodeType=1;}
 append(...items){for(const item of items){this.children.push(item);item.parentElement=this;}}
 prepend(item){this.children.unshift(item);item.parentElement=this;}
 replaceChildren(...items){this.children=[];this.append(...items);}
 setAttribute(name,value){this.attributes[name]=value;}
 addEventListener(){}
 matches(){return false;}
 getRootNode(){return {};}
 closest(selector){
  const matches=element=>selector.split(',').some(part=>{
   const key=part.trim();
   return key==='[data-review-anchor]'&&element.dataset.reviewAnchor!=null ||
    key==='[data-review-case]'&&element.dataset.reviewCase!=null ||
    key==='[id]'&&!!element.id;
  });
  for(let value=this;value;value=value.parentElement)if(matches(value))return value;
  return null;
 }
}
function node(...args){return new Element(...args);}
const body=node('body'),nav=node('nav');nav.id='case-nav';body.append(nav);
const sidebar={scrollTop:32};
const document={body,querySelector(){return sidebar;}};
function select(){return nav;}
function icon(){return node('svg');}
function scenarioGroups(){return [['Group',['active','other']]];}
function caseName(key){return key==='other'?'Other case':'Active case';}
function badge(){return null;}
function modelLabel(trial){return trial.model;}
let navigations=0,activeCase='active',activeIndex=1;
function pickCase(){navigations++;}function pickRun(){navigations++;}
const expandedCases=new Set(['active','other']);
const data={cases:{active:[{model:'Active 0'},{model:'Active 1'}],
 other:Array.from({length:OTHER_COUNT},(_,i)=>({model:'Other '+i}))},
 review_runs:{active:['d'.repeat(64),'e'.repeat(64)],other:['f'.repeat(64),'1'.repeat(64)]},
 review_targets:{active:[{dataset_id:'c'.repeat(64),run_index:8},{dataset_id:'c'.repeat(64),run_index:9}],
 other:[{dataset_id:'b'.repeat(64),run_index:4},{dataset_id:'a'.repeat(64),run_index:7}]}};
const datasetId=null,online=true,imported=false,before=JSON.stringify(data);
const location={hash:'#case=active&run=1'},originalRoute=location.hash;
renderCaseNavigation();
const all=[];function visit(element){all.push(element);element.children.forEach(visit);}visit(nav);
const caseItem=all.find(element=>element.cls==='case-item'&&element.dataset.reviewCase==='other');
const runButtons=all.filter(element=>element.dataset.navKind==='run'&&element.dataset.reviewCase==='other');
let origin;
if(SELECTION==='case-name')origin=caseItem.children[0].children.find(element=>element.dataset.navKind==='case').children[0];
else if(SELECTION==='case-row')origin=caseItem.children[0];
else if(SELECTION.startsWith('model-'))origin=runButtons[Number(SELECTION.slice(-1))].children[0];
else {
 origin=node('div');body.append(origin);
 if(SELECTION==='case-title')origin.id='case-title';
 else if(SELECTION==='ui')origin.id='chat-tab';
 else {origin.dataset.reviewAnchor='tool:0:0:0:output';origin.dataset.reviewField='工具输出';}
}
const quote='Selected exact text: 中文';
const Node={ELEMENT_NODE:1},window={getSelection(){return {toString(){return quote;},
 anchorNode:SELECTION==='case-title'?origin:{nodeType:3,parentElement:origin},rangeCount:0};}};
const parent={},messages=[];function parentMessage(message){messages.push(message);}
const selectionComment=node('button');let capturedSelection;
const innerWidth=1200,innerHeight=900;
showSelectionComment({target:origin,composedPath(){return[origin];},clientX:50,clientY:60});
submitSelectionComment();
console.log(JSON.stringify({messages,valid:validContext(messages[0]?.context),
 unchanged:JSON.stringify(data)===before,routeUnchanged:location.hash===originalRoute,
 navigations,activeCase,activeIndex,hidden:selectionComment.hidden}));
""".replace("OTHER_COUNT", str(other_count)).replace("SELECTION", json.dumps(selection))
    )
    assert observed["valid"] and observed["unchanged"] and observed["routeUnchanged"]
    assert observed["navigations"] == 0
    assert observed["activeCase"] == "active" and observed["activeIndex"] == 1 and observed["hidden"]
    assert len(observed["messages"]) == 1
    message = observed["messages"][0]
    assert message["quote"] == "Selected exact text: 中文"
    assert message["anchor"] == expected_anchor and message["subject"] == expected_subject
    context = message["context"]
    assert context["scope"] == expected_scope and context["model"] == expected_model
    assert context["dataset_id"] == source * 64 and context["run_index"] == index
    assert context["case_id"] == ("active" if selection in {"case-title", "tool-output", "ui"} else "other")
    if expected_scope == "case":
        assert context["run_id"] is None
    else:
        assert context["run_id"] == ({"model-0": "f", "model-1": "1"}.get(selection, "e") * 64)

    # Validate the emitted comment against the real backend schema without saving it.
    from lockbot.backend.app.trace_review import ReviewComment

    parsed = ReviewComment.model_validate(
        {
            **{key: context[key] for key in ("case_id", "scope", "run_index", "run_id", "dataset_id")},
            "anchor": message["anchor"],
            "subject": message["subject"],
            "quote": message["quote"],
            "body": "Review",
        }
    )
    assert parsed.scope == expected_scope
