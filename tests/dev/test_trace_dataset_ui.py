"""Run the actual viewer/Vue functions with browser APIs to verify dataset boundaries."""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIEWER = ROOT / "frontend/src/features/agent-traces/viewer.html"
INTERACTIONS = VIEWER.with_name("interactions.js")
VUE = ROOT / "frontend/src/views/AgentTraces.vue"


def function(path, name):
    return re.search(r"(?:async )?function " + name + r"\([^\n]*\) \{.*?\n\}", path.read_text(), re.S)[0]


def run(script):
    result = subprocess.run(["node"], input=script, text=True, capture_output=True, check=True, timeout=15)
    return json.loads(result.stdout)


def test_routes_keep_numeric_trial_index_and_independent_validated_dataset():
    code = function(VUE, "routeFields") + function(VUE, "validContext")
    observed = run(
        code
        + """
const id='a'.repeat(64);
const context={case_id:'one',case_name:'One',model:'Model',scope:'run',run_index:4,run_id:'b'.repeat(64)};
console.log(JSON.stringify({route:routeFields({dataset_id:id,case:'one',run:'4',view:'steps',step:'2'}),
 invalid:routeFields({dataset_id:'../secret',run:id}),legacy:validContext(context),
 selected:validContext({...context,dataset_id:id}),forged:validContext({...context,dataset_id:'../secret'})}));
"""
    )
    assert observed == {
        "route": {"case": "one", "run": "4", "step": "2", "view": "steps", "dataset_id": "a" * 64},
        "invalid": {},
        "legacy": True,
        "selected": True,
        "forged": False,
    }


def test_viewer_links_and_review_context_pin_the_selected_dataset_without_changing_trials():
    observed = run(
        function(VIEWER, "writeRoute")
        + function(INTERACTIONS, "reviewContext")
        + """
const datasetId='a'.repeat(64), legacyHistory=false, online=true, imported=false;
const activeCase='one', activeIndex=3, activeTab='events', activeStep=2,userNavigation=false;
const location={hash:''};
const data={cases:{one:[{model:'x'},{model:'x'},{model:'x'},{model:'chosen'}]},
 review_runs:{one:['a','b','c','bound-id']}};
const before=JSON.stringify(data), urls=[];
const history={replaceState(a,b,url){urls.push(url);}};
function notifyReviewParent(){} function caseName(){return 'One';}
writeRoute();
console.log(JSON.stringify({urls,run:reviewContext(),global:reviewContext('case'),unchanged:before===JSON.stringify(data)}));
"""
    )
    assert "dataset_id=" + "a" * 64 in observed["urls"][0]
    assert "run=3" in observed["urls"][0] and "step=2" in observed["urls"][0]
    assert observed["run"]["dataset_id"] == "a" * 64
    assert observed["run"]["run_id"] == "bound-id"
    assert observed["global"]["run_id"] is None
    assert observed["global"]["dataset_id"] == "a" * 64  # Validation context for a new-only case.
    assert observed["unchanged"]


def test_current_catalog_selects_latest_comprehensive_base_and_subsequent_retests():
    observed = run(
        function(VIEWER, "loadDatasets")
        + """
const requests=[];
const AbortSignal={timeout(){return 6000;}};
async function fetch(url,options){requests.push({url,options});return {ok:true,async json(){return {datasets:[
 {dataset_id:'a'.repeat(64),title:'old full',case_count:83},
 {dataset_id:'b'.repeat(64),title:'current full',case_count:83},
 {dataset_id:'c'.repeat(64),title:'partial retest',case_count:9}]};}};}
(async()=>{console.log(JSON.stringify({catalog:await loadDatasets('private'),requests}));})();
"""
    )
    assert [row["dataset_id"] for row in observed["catalog"]["datasets"]] == ["b" * 64, "c" * 64]
    request = observed["requests"][0]
    assert request["options"]["headers"] == {"Authorization": "Bearer private"}
    assert request["options"]["cache"] == "no-store" and request["options"]["redirect"] == "error"
    assert "dataset-select" not in VIEWER.read_text().split("</head>", 1)[1]


