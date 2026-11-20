"""Exercise the viewer's actual lazy-download action with a browser API harness."""

import json
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "frontend/src/features/agent-traces/interactions.js"
VIEWER = SOURCE.with_name("viewer.html")


@pytest.mark.parametrize(
    "mode,downloaded",
    [
        ("success", True),
        ("http-context", True),
        ("unauthorized", False),
        ("bad-header", False),
        ("bad-body", False),
        ("bad-length", False),
        ("no-token", False),
        ("offline", False),
        ("logout", False),
        ("unsafe-name", False),
        ("oversized", False),
    ],
)
def test_download_is_lazy_authenticated_bounded_and_integrity_checked(mode, downloaded):
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const source = fs.readFileSync(SOURCE, 'utf8');
const start = source.indexOf('function diagnosticDownload(');
const end = source.indexOf('\nfunction runEfficiency(', start);
const body = Buffer.from('Complete native diagnostic\n');
const digest = crypto.createHash('sha256').update(body).digest('hex');
const downloads = [], requests = [], blobs = [];
let token = MODE === 'no-token' ? '' : 'synthetic-token';
function node(tag, text) {
 return {tag, textContent:text, children:[], append(...items){this.children.push(...items);},
         click(){downloads.push({filename:this.download,url:this.href});}, remove(){}};
}
const context = {node, online: MODE !== 'offline', imported:false, Blob, Uint8Array,
  crypto:MODE === 'http-context' ? undefined : crypto.webcrypto,
  localStorage:{getItem(){return token;}}, AbortSignal:{timeout(){return 'bounded';}},
  URL:{createObjectURL(blob){blobs.push(blob); return 'blob:private-diagnostic';}, revokeObjectURL(){}},
  document:{body:{append(){}}},
  async fetch(url, options){
    requests.push({url,options});
    if(MODE === 'logout') token = '';
    return {status:MODE === 'unauthorized' ? 403 : 200, ok:MODE !== 'unauthorized',
      headers:{get(){return MODE === 'bad-header' ? '0'.repeat(64) : digest;}},
      async arrayBuffer(){
        const content = MODE === 'bad-body' ? Buffer.alloc(body.length, 65) :
          MODE === 'bad-length' ? Buffer.from('x') : body;
        return Uint8Array.from(content).buffer;
      }};
  }};
vm.createContext(context);
vm.runInContext(source.slice(start,end), context);
const artifact = {sha256:digest, bytes:MODE === 'oversized' ? 17*1024*1024 : body.length,
                  filename:MODE === 'unsafe-name' ? '../secret' : 'codex-native.jsonl',
                  url:'https://untrusted.example/should-never-be-used'};
const section = context.diagnosticDownload(artifact);
const button = section.children.find(item => item.tag === 'button');
if(requests.length) throw new Error('Must not fetch before a click');
(async()=>{
 if(button) await button.onclick();
 if(requests.some(r=>r.url !== '/api/dev/trace-review/evidence/'+digest)) throw new Error('Unsafe URL');
 if(requests.some(r=>r.options.headers.Authorization !== 'Bearer synthetic-token' ||
   r.options.cache !== 'no-store' || r.options.redirect !== 'error')) throw new Error('Unsafe auth/cache');
 if(blobs.length && !Buffer.from(await blobs[0].arrayBuffer()).equals(body)) throw new Error('Bytes changed');
 console.log(JSON.stringify({downloads,requests:requests.length,
  status:section.children.map(item=>item.textContent).join(' ')}));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    script = "const SOURCE=" + json.dumps(str(SOURCE)) + ";const MODE=" + json.dumps(mode) + ";\n" + script
    result = subprocess.run(["node"], input=script, text=True, capture_output=True, check=True)
    observed = json.loads(result.stdout)
    assert bool(observed["downloads"]) is downloaded
    if mode in {"offline", "no-token", "unsafe-name", "oversized"}:
        assert observed["requests"] == 0
    if downloaded:
        assert observed["downloads"][0]["filename"] == "codex-native.jsonl"
    else:
        assert "已下载" not in observed["status"]


def test_tool_inspector_exposes_download_without_hydrating_or_mutating_trial():
    source = VIEWER.read_text()
    assert "if (info.output?.artifact) fields.push(diagnosticDownload(info.output.artifact));" in source
    assert "info.output =" not in SOURCE.read_text()
