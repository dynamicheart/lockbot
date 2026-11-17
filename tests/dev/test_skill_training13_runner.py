"""Offline native subprocess tests; never contact SSH or a model provider."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "scripts/dev/skill_training13_runner.py"
spec = importlib.util.spec_from_file_location("training13_runner", SOURCE)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

CONNECTION = {
    "host": "container.example.test",
    "user": "eval",
    "port": 8890,
    "identity_file": "/private/key",
    "known_hosts": "/private/known_hosts",
}

NATIVE = r"""
import json,sys,time
from pathlib import Path
mode=sys.argv[1]
store=Path(sys.argv[2])
def send(m):
 print(json.dumps(m),flush=True)
def reply(m,result):
 send({'id':m['id'],'result':result})
def complete():
 send({'method':'thread/tokenUsage/updated','params':{'threadId':tid,'tokenUsage':{
  'last':{'inputTokens':7,'outputTokens':3},
  'total':{'inputTokens':14 if mode=='context' else 7,'outputTokens':6 if mode=='context' else 3}}}})
 send({'method':'item/completed','params':{'threadId':tid,'item':{'type':'agentMessage','text':'Done.'}}})
 send({'method':'turn/completed','params':{'threadId':tid,'turn':{'id':'turn2' if mode=='context' else 'turn1',
  'status':'completed'}}})
for line in sys.stdin:
 m=json.loads(line);method=m.get('method');p=m.get('params',{})
 if method=='initialize':
  assert p['capabilities']['experimentalApi'] is True
  reply(m,{'userAgent':'codex-cli/mock'})
 elif method=='thread/read':
  assert 'history' not in p
  reply(m,{'thread':json.loads(store.read_text())})
 elif method in ('thread/start','thread/fork'):
  assert p['sandbox']=='read-only' and p['approvalPolicy']=='never'
  assert p['baseInstructions']=='Fixture system.'
  assert p['modelProvider']=='skill_eval_mixin'
  tid='fork2' if method=='thread/fork' else 'thread1'
  thread={'id':tid}
  if method=='thread/start':
   assert p['environments']==[]
   assert p['allowProviderModelFallback'] is False
   assert p['dynamicTools'][0]['name']=='read_fixture'
  else:
   assert p['threadId']=='thread1' and p['lastTurnId']=='turn1'
   assert 'history' not in p
   thread.update(json.loads(store.read_text()))
   thread.update(id=tid,forkedFromId='thread1')
  reply(m,{'thread':thread,'model':'other' if mode=='model-mismatch' else p['model'],
   'modelProvider':p['modelProvider'],'instructionSources':[]})
 elif method=='turn/start':
  prompt=p['input'][0]['text']
  assert prompt==('Follow up.' if mode=='context' else 'Original prompt.')
  assert p['environments']==[]
  turn='turn2' if mode=='context' else 'turn1'
  # Exercise notifications delivered before the turn/start response.
  send({'method':'item/agentMessage/delta','params':{'threadId':tid,
   'delta':'X'*100 if mode=='output-limit' else 'Done.'}})
  reply(m,{'turn':{'id':turn}})
  if mode=='timeout':time.sleep(20)
  elif mode=='builtin-mcp':
   send({'method':'item/started','params':{'threadId':tid,'item':{'type':'mcpToolCall',
    'tool':'list_mcp_resources','arguments':{}}}})
  elif mode=='failed':
   send({'method':'turn/completed','params':{'threadId':tid,
    'turn':{'id':turn,'status':'failed','error':{'message':'synthetic provider failure'}}}})
  else:send({'id':'tool1','method':'item/tool/call','params':{'threadId':tid,'turnId':turn,
   'callId':'call1','tool':'forbidden' if mode=='forbidden' else 'read_fixture','arguments':{'path':'subject.md'}}})
 elif m.get('id')=='tool1':
  assert m['result']['success'] is True
  result=json.loads(m['result']['contentItems'][0]['text'])
  assert result['structuredContent']=={'fixture':True}
  if mode!='context':
   store.write_text(json.dumps({'id':'thread1','turns':[{'id':'turn1','status':'completed','items':[{'type':'userMessage','content':[{'type':'text','text':prompt}]},{'type':'agentMessage','text':'Done.'}]}]}))
  complete()
 elif method=='turn/interrupt':
  store.with_suffix('.interrupted').write_text('yes')
  reply(m,{})
"""

FIXTURE = r"""
import json,sys
for line in sys.stdin:
 m=json.loads(line)
 method=m.get('method')
 if 'id' not in m:continue
 if method=='initialize':r={}
 elif method=='tools/list':r={'tools':[{'name':'read_fixture','description':'Read fixture',
  'inputSchema':{'type':'object','properties':{'path':{'type':'string'}}}}]}
 elif method=='tools/call':
  assert m['params']=={'name':'read_fixture','arguments':{'path':'subject.md'}}
  r={'content':[{'type':'text','text':'synthetic body'}],'structuredContent':{'fixture':True}}
 else:raise AssertionError(method)
 print(json.dumps({'id':m['id'],'result':r}),flush=True)
