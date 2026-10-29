# Telemetry deployment handoff for administrators and Agents

## Download and verify

Download the versioned `lockbot-telemetry-<version>-linux-amd64.tar.gz` (or `linux-arm64`) and its `.sha256` from the matching Lockbot GitHub Release. PR builds are preview artifacts under **Publish Telemetry Bundle**, not formal releases. Verify `sha256sum -c <archive>.sha256` before unpacking. `manifest.json` lists the source commit, file hashes, dependency versions, and the official Prometheus release URL and verified upstream checksum. Never use a preview archive as if it were a published release.

The bundle contains the official Prometheus and promtool binaries, their licenses, the Lockbot exporter and gateway, the PyJWT wheel for offline gateway installation, public configuration examples and systemd templates. It excludes all deployment machine inventories, SSH identities, tokens, signing keys and collected data. Building it requires outbound access to official GitHub releases and PyPI; installing an already downloaded bundle does not require pip internet access.

Prometheus currently uses the reproducible 3.7.3 baseline; this is not a claim that it is the newest upstream release. The binary comes directly from `https://github.com/prometheus/prometheus/releases/download/v3.7.3/`, not an unofficial mirror or a custom Prometheus build. The deployment is a Linux/systemd binary deployment, not a Docker image deployment.

## Dependencies and access

- Collector host: Linux, Python **3.10+** with venv/pip, OpenSSH client, systemd, suitable disk space and outbound SSH to the inventory. The collector itself uses only Python's standard library.
- Every SSH target: Python **3.10+**, the correct XPU or NVIDIA driver and the matching `xpu-smi` or `nvidia-smi` executable. No full Lockbot installation is needed there.
- Gateway: Python 3.10+ and the bundled PyJWT dependency. HS256 does not require an extra cryptography package.
- No MPI, mpirun, private `cmd.py`, `yjb_smi.py` or custom shell collector is required. Existing SSH configuration can select the appropriate key and host alias.
- Unattended SSH mode requires the **collector service account** to log in with `BatchMode=yes` using an approved SSH key. Configure known host identities according to local policy. An Agent must not ask for private key contents in chat or disable SSH host verification. Password-only logins cannot support unattended sampling. Local-only collection needs no SSH setup.
- Read-only process/container metadata may require extra host permissions. Missing permission must produce unknown metadata, not fabricated ownership; grant only the access approved by the administrator. Docker inspect capability is privileged and is not granted automatically.

## Install without overwriting existing services

1. Check architecture (`uname -m`), Python version, disk space, existing ports and existing Prometheus/services. Do not replace another service. Stage the unpacked bundle at a new, administrator-approved location, conventionally `/opt/lockbot-telemetry`.
2. Run `bash prepare.sh`. `PYTHON_BIN=/path/to/python3.12 bash prepare.sh` selects a suitable interpreter. This prepares an offline virtualenv and checks the example Prometheus config; it does **not** start services, overwrite live config or generate credentials.
3. Copy `examples/fleet.example.json` to private `config.json`. Set each source node label, SSH account/host and SMI path; check filesystem aliases. Keep `interval_seconds: 300` and bounded workers. For a local collector use `examples/config.example.json`. Never commit your real inventory.
4. As the intended collector service account, verify one approved target using `ssh -o BatchMode=yes account@host 'python3 --version'`. Confirm SMI presence and XML support. Do not repeatedly execute SMI or test every machine simultaneously. Normal collection is staggered at five-minute intervals.
5. Copy `examples/prometheus.example.yml` to `prometheus.yml`. Create a writable `data/` directory for the Prometheus service account. Validate the actual file with `./promtool check config prometheus.yml`. Keep Prometheus bound to loopback; TSDB retention defaults to seven days / 1 GB.
6. Create a dedicated gateway account (the example uses `lockbot-metrics`). Copy `examples/gateway.example.json` to private `gateway.json`. Configure the **actual** loopback Prometheus URL, allowed CORS origins, canonical gateway audience, issuer instance ID, bot UUID and exact node scope. Generate an independent 32+ byte signing key with mode 0600, readable only by the gateway account; provide the same key through the approved secure channel to Lockbot's `METRICS_GRANT_SECRET_FILE`. Do not reuse JWT_SECRET or personal/OP keys, print keys, or put them into resource configuration JSON. Restart Lockbot after adding its environment setting.
7. Review the three service templates. Replace installation paths and Python paths; set the collector service account with its SSH home/config, give Prometheus ownership only of its TSDB, and give the gateway access only to its own private configuration/key. Examples do not automatically create accounts or configure SSH. Copy reviewed units to `/etc/systemd/system/`, then `systemctl daemon-reload` and enable/start only these approved new units. If paths/ports differ, update all matching configurations.
8. Check exporter `/metrics` and Prometheus `/-/ready` locally, then check the gateway with an authorized short-lived **metrics** grant. In Lockbot bot detail → Configuration → Data source, choose the template and map every resource. Validate and save from the actual viewer network. Node addresses/labels and card indices must be verified, not guessed. Allow gateway browser access only through the intended network/TLS boundary.

## Acceptance and rollback

Confirm each target's collector status and actual observation time. A query/scrape timestamp does not prove a fresh hardware sample. First CPU sampling can be unknown until a second counter observation. Test a failed/stale node, denied authorization, browser CORS, hidden-tab pause and the existing lock/queue flow. Check every configured card, available host memory and buffers/cache. Never treat zero accelerator load as a free resource or classify cache alone as a driver OOM.

Keep old scripts, units, configuration and TSDB before upgrading. Stop only the new telemetry services to roll back; Lockbot's queues remain independent. Do not delete or restore an unrelated Prometheus TSDB. An Agent should report download checksum, deployed versions, service state, node/card coverage and remaining unverified hardware/network cases without including secrets.

## Adding a second, isolated environment

The signing-key step above describes a new single-source deployment. **Do not replace an existing Lockbot `METRICS_GRANT_SECRET_FILE` when adding another source, and do not copy that existing HMAC signing key into a separate shared-root environment.** Replacing it breaks the first source; sharing it expands the new source host's signing authority. Automatic multi-environment grants require independently selected per-source keys or an asymmetric verifier design, which the current single-file setting does not implement.

For an isolated integration test, configure a new independent read-only gateway reader with a random secret and exact node scope. Select independent source credentials, deliver that token through the approved secure channel, and retain it only in the authorized browser's memory or the Agent's private credential store. Do not overwrite existing source configuration or signers. This is an explicitly scoped test mode, not automatic cross-environment authorization.

An administrator's successful browser test proves only that browser's network/permission path at that moment. Each viewer and Agent must have its own permitted path to the source. Lockbot does not need to reach the source server. If a local SSH tunnel is approved, bind only to localhost and keep source authorization/CORS enabled; localhost is a per-client endpoint and **must not be saved as a universally reachable shared bot source**. Do not assume a bastion supports TCP forwarding or ProxyJump. If it refuses forwarding, stop and request an approved private network entry instead of opening a public service or changing network ACLs.
