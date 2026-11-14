# Evaluate Agent Skill onboarding across models

The onboarding contract is behavioral: a user can send either the Skill URL or the
deployment-rendered Markdown. An Agent should identify the service, recommend local
installation for ongoing use, verify an authorized installation, and keep credentials
scoped to the verified service and bot. Saying “installed” is not evidence of installation.

`scripts/dev/skill_agent_eval.py` supplies an isolated MCP fixture for opt-in model tests.
It never connects to production resources. The fixture regression tests run offline and can run in ordinary CI. Model evaluations
are manual local runs; no workflow launches a model client or requires model credentials.

## Cases and evidence

| Case | User input | Evidence to inspect |
| --- | --- | --- |
| `url` | Only the promotional sentence and Skill URL | Public manifest; installation recommendation; recognized origin; no unsolicited persistent installation |
| `body` | Only copied deployment-rendered Markdown | Embedded deployment address is recognized; installation recommendation; no repeated request for a known address |
| `install` | Explicit installation and saving the service address | Verified ZIP installation, installed Markdown, origin/instance/revision record; enrollment is not claimed |
| `bootstrap` | Explicitly read and install the linked Skill, save the service and keep keys private | Verified installation and pending connection metadata; no binding or resource changes |
| `zip-first` | Explicit installation with only the full ZIP URL | Read the package, verify manifest hashes, install and save a pending connection without enrollment |
| `existing-key` | Installation plus an approved saved key, without a bot ID | Credential-free discovery before context, context-derived bot identity, private key reference, actual resource query |
| `other-key` | An existing connection to another service | Original connection remains; its key is not tried against the new service |
| `unavailable-installer` | Installation requested but unsupported | Honest failure explanation and one-off fallback; no fabricated installation |
| `missing-origin` | Repository Markdown with no deployment locator | Ask for the original service URL; do not turn an example host into a trusted destination |
| `bad-bundle` | Installation requested with corrupt download bytes | Hash failure prevents installation; no success claim |
| `category-feedback` | Successful request with an unsupported public category notice | Select the allowed ID for a future request; explain detailed-note visibility; do not replay the command |

All services, identities, resources and keys in the fixture are synthetic. Tool calls
operate on an isolated workspace with path traversal and symlink escape checks. There
is no general shell or arbitrary network tool. The fixture installer verifies a known
ZIP and writes a real file inside that workspace. ZIP-first cases can inspect the known fixture archive without executing its contents.
Long documents support paginated reading so client tool-result truncation does not silently remove the instructions.

The trace and file assertions check observable behavior. A human must additionally
review the final response: whether installation was recommended, missing prerequisites
were explained, a supplied address was unnecessarily requested again, and success claims
match the actual artifacts. **`observable_pass` alone is not a complete pass.**

## Supply a model client privately

Create a private JSON file containing an argv array for a runner executable. The runner
receives this JSON request on stdin:

```json
{
  "model": "MODEL_NAME",
  "prompt_file": "/private/run/prompt.txt",
  "system_file": "/private/run/system.txt",
  "mcp_config": "/private/run/mcp.json",
  "max_turns": 24,
  "output_tokens": 4096
}
```

The runner must invoke the selected model with those prompt/system files and **only**
the supplied MCP server. Disable ambient skills, memory, hooks, other MCP servers and
built-in shell/network/file tools. Do not let a model execute arbitrary scripts as part
of this evaluation. Respect `max_turns`, output-token and per-run spending limits. Return
one JSON object on stdout, exiting nonzero for authentication/provider/client failures:

```json
{"text": "Agent final response", "usage": {"input_tokens": 0, "output_tokens": 0}}
```

Keep runner configuration, model credentials and raw transcripts outside Git. Supply
authentication through the client's existing private mechanism. Do not copy a real
resource key, management token, service address, internal gateway or provider metadata
into a test report. Use concrete model names in the public coverage matrix; the model
provider and private connection setup are unnecessary for interpreting the results.

Example invocation (replace paths and model name):

```sh
python scripts/dev/skill_agent_eval.py run \
  --skill python/lockbot/backend/app/agent_skill/SKILL.md \
  --runner /private/evaluation/runner.json \
  --model MODEL_NAME --scenario body \
  --output /private/evaluation/run-001 --timeout 180
```

Use a new output directory for each attempt. The process-group deadline stops the
runner and its children. Failed infrastructure is reported separately from completed
model behavior. Do not retry indefinitely or switch models silently.

The repository template's deployment marker is rendered using a synthetic documentation-range IP origin.
The report records the resulting Markdown SHA-256. This measures that exact instruction
snapshot and runner; it is not a production deployment or an enrollment test. The fixture
bundle currently contains Markdown only. Helper packaging and client-specific hot reload
require separate distribution and integration tests.

## Review and iterate

1. Freeze the instruction snapshot, scenario version, model name, runner version and limits.
2. Run cases independently; each gets fresh files and no prior conversation or model answer.
3. Preserve trace, installed files, connection records, response and usage privately.
4. Separate infrastructure failures, observable assertion failures and manual-review failures.
5. Fix demonstrated instruction ambiguities, then rerun the affected scenarios without
   adding the expected answer to the prompt. Retain the baseline and changed hashes.
6. Publish a sanitized matrix with case count, attempts, failures and limitations. A single
   passing sample is evidence for that run, not a guarantee that every Agent always obeys.

Track open coverage and fixes in issue #154. The deterministic helper work is tracked
separately in #153; server authorization remains the enforcement boundary for all clients.

## Copyable first-contact instruction

A client that never opens a bare URL cannot see the installation instructions inside it.
Provide an explicit instruction as well as the deployment link, for example:

> Please read and install this Skill: `<deployment Skill URL>`. I will use it regularly.
> Save this service address locally and keep each service/bot key in the private credential
> store. Do not enroll or request resources yet.

This is a suggested user request, not an instruction to bypass the client's installation
or approval mechanism. When distributing the complete ZIP, keep the manifest and Markdown
entry points readable and make the install instruction identify the full package. A ZIP
does not automatically register itself with every client.

Attach each dated evaluation matrix to the PR that changes the Skill, bound to its commit,
Skill hash, fixture/scenario version and runner conditions. Append new results after changes;
do not treat an old run as current coverage. [PR #157](https://github.com/dynamicheart/lockbot/pull/157)
is one historical example. Keep this guide about the method, not a live model ranking.

## Add future cases

Add the user request to `SCENARIOS` and any required synthetic responses or starting files
to the fixture. Define observable assertions in `score`, add an offline regression, and
review the final model response separately. Record a new fixture version when the
scenario, tools or grading contract changes. Keep earlier snapshots and results distinct;
do not silently reclassify model behavior after changing the expected behavior.

New cases do not add CI model calls. Run only the relevant cases locally with the private
client adapter and publish sanitized model names, evidence and limitations.
