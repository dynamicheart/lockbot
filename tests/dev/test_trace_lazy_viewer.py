"""Exercise the shipped lazy hydration JavaScript with controlled request completion."""

import json

import pytest
from test_trace_dataset_ui import VIEWER, function, run


def hydrate_program(body):
    source = VIEWER.read_text()
    start = source.index("  hydrateTrial = async (caseId,index) => {")
    end = source.index("\n  async function pollSnapshot()", start)
    hydrate = source[start:end]
    start = source.index("  function hideSnapshot() {")
    end = source.index("\n  function synchronizeToken()", start)
    hide = source[start:end]
    return run(
        function(VIEWER, "validRecord")
        + """
const assert=require('node:assert/strict');
const statusNames={pass:'pass',pending:'pending',timeout:'timeout',blocked:'blocked',fail:'fail'};
let token='operator-one',tokenRevision=0,imported=false,activeCase='one',activeIndex=0,hydrateTrial;
const datasetId=null,online=true,snapshotParams=new URLSearchParams();
const localStorage={getItem(){return token;}};
const trialCache=new Map(),pendingTrials=new Map(),requests=[],statuses=[];
const layout={hidden:false},inspector={open:false,close(){this.open=false;}};
let lastSnapshot='catalog',draws=0,drawHook=null;
const content={children:[],replaceChildren(...rows){this.children=rows;}};
function select(id){assert.equal(id,'content');return content;}
function node(tag,text,cls){return {tag,text,cls};}
function reportStatus(text,error){statuses.push({text,error});}
function draw(){draws++;if(drawHook)drawHook();}
function fetch(url,options){return new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));}
const lazy=model=>({model,state:'pass',sessions:[],lazy:true});
const complete=model=>({model,state:'pass',sessions:[[{role:'user',text:'Find compute'},
 {role:'assistant',text:'Available choices'}]]});
const sourceRows=Object.freeze([Object.freeze(lazy('M')),Object.freeze(lazy('N'))]);
let data={schema:1,cases:{one:sourceRows},review_runs:{one:['run-m','run-n']},
 review_targets:{one:[{dataset_id:'source-a',run_index:7,title:'A'},{dataset_id:'source-b',run_index:2,title:'B'}]}};
function payload(index,overrides={}){
 const target=data.review_targets.one[index];
 return {dataset_id:target.dataset_id,case_id:'one',run_index:target.run_index,
  run_id:data.review_runs.one[index],trial:complete(index?'N':'M'),...overrides};
}
function respond(request,value,status=200){request.resolve({ok:status===200,status,async json(){return value;}});}
"""
        + hide
        + hydrate
        + "\n(async()=>{\n"
        + body
        + "\nconsole.log(JSON.stringify({ok:true}));\n})().catch(error=>{console.error(error);process.exitCode=1;});"
    )


def test_hydration_uses_immutable_source_coordinates_and_preserves_catalog_rows():
    assert hydrate_program(
        """
const before=JSON.stringify(sourceRows),targetBefore=JSON.stringify(data.review_targets);
const work=hydrateTrial('one',0);
assert.equal(requests.length,1);
const request=requests[0],params=new URL(request.url,'https://example.test').searchParams;
assert.equal(params.get('dataset_id'),'source-a');assert.equal(params.get('run_index'),'7');
assert.equal(params.get('run_id'),'run-m');assert.equal(params.get('case_id'),'one');
assert.equal(request.options.headers.Authorization,'Bearer operator-one');
assert.equal(request.options.cache,'no-store');assert.equal(request.options.redirect,'error');
respond(request,payload(0));await work;
assert.equal(data.cases.one[0].lazy,undefined);assert.equal(draws,1);
assert.equal(JSON.stringify(sourceRows),before);assert.equal(JSON.stringify(data.review_targets),targetBefore);
assert.deepEqual(data.review_runs.one,['run-m','run-n']);assert.equal(pendingTrials.size,0);
"""
    ) == {"ok": True}


def test_switching_run_cannot_display_a_stale_response_and_reuses_cached_trial():
    assert hydrate_program(
        """
const first=hydrateTrial('one',0),firstPayload=payload(0);
activeIndex=1;const second=hydrateTrial('one',1),secondPayload=payload(1);
respond(requests[0],firstPayload);await first;
assert.equal(draws,0);assert.equal(data.cases.one[0].lazy,true);
respond(requests[1],secondPayload);await second;
assert.equal(draws,1);assert.equal(data.cases.one[1].model,'N');
activeIndex=0;await hydrateTrial('one',0);
assert.equal(requests.length,2);assert.equal(data.cases.one[0].model,'M');assert.equal(draws,2);
"""
    ) == {"ok": True}


def test_duplicate_pending_selection_issues_one_read():
    assert hydrate_program(
        """
const work=hydrateTrial('one',0);await hydrateTrial('one',0);
assert.equal(requests.length,1);respond(requests[0],payload(0));await work;
assert.equal(draws,1);assert.equal(pendingTrials.size,0);
"""
    ) == {"ok": True}


