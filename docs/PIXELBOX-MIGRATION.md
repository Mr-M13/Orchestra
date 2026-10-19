# PixelBox migration: Linux ARM64 coordinator with GitHub Actions workers

Status: **implementation complete; deployment and cutover require explicit owner approval.**

PixelBox (`hamed@100.70.11.93`) becomes the persistent Orchestra coordinator.
GitHub Actions supplies disposable coding workers. ChatGPT remains the control
plane through the authenticated MCP tunnel. The Mac service, Nextcloud Docker
stack and Tailscale Serve configuration remain in place until a separately
approved cutover.

## 1. Architecture

```text
ChatGPT / authenticated MCP tunnel
                |
                v
  PixelBox: orchestra-omcp.service
  - fixed principal and permissions
  - approval / standing-authority policy
  - durable state, audit, idempotency
  - repository profiles and scheduler
  - cancellation and restart reconciliation
          |                       |
          v                       v
  local Codex / Ina        GitHub Actions dispatcher
  (bubblewrap sandbox)     (owner-configured workflow)
                                  |
                                  v
                         ephemeral ubuntu runner
                         Codex or DeepSeek agent
                         patch + bounded result artifact
                                  |
                                  v
                         coordinator applies patch to
                         its isolated task worktree
```

Design boundaries:

- The coordinator owns truth. GitHub Actions is an execution instrument, not a
  second scheduler.
- A GitHub worker never receives the tunnel key, OMCP principal marker, local
  approval token or repository write token.
- The workflow has `contents: read` and uploads a patch artifact. It does not
  push, merge, deploy or create releases.
- A successful remote run is execution evidence, not independent verification
  and not authority to integrate.

## 2. Prerequisites on PixelBox

Use 64-bit Raspberry Pi OS/Debian. Install only additive packages; do not alter
the existing Nextcloud Docker services or Tailscale Serve configuration.

```sh
sudo apt-get update
sudo apt-get install -y python3 python3-venv git bwrap rsync curl ca-certificates
python3 --version            # 3.11 or newer
bwrap --version
git --version
```

Install the reviewed Codex CLI for the service user, or place it on the service
`PATH`. Confirm:

```sh
command -v codex
codex --version
```

Install `tunnel-client` from the OpenAI Platform tunnel page or the latest
public release. Keep it in `/usr/local/bin/tunnel-client` and verify:

```sh
tunnel-client help quickstart
```

OpenCode is optional. If used, install the reviewed version at an owner-chosen
path and set `OMCP_OPENCODE_CLI` in `/etc/orchestra/omcp.env`. The path is
resolved and compared before each launch.

Do not install the macOS approval notification LaunchAgents on PixelBox. The
local approval command and the existing Linux-compatible approval portal are
available; remote approval is not enabled by this migration.

## 3. Directory layout

Keep the deployed source, checkout and state separate:

```text
/opt/orchestra/                         deployed reviewed source
/home/hamed/orchestra-migration/
  checkout/                             owner Git checkout (origin/main)
  state/                                private authoritative state
  backup/                               optional encrypted backups
/etc/orchestra/
  omcp.json                             owner-only configuration
  omcp.env                              fixed-principal tunnel assertions
  tunnel.env                            tunnel runtime key
  secrets/github-actions.token          fine-grained GitHub token
```

`state_dir` must remain outside `project_root`. OMCP enforces this and refuses a
configuration whose state lives inside the checkout.

## 4. GitHub Actions worker setup

### Repository workflow

The reviewed workflow is `.github/workflows/orchestra-worker.yml`. It:

- runs only on `workflow_dispatch`;
- has a deterministic `run-name` of `Orchestra <task_id>`;
- uses `contents: read` and does not persist checkout credentials;
- installs the pinned Codex CLI and OpenCode CLI;
- runs `codex` for the Codex agent and OpenCode for the DeepSeek agent;
- keeps provider keys and OpenCode runtime files outside the uploaded artifact;
- writes `result.txt`, `patch.diff`, `worker.log` and `summary.json` to an
  artifact named `omcp-<task_id>`;
- uploads the artifact even when execution fails, so the coordinator can
  report a bounded failure.

### Secrets and environment

Create a GitHub Actions environment named `orchestra-workers`. Add:

| Secret | Used by | Notes |
| --- | --- | --- |
| `OPENAI_API_KEY` | Codex worker | Provider credential only. |
| `DEEPSEEK_API_KEY` | DeepSeek worker | Provider credential only. |

