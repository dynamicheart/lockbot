"""Offline handoff example. Run with PYTHONPATH=python python examples/agent_handoff.py."""

import json

from lockbot.agent_client.handoff import final_command_payload, make_handoff

# Replace only after authenticating context with the selected private-store key
# on the approved service origin. Do not put the actual key into this envelope.
origin = "https://resources.example.invalid"
credential_ref = "keyring:resource-group-a"
context = {
    "server": {"service_id": "lockbot.resource-api", "instance_id": "instance-a", "api_version": "1"},
    "bot": {"id": 7, "uuid": "bot-a"},
    "actor": {"platform": "Infoflow", "external_user_id": "user-a"},
    "credential_scope": "one_user_one_bot",
    "agent_access_enabled": True,
}
trusted = make_handoff(origin, context, credential_ref)
# Parent retains `trusted` privately; only this serialized copy goes to the child.
received = json.loads(json.dumps(trusted))
candidate = {"command": "lock node-a 30m", "purpose": "child-inferred internal detail", "agent_label": "child"}
body = final_command_payload(
    trusted,
    received,
    origin=origin,
    authenticated_context=context,
    credential_ref=credential_ref,
    candidate=candidate,
)
print(json.dumps(body))  # {"command": "lock node-a 30m"}; no HTTP, mutation or retry.