@pytest.mark.parametrize("status", [200, 401, 403])
def test_imported_local_trace_survives_pending_online_completion(status):
    assert hydrate_program(
        f"""
const work=hydrateTrial('one',0),answer=payload(0);
imported=true;const local={{schema:1,cases:{{one:[complete('local')]}}}};data=local;
respond(requests[0],answer,{status});await work;
assert.equal(data,local);assert.equal(layout.hidden,false);assert.equal(draws,0);
assert.equal(trialCache.size,0);assert.equal(statuses.length,0);
"""
    ) == {"ok": True}


@pytest.mark.parametrize("status", [200, 401, 403])
def test_token_change_rejects_old_response_and_old_auth_error(status):
    assert hydrate_program(
        f"""
const work=hydrateTrial('one',0),answer=payload(0);token='operator-two';
respond(requests[0],answer,{status});await work;
assert.equal(data.cases.one[0].lazy,true);assert.equal(layout.hidden,false);
assert.equal(draws,0);assert.equal(trialCache.size,0);assert.equal(statuses.length,0);
"""
    ) == {"ok": True}


@pytest.mark.parametrize("status", [401, 403])
def test_current_auth_denial_hides_online_content(status):
    assert hydrate_program(
        f"""
const work=hydrateTrial('one',0);respond(requests[0],null,{status});await work;
assert.equal(layout.hidden,true);assert.equal(statuses.length,1);assert.equal(statuses[0].error,true);
assert.equal(pendingTrials.size,0);
"""
    ) == {"ok": True}


@pytest.mark.parametrize("status", [200, 401, 403])
def test_old_hydration_cannot_survive_logout_and_login_with_the_same_token(status):
    assert hydrate_program(
        f"""
const work=hydrateTrial('one',0),answer=payload(0);
token=null;tokenRevision++;token='operator-one';tokenRevision++;
respond(requests[0],answer,{status});await work;
assert.equal(data.cases.one[0].lazy,true);assert.equal(draws,0);
assert.equal(trialCache.size,0);assert.equal(statuses.length,0);
"""
    ) == {"ok": True}


@pytest.mark.parametrize(
    "change",
    [
        {"dataset_id": "wrong-source"},
        {"run_index": 0},
        {"run_id": "wrong-run"},
        {"case_id": "wrong-case"},
        {"trial": {"model": "M", "sessions": [], "lazy": True}},
        {"trial": {"model": "M", "sessions": "invalid"}},
    ],
)
def test_mismatched_or_invalid_trial_is_not_rendered(change):
    assert hydrate_program(
        """
const work=hydrateTrial('one',0);
respond(requests[0],payload(0,"""
        + json.dumps(change)
        + """));await work;
assert.equal(data.cases.one[0].lazy,true);assert.equal(draws,0);
assert.equal(content.children[1].tag,'button');assert.equal(pendingTrials.size,0);
"""
    ) == {"ok": True}


def test_failed_read_retries_only_after_user_requests_retry():
    assert hydrate_program(
        """
const work=hydrateTrial('one',0);requests[0].reject(new Error('network'));await work;
assert.equal(requests.length,1);assert.equal(pendingTrials.size,0);
let retryWork;drawHook=()=>{retryWork=hydrateTrial('one',0);};
content.children[1].onclick();assert.equal(requests.length,2);
respond(requests[1],payload(0));drawHook=null;await retryWork;
assert.equal(data.cases.one[0].lazy,undefined);assert.equal(pendingTrials.size,0);
"""
    ) == {"ok": True}


def test_pending_source_coordinates_cannot_overwrite_replaced_catalog_selection():
    assert hydrate_program(
        """
const work=hydrateTrial('one',0),answer=payload(0);
data.review_targets.one[0]={dataset_id:'new-source',run_index:12,title:'new source'};
respond(requests[0],answer);await work;
assert.equal(data.cases.one[0].lazy,true);assert.equal(draws,0);
"""
    ) == {"ok": True}


def test_pending_selected_run_relocation_does_not_leave_loading_stranded():
    assert hydrate_program(
        """
const answer=payload(0),work=hydrateTrial('one',0);
// A new catalog changes merged display positions but keeps the original source run.
data={...data,cases:{one:[data.cases.one[1],data.cases.one[0]]},
 review_runs:{one:['run-n','run-m']},
 review_targets:{one:[data.review_targets.one[1],data.review_targets.one[0]]}};
activeIndex=1;
await hydrateTrial('one',1);assert.equal(requests.length,1);
drawHook=()=>{if(data.cases.one[activeIndex].lazy)void hydrateTrial('one',activeIndex);};
respond(requests[0],answer);await work;await new Promise(resolve=>setImmediate(resolve));
assert.equal(data.cases.one[activeIndex].lazy,undefined);
assert.equal(data.cases.one[activeIndex].model,'M');assert.ok(draws>0);
"""
    ) == {"ok": True}