def test_pending_viewer_navigation_does_not_echo_an_older_parent_route():
    observed = run(
        function(VUE, "synchronize")
        + function(VUE, "routeFields")
        + """
const sent=[];
function send(message){sent.push(message);}
const document={documentElement:{classList:{contains(){return false;}}}};
const reviewOpen={value:false},route={query:{case:'one',run:'8'}};
let requestedRoute='newer selection';
synchronize();
const during=sent.map(message=>message.type);sent.length=0;
requestedRoute='';route.query.run='2';synchronize();
console.log(JSON.stringify({during,settled:sent}));
"""
    )
    assert "lockbot-trace-set-route" not in observed["during"]
    assert observed["settled"][-1]["route"] == {"case": "one", "run": "2"}


def test_comments_carry_dataset_validation_and_namespace_while_global_patch_stays_global():
    script = (
        function(VUE, "loadComments")
        + function(VUE, "save")
        + function(VUE, "resolve")
        + """
function authIdentity(){return 'same-user';}
const id='a'.repeat(64), requests=[];
const draftContext={value:{dataset_id:id,case_id:'new-only',scope:'case',
 run_index:null,run_id:null,server_backed:true,anchor:'case',quote:''}};
const current={value:draftContext.value}, displayContext=current;
const draft={value:'Review'}, reviewSubject={value:'general'};
const saving={value:false}, saved={value:false}, saveError={value:''}, loadError={value:''}, comments={value:[]};
let requestGeneration=0;
const api={async get(url,options){requests.push({method:'GET',url,options});return {data:{comments:[]}};},
 async post(url,body,options){requests.push({method:'POST',url,body,options});},
 async patch(url,body,options){requests.push({method:'PATCH',url,body,options});}};
(async()=>{await save();await resolve({id:'global',status:'open'});
 await resolve({id:'trial',status:'open',dataset_id:id});
 console.log(JSON.stringify(requests));})()
"""
    )
    observed = run(script)
    assert observed[0]["body"]["dataset_id"] == "a" * 64
    assert observed[0]["body"]["scope"] == "case"
    assert observed[1]["options"]["params"]["dataset_id"] == "a" * 64
    patches = [event for event in observed if event["method"] == "PATCH"]
    assert patches[0]["options"]["params"] == {}
    assert patches[1]["options"]["params"]["dataset_id"] == "a" * 64


def test_switching_dataset_cannot_reuse_a_pending_comment_draft():
    code = "\n".join(function(VUE, name) for name in ("receive", "validContext"))
    observed = run(
        code
        + """
const location={origin:'https://example.test'}, frame={};const viewer={value:{contentWindow:frame}};
const old={case_id:'one',case_name:'One',model:'Model',scope:'run',
 run_index:0,run_id:'c'.repeat(64),dataset_id:'a'.repeat(64),anchor:'run',quote:''};
const reselecting={value:false};
const draft={value:'Unsaved text'},draftContext={value:old},reviewOpen={value:false},saveError={value:''};
receive({origin:location.origin,source:frame,data:{type:'lockbot-trace-comment',context:{...old,dataset_id:'b'.repeat(64)},anchor:'run',quote:''}});
console.log(JSON.stringify({text:draft.value,dataset:draftContext.value.dataset_id,error:saveError.value,open:reviewOpen.value}));
"""
    )
    assert observed["text"] == "Unsaved text" and observed["dataset"] == "a" * 64
    assert observed["error"] and observed["open"]


def test_embedded_selector_updates_parent_route_before_reloading_the_iframe():
    observed = run(
        function(VIEWER, "switchDatasetRoute")
        + """
const messages=[],urls=[];let reloads=0;
const window={},parent={postMessage(message,origin){messages.push({message,origin});}};
const location={origin:'https://example.test',reload(){reloads++;}};
const history={replaceState(a,b,url){urls.push(url);}};
const route=new URLSearchParams({dataset_id:'b'.repeat(64),case:'one',run:'3',step:'2'});
switchDatasetRoute(route);
const before={messages:messages.length,reloads,urls:urls.length};
switchDatasetRoute(route,false);
console.log(JSON.stringify({before,messages,urls,reloads}));
"""
    )
    assert observed["before"] == {"messages": 1, "reloads": 0, "urls": 0}
    assert observed["messages"][0]["message"] == {
        "type": "lockbot-trace-select-dataset",
        "route": {"dataset_id": "b" * 64, "case": "one", "run": "3", "step": "2"},
    }
    assert observed["reloads"] == 1
    assert "dataset_id=" + "b" * 64 in observed["urls"][0]
    assert "run=3" in observed["urls"][0]