Do not put the tunnel runtime key, OMCP approval token or GitHub token in the
workflow secrets. The Actions token is available to the workflow automatically
but is not exported to the agent process; `persist-credentials: false` also
keeps it out of the checkout.

For stronger separation, add required reviewers to the `orchestra-workers`
environment. This is optional because the coordinator already owns the approval
gate; if enabled, it becomes a second, independent approval.

### Fine-grained GitHub token for the coordinator

Create a fine-grained token scoped to the single configured repository with:

- Actions: read and write (dispatch, run status, cancel, artifact download);
- Contents: read (workflow metadata and checkout);
- Metadata: read (mandatory).

Store it at `/etc/orchestra/secrets/github-actions.token`, owned by `hamed`,
mode `0600`. Never put it in `omcp.json`, a task, a command line, an audit
record or Git. The coordinator sends it only in the HTTPS `Authorization`
header.

### Workflow ref

`workflow_ref` must name a branch or tag that contains the reviewed workflow.
Use `main` only after the workflow and worker script are merged. Until then,
use the reviewed feature branch for staging and record that choice. The
workflow file cannot be selected by a task; it comes from owner-only
configuration.

## 5. Configuration

Start from `deploy/pixelbox/omcp.example.json` and adjust paths and IDs. Minimum
production settings:

- `task_approval_mode: "required"` until the owner explicitly accepts a broader
  policy;
- `max_parallel_tasks: 2` initially;
- `sandbox_backend: "bubblewrap"` on PixelBox;
- each `github_actions` worker points at one reviewed workflow and one agent;
- no standing authorization for GitHub Actions workers;
- `repository_profiles` binds each allowlisted checkout and base ref.

Install owner-only files:

```sh
sudo install -d -m 0700 -o hamed -g hamed /etc/orchestra /etc/orchestra/secrets
sudo install -m 0600 -o hamed -g hamed omcp.example.json /etc/orchestra/omcp.json
sudo install -m 0600 -o hamed -g hamed omcp.env.example /etc/orchestra/omcp.env
sudo install -m 0600 -o hamed -g hamed tunnel.env.example /etc/orchestra/tunnel.env
```

Edit the files locally. Do not paste secret values into a chat, issue, pull
request or log.

Validate without starting the service:

```sh
cd /opt/orchestra
python3 -c 'from pathlib import Path; from omcp.controller import Config; Config.load(Path("/etc/orchestra/omcp.json")); print("configuration valid")'
```

`Config.load` checks owner-only mode, repository profile IDs, worker allowlists,
credential-file permissions, workflow names and refs, bound approvals and
concurrency limits. It does not print secret values.

## 6. Authenticated MCP tunnel

The tunnel is a transport boundary. It does not prove individual ChatGPT user
identity to the stdio process. Keep the fixed-principal model and use a
dedicated tunnel/app; do not expose mutating tools to an unverified app or
workspace boundary.

Initialize the local stdio profile once. Enter the tunnel runtime key locally;
do not put it on the command line or in shell history.

```sh
read -rs CONTROL_PLANE_API_KEY
export CONTROL_PLANE_API_KEY
tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile pixelbox \
  --tunnel-id tunnel_REPLACE_WITH_VERIFIED_TUNNEL_ID \
  --mcp-command "python3 -m omcp.mcp_proxy --socket /home/hamed/orchestra-migration/state/mcp.sock"
unset CONTROL_PLANE_API_KEY
```

`orchestra-omcp.service` owns the persistent coordinator and its owner-only
`mcp.sock`. `tunnel-client` starts only the small stdio proxy, so an individual
tunnel connection cannot terminate the scheduler or lose durable state. The
coordinator service environment supplies:

```text
OMCP_TUNNEL_AUTHENTICATED=1
OMCP_AUTHENTICATED_CALLER=orchestra:dedicated-workspace
```

The caller value must exactly match `principal.identity`. These are an operator
assertion that the dedicated app/workspace boundary was verified; they are not
per-request authentication. Record the tunnel ID, Platform organization,
ChatGPT workspace, app visibility and membership review date in the operator
evidence file. Review again on membership or app changes.

```sh
tunnel-client doctor --profile pixelbox --explain
sudo systemctl enable --now orchestra-tunnel.service
curl --fail --silent http://127.0.0.1:8788/readyz
```

Use the actual tunnel-client admin/health port from its output; do not assume
`8788` if the tool reports another local port. Then discover the nine OMCP tools
through ChatGPT and run the verification sequence in [OMCP](OMCP.md).