"""


def case(tmp_path, phase="phase-1"):
    root = tmp_path / phase
    root.mkdir()
    (root / "prompt.txt").write_text("Original prompt." if phase == "phase-1" else "Follow up.")
    (root / "system.txt").write_text("Fixture system.")
    fixture = tmp_path / "fixture.py"
    fixture.write_text(FIXTURE)
    (root / "mcp.json").write_text(
        json.dumps({"mcpServers": {"fixture": {"command": sys.executable, "args": [str(fixture)]}}})
    )
    return root, {
        "model": "official:gpt-test",
        "prompt_file": str(root / "prompt.txt"),
        "system_file": str(root / "system.txt"),
        "mcp_config": str(root / "mcp.json"),
        "max_turns": 4,
        "output_tokens": 256,
    }


def native(monkeypatch, tmp_path, mode="normal"):
    script = tmp_path / "native.py"
    script.write_text(NATIVE)
    monkeypatch.setattr(
        runner,
        "connection_argv",
        lambda c, h: [sys.executable, str(script), mode, str(tmp_path / "native-thread.json")],
    )


def test_real_subprocess_bridge_retains_trace_and_structured_mcp_result(tmp_path, monkeypatch):
    root, request = case(tmp_path)
    native(monkeypatch, tmp_path)
    response = runner.run(request, CONNECTION, 5)
    assert response["text"] == "Done."
    assert response["model"] == "official:gpt-test"
    assert response["transport_model"] == "gpt-test"
    assert response["model_source"] == "official"
    assert response["turns"] is None
    assert "conversation_sha256" not in response
    metadata = json.loads((root / "native-session.json").read_text())
    assert metadata["thread_id"] == "thread1"
    assert metadata["dynamic_tool_calls"] == 1
    assert metadata["limits"]["hidden_reasoning_hard_cap"] is False
    trace = (root / "codex-native.jsonl").read_text()
    assert "structuredContent" in trace and "thread/start" in trace
    for file in root.glob("*native*"):
        assert file.stat().st_mode & 0o077 == 0


def test_context_uses_verified_native_fork_without_importing_history(tmp_path, monkeypatch):
    root, request = case(tmp_path)
    native(monkeypatch, tmp_path)
    response = runner.run(request, CONNECTION, 5)
    (root / "response.json").write_text(json.dumps(response))
    second, follow = case(tmp_path, "phase-2")
    history = [{"role": "user", "content": "Original prompt."}, {"role": "assistant", "content": "Done."}]
    conversation = second / "conversation.json"
    conversation.write_text(json.dumps(history))
    follow.update(conversation_file=str(conversation), conversation_sha256=runner.digest(conversation.read_bytes()))
    native(monkeypatch, tmp_path, "context")
    answer = runner.run(follow, CONNECTION, 5)
    assert answer["native_thread_id"] == "fork2"
    assert answer["usage"] == {"input_tokens": 7, "output_tokens": 3}
    assert answer["conversation_sha256"] == follow["conversation_sha256"]
    messages = [json.loads(line)["message"] for line in (second / "codex-native.jsonl").read_text().splitlines()]
    assert any(message.get("method") == "thread/fork" for message in messages)
    assert not any("history" in message.get("params", {}) for message in messages)
    turn = next(message for message in messages if message.get("method") == "turn/start")
    assert turn["params"]["input"] == [{"type": "text", "text": "Follow up."}]


@pytest.mark.parametrize("tamper", ["digest", "history", "metadata", "native-history"])
def test_context_rejects_tampering_before_new_turn(tmp_path, monkeypatch, tamper):
    first, request = case(tmp_path)
    native(monkeypatch, tmp_path)
    result = runner.run(request, CONNECTION, 5)
    (first / "response.json").write_text(json.dumps(result))
    second, follow = case(tmp_path, "phase-2")
    history = [{"role": "user", "content": "Original prompt."}, {"role": "assistant", "content": "Done."}]
    if tamper == "history":
        history[0]["content"] = "Forged"
    conversation = second / "conversation.json"
    conversation.write_text(json.dumps(history))
    follow.update(conversation_file=str(conversation), conversation_sha256=runner.digest(conversation.read_bytes()))
    if tamper == "digest":
        follow["conversation_sha256"] = "bad"
    if tamper == "metadata":
        metadata = json.loads((first / "native-session.json").read_text())
        metadata["model"] = "other"
        (first / "native-session.json").write_text(json.dumps(metadata))
    if tamper == "native-history":
        path = tmp_path / "native-thread.json"
        data = json.loads(path.read_text())
        data["turns"][0]["items"][1]["text"] = "Forged native response"
        path.write_text(json.dumps(data))
    native(monkeypatch, tmp_path, "context")
    with pytest.raises(runner.RunnerError, match="mismatch|match|differ") as error:
        runner.run(follow, CONNECTION, 5)
    assert error.value.kind == "unsupported_context"
    trace = second / "codex-native.jsonl"
    assert not trace.exists() or '"method": "turn/start"' not in trace.read_text()


@pytest.mark.parametrize(
    "mode,kind",
    [
        ("forbidden", "native_isolation_failure"),
        ("builtin-mcp", "native_isolation_failure"),
        ("failed", "native_turn_failed"),
        ("model-mismatch", "native_model_mismatch"),
        ("output-limit", "error_output_limit"),
        ("timeout", "timeout"),
    ],
)
def test_native_failures_never_become_success(tmp_path, monkeypatch, mode, kind):
    root, request = case(tmp_path)
    if mode == "output-limit":
        request["output_tokens"] = 10
    native(monkeypatch, tmp_path, mode)
    with pytest.raises(runner.RunnerError) as error:
        runner.run(request, CONNECTION, 0.2 if mode == "timeout" else 5)
    assert error.value.kind == kind
    assert not (root / "native-session.json").exists()
    if mode != "model-mismatch":
        assert '"method": "turn/interrupt"' in (root / "codex-native.jsonl").read_text()
    if mode == "builtin-mcp":
        assert '"method": "tools/call"' not in (root / "fixture-native.jsonl").read_text()


def test_turn_budget_stops_before_fixture_tool_execution(tmp_path, monkeypatch):
    root, request = case(tmp_path)
    request["max_turns"] = 1
    native(monkeypatch, tmp_path)
    with pytest.raises(runner.RunnerError) as error:
        runner.run(request, CONNECTION, 5)
    assert error.value.kind == "error_max_turns"
    assert '"method": "tools/call"' not in (root / "fixture-native.jsonl").read_text()


def test_ssh_command_uses_only_isolated_authless_loopback_provider():
    argv = runner.connection_argv(CONNECTION, runner.REMOTE_PREFIX + "abc123")
    assert "StrictHostKeyChecking=yes" in argv
    assert "BatchMode=yes" in argv
    assert "GlobalKnownHostsFile=/dev/null" in argv
    command = argv[-1]
    assert "requires_openai_auth=false" in command
    assert "127.0.0.1:8582/v1" in command
    assert "features.shell_tool=false" in command
    assert "features.skip_host_skill_discovery=true" in command
    assert "project_doc_max_bytes=0" in command
    assert "CODEX_HOME" in command
    assert "auth.json" not in command
    with pytest.raises(ValueError):
        runner.connection_argv(CONNECTION, "/home/agent/.codex")
    with pytest.raises(ValueError):
        runner.connection_argv({**CONNECTION, "base_url": "https://example.test/v1"}, runner.REMOTE_PREFIX + "abc")


def test_noncontext_followup_uses_fresh_home_and_no_native_history(tmp_path, monkeypatch):
    first, request = case(tmp_path)
    native(monkeypatch, tmp_path)
    runner.run(request, CONNECTION, 5)
    second, follow = case(tmp_path, "phase-2")
    (second / "prompt.txt").write_text("Original prompt.")
    runner.run(follow, CONNECTION, 5)
    one = json.loads((first / "native-session.json").read_text())
    two = json.loads((second / "native-session.json").read_text())
    assert one["remote_home"] != two["remote_home"]
    trace = (second / "codex-native.jsonl").read_text()
    assert '"method": "thread/start"' in trace
    assert '"method": "thread/fork"' not in trace
    assert '"method": "thread/read"' not in trace


def test_unrelated_mcp_server_rejected_before_native_start(tmp_path, monkeypatch):
    root, request = case(tmp_path)
    (root / "mcp.json").write_text(json.dumps({"mcpServers": {"unrelated": {"command": "false"}}}))

    def forbidden(*args):
        raise AssertionError("Native startup must not occur")

    monkeypatch.setattr(runner, "connection_argv", forbidden)
    with pytest.raises(ValueError, match="Only the supplied fixture"):
        runner.run(request, CONNECTION, 5)
    assert not (root / "codex-native.jsonl").exists()


@pytest.mark.parametrize(
    "requested,transport,source",
    [
        ("official:gpt-6.1-sol", "gpt-6.1-sol", "official"),
        ("gpt-6.1-terra-baidu-oneapi", "gpt-6.1-terra-baidu-oneapi", "gateway"),
        ("gpt-6.1-sol", "gpt-6.1-sol", "gateway"),
    ],
)
def test_requested_identity_and_native_transport_are_explicit(requested, transport, source):
    assert runner.model_identity(requested) == (transport, source)


@pytest.mark.parametrize("requested", ["official:", "official:other:model", "official:custom", "official:gpt-test bad"])
def test_unrecognized_official_id_cannot_fall_back_to_another_transport(requested):
    with pytest.raises(ValueError, match="Unsupported official model identity"):
        runner.model_identity(requested)
