"""Cross-tab authentication changes refresh promptly without reviving old reads."""

import re
import textwrap

import pytest
from test_trace_dataset_ui import VIEWER, function, run


def auth_program(body):
    source = VIEWER.read_text()
    names = ("hideSnapshot", "synchronizeToken", "authStorageChanged", "pollSnapshot")
    code = "\n".join(
        textwrap.dedent(re.search(r"  (?:async )?function " + name + r"\([^\n]*\) \{.*?\n  \}", source, re.S)[0])
        for name in names
    )
    return run(
        function(VIEWER, "validRecord")
        + code
        + """
const assert=require('node:assert/strict');
const statusNames={pass:'pass',pending:'pending',timeout:'timeout',blocked:'blocked',fail:'fail'};
let token=null,currentToken=null,tokenRevision=0,imported=false,reviewBusy=false,selected=false;
const localStorage={getItem(){return token;}},location={origin:'https://example.test',href:'https://example.test/dev/trace-review'};
const layout={hidden:true},inspector={open:false,close(){this.open=false;closes++;}};
let closes=0,lastSnapshot='',activeCase='one',activeIndex=0,cases=[],data={},draws=0;
const snapshotCache=new Map(),trialCache=new Map(),requests=[],statuses=[];
const datasetId=null,raw=true,snapshotParams=new URLSearchParams(),snapshotUrl='/catalog';
const window={getSelection(){return {isCollapsed:!selected};}};
const liveStatus={hidden:false};function select(){return liveStatus;}
function reportStatus(text,error){statuses.push({text,error});}
function draw(){draws++;}function readRoute(){activeCase='one';activeIndex=0;}
function loadDatasets(){return Promise.resolve({datasets:[]});}
function fetch(url,options){return new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));}
const snapshot=model=>({schema:1,visibility:'raw',cases:{one:[{model,state:'pass',sessions:[]} ]}});
function respond(request,model,status=200){
 request.resolve({ok:status===200,status,async text(){return JSON.stringify(snapshot(model));}});
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const event=(extra={})=>({key:'lockbot_token',storageArea:localStorage,url:location.href,...extra});
"""
        + "\n(async()=>{\n"
        + body
        + "\nconsole.log(JSON.stringify({ok:true}));\n})().catch(error=>{console.error(error);process.exitCode=1;});"
    )


def test_cross_tab_login_reads_immediately_and_logout_hides_busy_selected_content():
    assert auth_program(
        """
token='operator-one';selected=true;reviewBusy=true;
authStorageChanged(event());await tick();assert.equal(requests.length,1);
assert.equal(requests[0].options.headers.Authorization,'Bearer operator-one');
respond(requests[0],'one');await tick();assert.equal(draws,1);assert.equal(layout.hidden,false);
authStorageChanged(event());await tick();assert.equal(requests.length,1);
inspector.open=true;trialCache.set('old','secret');snapshotCache.set('old','secret');
token=null;authStorageChanged(event({key:null}));
assert.equal(layout.hidden,true);assert.equal(inspector.open,false);assert.equal(closes,1);
assert.equal(trialCache.size,0);assert.equal(snapshotCache.size,0);assert.equal(lastSnapshot,'');
assert.ok(statuses.at(-1).text.includes('请先登录'));assert.equal(requests.length,1);
token='operator-two';authStorageChanged(event());await tick();assert.equal(requests.length,2);
respond(requests[1],'two');await tick();assert.equal(draws,2);assert.equal(data.cases.one[0].model,'two');
"""
    ) == {"ok": True}


@pytest.mark.parametrize("result", ["success", "denied", "network-error"])
def test_late_read_cannot_restore_snapshot_after_logout_and_token_reuse(result):
    completion = {
        "success": "respond(requests[0],'old');",
        "denied": "respond(requests[0],'old',401);",
        "network-error": "requests[0].reject(new Error('old network error'));",
    }[result]
    assert auth_program(
        """
token='same-token';const first=pollSnapshot();await tick();assert.equal(requests.length,1);
token=null;authStorageChanged(event());token='same-token';authStorageChanged(event());
await tick();assert.equal(requests.length,2);
respond(requests[1],'current');await tick();assert.equal(layout.hidden,false);assert.equal(draws,1);
const before=JSON.stringify(statuses);
"""
        + completion
        + """
await first;await tick();assert.equal(draws,1);assert.equal(layout.hidden,false);
assert.equal(data.cases.one[0].model,'current');assert.equal(JSON.stringify(statuses),before);
"""
    ) == {"ok": True}


def test_storage_payloads_unrelated_events_and_local_imports_do_not_create_access():
    assert auth_program(
        """
authStorageChanged(event({newValue:'forged-token'}));await tick();assert.equal(requests.length,0);
token='actual-store';authStorageChanged(event({key:'lockbot_locale'}));
authStorageChanged(event({storageArea:{}}));
authStorageChanged(event({url:'https://other.example/'}));await tick();assert.equal(requests.length,0);
imported=true;layout.hidden=false;lastSnapshot='local-file';
authStorageChanged(event());await pollSnapshot();
assert.equal(layout.hidden,false);assert.equal(lastSnapshot,'local-file');assert.equal(requests.length,0);
"""
    ) == {"ok": True}


def test_storage_notification_without_an_area_still_reads_only_the_current_store():
    assert auth_program(
        """
token='actual-store';authStorageChanged(event({storageArea:null,newValue:'untrusted-payload'}));
await tick();assert.equal(requests.length,1);
assert.equal(requests[0].options.headers.Authorization,'Bearer actual-store');
respond(requests[0],'current');await tick();assert.equal(draws,1);
"""
    ) == {"ok": True}