Keep the existing read-only Mac Bridge tunnel running until the end-to-end gate
below passes.

## 7. systemd operations

Units live in `deploy/pixelbox/systemd/`:

| Unit | Purpose |
| --- | --- |
| `orchestra-omcp.service` | persistent coordinator, local approval socket and MCP Unix socket |
| `orchestra-tunnel.service` | dedicated authenticated tunnel plus a stateless stdio proxy |
| `orchestra-health.service` | one-shot sanitized health report |
| `orchestra-health.timer` | runs health every two minutes |

Preview the installation without changing anything:

```sh
./deploy/pixelbox/install.sh
```

After explicit cutover approval:

```sh
ORCHESTRA_CUTOVER_APPROVED=1 ./deploy/pixelbox/install.sh --apply
```

The installer copies the reviewed tree, installs units, preserves existing
configuration files, and starts only the coordinator and health timer. It does
not touch Nextcloud units, Docker, Tailscale Serve, the Mac service, or the
tunnel. Enable the tunnel separately after its profile is verified.

Common commands:

```sh
systemctl status orchestra-omcp.service --no-pager
journalctl -u orchestra-omcp.service -n 100 --no-pager
systemctl restart orchestra-omcp.service
systemctl stop orchestra-omcp.service
systemctl start orchestra-health.service
systemctl list-timers orchestra-health.timer
```

`KillMode=control-group` ensures a planned service stop also stops local worker
and host-operation runners. A crash or power loss can leave a runner alive; the
coordinator adopts only a matching task/lease/PID and fails closed otherwise.

## 8. Health and diagnostics

```sh
cd /opt/orchestra
python3 -m omcp.health --config /etc/orchestra/omcp.json
python3 -m omcp.health --config /etc/orchestra/omcp.json --json
python3 -m omcp.health --config /etc/orchestra/omcp.json \
  --tunnel-health-url http://127.0.0.1:8788/readyz
```

The report includes:

- configuration validity and owner-only mode;
- repository profile and worker inventory, including remote adapters;
- sandbox backend availability;
- state readability and audit presence;
- heartbeat age, PID liveness, active tasks and execution limit;
- pending and expired approvals;
- scheduler saturation, disk headroom and tunnel readiness.

It deliberately excludes instructions, provider output, tokens and task bodies.
Exit codes: `0` healthy, `2` degraded unless `--allow-degraded`, `1` failed.
`bridge_status` also reports the platform, sandbox backend, scheduler cursor and
remote worker names.

## 9. Approvals, authorization and audit

Approval policy remains server-owned. The remote MCP caller cannot approve a
task or change policy. Use a local terminal:

```sh
cd /opt/orchestra
python3 -m omcp.approval \
  --config /etc/orchestra/omcp.json \
  --task-id TASK_ID \
  --decision approve
```

The command requires an interactive terminal, displays the full task context
and asks the operator to type the exact decision and task ID, followed by a
reason. It sends the decision only through the owner-only Unix socket and
per-process capability file under `state_dir`.

Every task records:

- fixed principal, authentication description and permission check;
- repository profile, repository and base ref;
- worker, consequence class and authorization mode;
- approval status, decision actor, time and reason;
- scheduler lane, reservation and resource requirements;
- cancellation reason and terminal reconciliation;
- for GitHub workers, the safe run ID/URL and bounded artifact name.

`audit.jsonl` is append-only and fsynced per event. It does not contain task
instructions or worker output. Provider credentials are never written to state,
specs, audit or completion files.

Disabling task approvals (`task_approval_mode: "disabled"`) does not disable
permissions, profile allowlists, sandboxing, concurrency, cancellation, result
bounds or audit. Direct DeepSeek and host operations still require explicit
local approval.

## 10. GitHub worker lifecycle and cancellation

1. A valid task is persisted with a request ID and content digest.
2. The scheduler claims a lane and repository reservation.
3. A fresh worktree is created from the configured base ref.
4. The detached GitHub runner dispatches the reviewed workflow with bounded
   compressed task text, or adopts an existing deterministic run after restart.
5. The runner persists the exact run ID before polling.
6. On success it downloads only the named artifact, rejects unsafe paths and
   size overruns, verifies the patch applies, and records the result.
7. On cancel, timeout, runner termination or restart reconciliation, it asks
   GitHub to cancel the exact run before marking the task terminal.
8. The coordinator never retries an interrupted task automatically.

Cancellation is terminal for queued work and best-effort for a remote run that
GitHub has already started. Verify the run URL in task status if GitHub reports
that cancellation was too late. Do not re-dispatch the same request ID with
different content; idempotency fails closed.

## 11. Recovery

Authoritative state is `state_dir/state.json`; audit is append-only. Worker
logs, prompts and worktrees remain for inspection. Back up the whole state
directory while the service is stopped, then encrypt backups containing task
text and logs.

Restart procedure:

1. `systemctl stop orchestra-omcp.service` (or recover after a crash).
2. Check free disk and file ownership: state and config must be owner-only.
3. Start the coordinator.
4. Run `omcp.health`. Confirm heartbeat, workers, sandbox and task counts.
5. Review active tasks. A runner is adopted only when task ID, lease ID and a
   live PID match. Otherwise the task becomes `interrupted` or `cancelled` and
   its reservation is released.
6. For a remote task, compare the recorded run ID/URL with GitHub. If the run is
   still active after the coordinator marked it terminal, cancel it manually
   from GitHub and record the evidence.

Never edit `state.json` by hand to force a result. Preserve the file and use the
audit record to reconstruct what happened.

Backup example (run while stopped):

```sh
umask 077
tar -C /home/hamed/orchestra-migration -czf \
  /home/hamed/orchestra-migration/backup/state-$(date -u +%Y%m%dT%H%M%SZ).tar.gz state
```

A restore is a new deployment action: restore into an empty state directory,
validate permissions, start the coordinator, and verify health before
submitting work.

## 12. Cutover gate

Do not cut over on unit tests alone. The owner must explicitly approve after all
of the following evidence is recorded:

1. `python3 -m unittest discover -s tests -v` passes on PixelBox.
2. `python3 -m compileall -q omcp tests scripts` passes.
3. `Config.load` validates the reviewed owner-only configuration.
4. `omcp.health` reports `ok` (or a documented, accepted degraded reason).
5. The dedicated tunnel profile passes `tunnel-client doctor` and `/readyz`.
6. ChatGPT discovers all nine OMCP tools and `bridge_status` reports
   `platform=linux`, `sandbox.backend=bubblewrap`, and the expected workers.
7. An unclassified harmless task remains `approval_required`; approving it
   locally runs it and returns a terminal result.
8. Idempotent replay returns the same task ID.
9. A queued task and a running task both cancel terminally; a remote run is
   cancelled in GitHub.
10. Codex and DeepSeek GitHub workers each complete one harmless task and return
    a patch/result. Confirm the workflow did not push or merge.
11. A pending approval survives a coordinator restart with no duplicate
    notification or execution.
12. The Mac Orchestra service is still running and the read-only Bridge is
    untouched.
13. Nextcloud units and containers, Docker configuration, and Tailscale Serve
    status are observed unchanged. Do not restart or edit them as part of this
    gate.

Only then route the production ChatGPT app to the PixelBox tunnel. Keep the Mac
service available for rollback until the owner records a successful observation
window.

### Rollback

Rollback is a routing action, not a code mutation:

1. Disable the PixelBox tunnel unit.
2. Point the private ChatGPT app back to the verified Mac tunnel.
3. Leave PixelBox state and logs intact for evidence.
4. Re-run the Mac health and harmless-task checks.
5. Do not delete PixelBox state until the owner authorises retention cleanup.

## 13. Security notes and limitations

- Never commit credentials. Never print a token, API key or capability value in
  a command, log, issue or task.
- The GitHub token is owner-only and repository-scoped. Rotate it after suspected
  exposure and update `/etc/orchestra/secrets/github-actions.token`.
- The workflow receives provider keys through the `orchestra-workers`
  environment. Treat that environment's membership and reviewer rules as part of
  the trust boundary.
- The Linux bubblewrap file sandbox is deny-by-default for filesystem paths. A
  policy that needs a provider endpoint shares the network namespace because
  bubblewrap cannot filter egress by host. For direct local provider adapters,
  enforce destination allowlisting at the host firewall if that extra control is
  required. GitHub Actions workers do not run on PixelBox and use GitHub's
  ephemeral runner isolation.
- DeepSeek has two distinct modes: the existing direct, response-only HTTPS
  adapter on macOS, and the GitHub Actions coding worker configured in this
  migration. They are not aliases. The direct adapter still requires explicit
  local approval.
- The Mac approval notification helper is macOS-specific and is not installed on
  PixelBox. Remote approval remains unavailable until a separately reviewed
  authenticated decision protocol exists.
- v1 does not auto-merge, push, deploy, retry interrupted work, provide
  multi-coordinator election, or claim that worker success verifies the goal.
