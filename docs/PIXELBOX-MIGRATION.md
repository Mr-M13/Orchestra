# PixelBox migration: Linux ARM64 coordinator with GitHub Actions workers

Status: **implementation and portable release-staging logic are locally tested;
Linux ARM64 staging, live deployment verification, and production cutover have
not been performed and require explicit owner approval.**

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

The supplied configuration uses a systemd transient-service sandbox. It is the
compatible fallback for a host where Bubblewrap cannot construct its private
root filesystem (for example, `Can't open source /: Function not implemented`).
It retains `ProtectSystem=strict`, private temporary storage,
no-new-privileges, restricted address families, and explicit read/write bind
paths. Do not replace it with unsandboxed execution. Bubblewrap remains
available for hosts where a real worker preflight succeeds.

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
Linux-compatible owner approval portal is `omcp.approval_portal`, installed by
`orchestra-approval-portal.service` on `127.0.0.1:8767`. It requires a
pre-existing owner-only token in the state directory and calls the existing
`approval.sock` capability; the state directory is mounted read-only by the unit.
Access it only through an authenticated SSH tunnel from the owner's workstation:

```sh
ssh -N -L 8767:127.0.0.1:8767 hamed@100.70.11.93
```

Then open the bootstrap URL using the token read locally from the owner-only
token file. Do not print the token, expose port 8767 through Cloudflare or
Tailscale Serve, or add a web approval RPC. Remote approval mode is not enabled
by this migration. The public dashboard is read-only for approvals and never
trusts forwarded identity headers.

## 3. Directory layout

Keep the deployed source, checkout and state separate:

```text
/opt/orchestra/
  releases/<40-character-git-sha>/      root-owned source exported from Git
  current -> releases/<sha>              atomically selected active release
  previous -> releases/<sha>             last successfully active release
/home/hamed/orchestra-migration/
  checkout/                             owner Git checkout (origin/main)
  state/                                private authoritative state
  backup/                               optional encrypted backups
/etc/orchestra/
  omcp.json                             owner-only configuration
  omcp.env                              fixed-principal tunnel assertions
  tunnel.env                            tunnel runtime key
  github-dispatch/                     0700, owned by orchestra-github only
    config.json                        fixed target and allowlist policy
    github-actions.token               fine-grained GitHub token
```

`state_dir` must remain outside `project_root`. OMCP enforces this and refuses a
configuration whose state lives inside the checkout. Create and verify the
actual checkout before activation; the installer does not clone a repository
or infer a production revision. Every mutating command requires the full,
reviewed 40-character Git commit SHA.

Keep the checkout's `origin` set to the configured repository's canonical
`https://github.com/<owner>/<repository>.git` URL. For `github_actions`
workers, synchronise `origin/<branch>` through the reviewed migration/import
step before activation. The coordinator creates the local worktree from that
owner-synchronised ref without fetching, so the broker's private GitHub token
does not cross into the coordinator identity. Local workers fetch their configured base ref by default. On credential-isolated
PixelBox, set `local_worker_git_mode` to `owner_synced` and set
`local_worker_source_sha` to the full, independently reviewed commit SHA.
An owner-controlled sync must update the local tracking ref to that exact SHA
before starting workers. The coordinator checks that the ref matches the pin
and does not fetch. This pin is not evidence of freshness or upstream approval;
the owner must independently verify the SHA and repeat the sync/pin update for
new revisions. Never put GitHub credentials in the coordinator.

Each repository profile may override the global `local_worker_git_mode` and
`local_worker_source_sha`. This is the required shape for multiple unrelated
repositories: one profile can stay owner-synced and pinned while another
fetches its own `origin/main`. Profiles that omit these values inherit the
global defaults, preserving older configurations. Preparing one profile never
changes another profile's source strategy.

Routine writable paths are materialised inside the isolated task worktree
before the worker starts. A missing authorised directory is created safely;
absolute paths, `..`, `.git`, `.codex`, non-directory components and symlink
components are rejected. The coordinator does not make the source checkout
writable or create paths outside the task worktree.

Every local Codex task must emit a structured `.orchestra-tmp/completion.json`
contract; natural-language output is not used to decide success. Standing
routine tasks are also verified by a coordinator-controlled security probe.
The probe and its report live under coordinator state, outside worker-writable
paths; the coordinator runs it in a separate transient service through
`codex sandbox` with the same authoritative routine permission profile as the
worker. Weakening the shared policy weakens both paths. See
[OMCP standing authorization](OMCP.md#standing-authorization).

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

Do not configure required reviewers or a wait timer on the `orchestra-workers`
environment. Keep provider keys as environment secrets so GitHub releases them
only to the dependent execution job, not to the secretless SHA-verification job.
The coordinator and workflow each require the run head SHA to exactly match the
configured `workflow_sha`; they reject a moved or unfamiliar ref. Before dispatch,
the coordinator reads the environment through the GitHub API and fails closed
unless it has no human-review rule and permits deployments only from protected
branches.

### Credential-isolated GitHub dispatch broker

Create a fine-grained token scoped to the single configured repository with:

- Actions: read and write (dispatch, run status, cancel, artifact download);
- Contents: read (workflow metadata and checkout);
- Metadata: read (mandatory).

The coordinator must not own or read this token. A `0600` file under the same
UID is not isolation. After a separate owner approval, create a locked
`orchestra-github` system identity and an `orchestra-ipc` group, add only the
coordinator identity to that group, and install the broker directory as
`0700 orchestra-github:orchestra-github`. Install both
`github-dispatch/config.json` and `github-actions.token` there as `0600` owned
by `orchestra-github`. Never put the token in `omcp.json`, a task, argv, an
environment variable, a log, an audit record, a worktree or Git.

The service binds `/run/orchestra-github-dispatch/dispatch.sock` as the
dedicated UID with group-only coordinator access. It also checks Linux peer
credentials. Its protocol allows only a fixed configured target, allowlisted
agent/model pairs, exact run identity operations and the artifact name derived
from the task UUID. There is no generic URL, header, repository, workflow, ref,
SHA or credential request field. Bubblewrap/Seatbelt worker policies do not
mount or allow this socket; do not grant the IPC group to a worker identity.

### Workflow ref

Broker `workflow_ref` must name a protected branch that contains the reviewed
workflow; tags, raw commit IDs and other refs are rejected. Use `main` only after
the workflow and worker script are merged. Until then, use a protected reviewed
feature branch for staging and record that choice. The
workflow file cannot be selected by a task; it comes from owner-only
configuration. `workflow_sha` must be the full 40-character commit currently at
that branch. Immediately before dispatch, the coordinator resolves the branch,
requires GitHub to report it as protected, and compares its current commit with
`workflow_sha`; it then verifies the environment protections above. No workflow
is dispatched on a mismatch or missing protection. The expected SHA is also a
dispatch input. A credential-free job compares it with `github.sha`, and
only its success allows the environment-gated execution job to request provider
secrets. The checkout is pinned to that SHA. Returned-run validation checks both
ref and SHA, the workflow-dispatch event, the first run attempt, and the
owner-configured `dispatch_actor` in both GitHub actor fields. Dispatch requests
require GitHub's authenticated returned run ID. Recovery uses only that
persisted ID and the full immutable identity (including request, agent, model
and payload digest), so
a stale or spoofed same-name run is never adopted.

`github_service_uid` in `omcp.json` must be the numeric UID of
`orchestra-github`, and must differ from the coordinator effective UID. The
broker config separately binds both `service_uid` and `coordinator_uid`.
Configuration loading fails closed unless the live socket is owned by the
dedicated UID and is not world-accessible. The example deliberately contains
invalid placeholders. This code change does not create an account, group,
credential, config, socket, service activation or cutover.

## 5. Configuration

Start from `deploy/pixelbox/omcp.example.json` and adjust paths and IDs. Minimum
production settings:

- `task_approval_mode: "disabled"` for automatic `routine_development` work;
  protected consequence classes, direct DeepSeek work and host operations remain
  explicitly approved;
- `max_parallel_tasks: 2` initially;
- `sandbox_backend: "bubblewrap"` on PixelBox;
- `local_worker_launcher: "systemd-user"` on PixelBox so local Codex workers
  run in a separate transient user service rather than inheriting the
  coordinator's seccomp filter;
- each `github_actions` worker points only at the reviewed broker socket and
  binds its dedicated `github_service_uid`; target/SHA/token policy stays in
  the broker's private config;
- no standing authorization for GitHub Actions workers;
- `repository_profiles` binds each allowlisted checkout and base ref.

### Dashboard-development file grant

A narrow dashboard-development fragment is staged as
`deploy/pixelbox/omcp.dashboard-development.example.json`. It is not part of
`omcp.example.json` and the installer does not merge it into an existing owner
configuration. Add it only after explicit owner approval, preserving the
existing `ina-orchestra-routine-tests-v1` tests-only grant for unrelated work.

The fragment adds a policy-only repository profile
`orchestra-dashboard` with the same checkout and base ref, plus a separate
grant. Use that profile for dashboard-development tasks; use `orchestra` for
the existing tests-only work. Two grants may not overlap on consequence,
repository profile and worker, so configuration loading fails closed instead of
silently choosing by insertion order:

```json
{
  "repository_profiles": {
    "orchestra-dashboard": {
      "repository": "Mr-M13/Orchestra",
      "project_root": "/home/hamed/orchestra-migration/checkout",
      "base_ref": "origin/main"
    }
  },
  "standing_authorizations": [
    {
      "id": "ina-orchestra-dashboard-v1",
      "consequence_class": "routine_development",
      "repository_profiles": ["orchestra-dashboard"],
      "workers": ["ina"],
      "writable_paths": [],
      "writable_files": [
        "dashboard/app.py",
        "dashboard/system.py",
        "dashboard/static/system.js",
        "dashboard/README.md"
      ]
    }
  ]
}
```

`writable_files` grants exact regular files relative to the task worktree; it
does not grant a directory or the rest of the repository. The coordinator
creates only the missing parent directories, rejects absolute/traversal,
symlink, Git-metadata and non-regular targets, and still adds the private
task-local `.orchestra-tmp` directory as writable for
`orchestra-completion.json`. Both the real Codex worker and the trusted security
probe derive their rules from the same policy. A task matching this grant still
has `network: false`, no external credentials and read-only Git metadata.

### Local worker boundary

On Linux, the `systemd-user` launcher starts every local worker (Codex,
OpenCode, and the direct response-only DeepSeek adapter) in a transient
`orchestra-worker-<task-id>.service` under the service user's systemd manager.
The coordinator unit keeps `RestrictSUIDSGID=yes`; the worker service
deliberately sets `RestrictSUIDSGID=no`.

This is not a cosmetic removal. systemd implements `RestrictSUIDSGID=yes` with
a seccomp filter that makes `openat2()` return `ENOSYS`, because seccomp cannot
inspect openat2's flags. Bubblewrap 0.12 uses openat2 for safe path resolution,
so Codex or a direct DeepSeek runner fails before it can write a startup lease
or worker log with:

```text
bwrap: Can't mkdir parents for /: Function not implemented
bwrap: Can't open source /: Function not implemented
```

Before changing the launcher, reproduce this without a provider call:

```sh
cd /opt/orchestra/current
./deploy/pixelbox/reproduce-worker-startup.sh --config /etc/orchestra/omcp.json
```

The probe builds the same DeepSeek Bubblewrap policy, substitutes
`/usr/bin/true` for the Python runner, and runs it through transient user
services with `RestrictSUIDSGID=yes` and `RestrictSUIDSGID=no`. It reads no
credential content, opens no network connection and consumes no credits. The
expected result is `REPRODUCED`: the restricted launch fails, the user-service
launcher succeeds. The script prints only bounded, sanitized stderr.

The worker service retains `ProtectSystem=strict`, `ProtectHome=read-only`,
`PrivateTmp=yes`, `NoNewPrivileges=yes`, restricted address families,
non-root execution, control-group cleanup, and writable paths limited to the
task directory, its exact worktree and Git metadata, and the configured Codex
home. Codex's own sandbox still enforces the per-task filesystem and network
policy. It may read the owner checkout so Git can resolve the external metadata
of a linked worktree, but routine mode never permits writes there. The routine
profile also explicitly denies `/etc/orchestra`, because Codex's minimal system
read set otherwise includes `/etc`.

The launcher fails closed unless the service user has a live systemd user
manager at `/run/user/<uid>/systemd/private`. On a host where the service user
does not maintain a login session, enable lingering once with owner approval:

```sh
sudo loginctl enable-linger hamed
test -S /run/user/1000/systemd/private
```

Enabling lingering starts a user manager but does not start this worker
service; workers are transient and exist only for the lifetime of a task.
Do not add `RestrictSUIDSGID=no` to `orchestra-omcp.service` as a shortcut.

Install owner-only files:

```sh
sudo install -d -m 0700 -o hamed -g hamed /etc/orchestra
sudo install -m 0600 -o hamed -g hamed omcp.example.json /etc/orchestra/omcp.json
sudo install -m 0600 -o hamed -g hamed omcp.env.example /etc/orchestra/omcp.env
sudo install -m 0600 -o hamed -g hamed tunnel.env.example /etc/orchestra/tunnel.env
```

Do not run the following migration without explicit future approval. The exact
commands are intentionally illustrative: the owner must review local UID/GID
allocation and existing account/group state first.

```sh
sudo useradd --system --no-create-home --shell /usr/sbin/nologin orchestra-github
sudo groupadd --system orchestra-ipc
sudo usermod -a -G orchestra-ipc hamed
sudo usermod -a -G orchestra-ipc orchestra-github
sudo install -d -m 0700 -o orchestra-github -g orchestra-github /etc/orchestra/github-dispatch
sudo install -m 0600 -o orchestra-github -g orchestra-github github-dispatch.example.json /etc/orchestra/github-dispatch/config.json
# Traverse-only parent access.  Do not grant read/list on /etc/orchestra.
sudo setfacl -m u:orchestra-github:--x,m::--x /etc/orchestra
# Enter the token locally without argv, shell history, chat, issue, PR, or log exposure.
sudo systemctl enable --now orchestra-github-dispatch.service
```

`install -d -m 0700` rewrites the ACL mask on an existing `/etc/orchestra`,
which disables the broker's named traverse entry. The installer re-applies this
narrow ACL after every staging, activation preflight, and rollback preflight.
Verify the effective entry, not just the mode bits:

```sh
getfacl -cp /etc/orchestra
# user:orchestra-github:--x
# mask::--x
# other::---
```

Edit the files locally. Do not paste secret values into a chat, issue, pull
request or log. Verify the broker first; only then validate/start the
coordinator. Account/group creation, unit installation, activation and cutover
remain separate approval gates.

Validate without starting the service:

```sh
cd /opt/orchestra/current
sudo -u orchestra-github python3 -c 'from pathlib import Path; from omcp.github_dispatch_service import ServiceConfig; ServiceConfig.load(Path("/etc/orchestra/github-dispatch/config.json")); print("broker configuration valid")'
python3 -c 'from pathlib import Path; from omcp.controller import Config; Config.load(Path("/etc/orchestra/omcp.json")); print("coordinator configuration and live broker socket valid")'
```

`Config.load` checks owner-only mode, repository profile IDs, worker allowlists,
broker socket ownership, bound approvals and concurrency limits. Broker config
validation checks dedicated UID separation, private credential ownership,
target/ref/SHA syntax and model allowlists. Neither prints secret values.

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
  --mcp-command "python3 -m omcp.mcp_proxy --socket /run/orchestra/omcp.sock"
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
| `orchestra-dashboard.service` | loopback unified dashboard on `127.0.0.1:8082`; read-only system projection and no approval authority |
| `orchestra-approval-portal.service` | owner-only approval portal on `127.0.0.1:8767`; no public ingress and no approval RPC |

Preview the exact current commit without changing anything:

```sh
./deploy/pixelbox/install.sh
```

Record the full reviewed commit ID, then stage only that Git object. `git
archive` is used, so uncommitted checkout changes cannot enter the release:

```sh
release_sha=$(git rev-parse HEAD^{commit})
ORCHESTRA_DEPLOY_APPROVED=1 \
  ./deploy/pixelbox/install.sh --stage --sha "$release_sha"
```

Staging records and rechecks a SHA-256 digest of the complete exported tree, in
addition to validating the release marker, required files, shell syntax, and
Python compilation. It may create missing configuration templates, but it does not
install systemd units, change `current`, or start services. Portable staging on
macOS or x86 validates only those portable mechanics; it is not evidence that
the release has been staged or run on Linux ARM64.

After reviewing the owner-only configuration and granting separate cutover
approval, activate the already staged SHA:

```sh
ORCHESTRA_DEPLOY_APPROVED=1 ORCHESTRA_CUTOVER_APPROVED=1 \
  ./deploy/pixelbox/install.sh --activate --sha "$release_sha"
```

Activation is refused unless the host is Linux ARM64 and the reviewed PixelBox
paths and service user are in use. Static broker configuration and ownership
validation completes before any systemd state changes. The installer then
installs units that point at `/opt/orchestra/current`, replaces that symlink
with one atomic rename, restarts the coordinator and health timer, starts the
gateway when the release includes it, and verifies an authenticated backend
cycle before reporting success. A failed activation restores the prior symlink
and unit files, re-enables and health-checks the prior gateway when applicable,
and reports a hard failure if restoration is incomplete. It does not touch
Nextcloud, Docker, Tailscale, the Mac coordinator, or the tunnel. Enable the
tunnel separately only after its profile is verified.

`--apply` stages and activates in one invocation for compatibility. If it
creates templates or finds the dedicated broker config/token absent, it exits with status `3`
without changing systemd or the active symlink.

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
cd /opt/orchestra/current
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
cd /opt/orchestra/current
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

## 12. Optional HTTPS MCP gateway

The HTTPS MCP gateway is an optional second ingress for clients that cannot use
the Secure MCP Tunnel. It is not required for the tunnel path and must not be
exposed directly.

- Staging an HTTPS-capable release creates the `orchestra-mcp` group and
  `orchestra-mcp-http` service identity before installing gateway configuration.
- `/etc/orchestra/mcp-http.env` is owned by `orchestra-mcp-http`, group
  `orchestra-mcp`, mode `0600`; the installer preserves existing contents and
  never prints or replaces the token. Activation rejects a placeholder, a
  token shorter than 32 characters, duplicate token lines, non-token keys, or
  characters outside `[A-Za-z0-9._~+/=-]`.
- The service binds only `127.0.0.1:8090`, allows only IPv4 loopback network
  traffic, accepts only `AF_UNIX` and `AF_INET`, and reaches the coordinator
  socket through the `orchestra-mcp` group. Cloudflare ingress is the only
  intended external path.
- Idle MCP sessions expire after 15 minutes, a background sweeper removes stale
  sessions, concurrent sessions are capped, and shutdown/`DELETE` closes backend
  sockets.
- Rollback to a pre-HTTPS release disables and removes the gateway unit. The
  `orchestra-mcp-http` identity, group traverse ACL, and token file remain
  dormant; treat a security rollback as token-rotation event and remove those
  residual objects through an owner-approved cleanup.
- Staging an HTTPS-capable release requires Linux account tools (`getent`,
  `groupadd`, `useradd`) and POSIX ACL tools (`setfacl`, `getfacl`). Portable
  staging on another platform is limited to pre-HTTPS releases.
- The installer places a self-contained verifier at
  `/usr/local/lib/orchestra/http_gateway_verify.py`. Activation and rollback use
  that stable copy, not the target release's historical verifier. When rolling
  back to an older gateway release whose unit has the invalid
  `SocketBindAllow=inet:` form, the installer installs the invoking hardened
  unit instead; the historical code remains the rollback target.
- Verify with `deploy/pixelbox/verify_mcp_http.sh` and the Cloudflare ingress
  checks in [CLOUDFLARE-MCP.md](CLOUDFLARE-MCP.md).

## 13. Cutover gate

Do not cut over on unit tests alone. The owner must explicitly approve after all
of the following evidence is recorded:

1. `python3 -m unittest discover -s tests -v` passes on PixelBox.
2. `python3 -m compileall -q omcp tests scripts` passes.
3. `Config.load` validates the reviewed owner-only configuration.
4. `omcp.health` reports `ok` (or a documented, accepted degraded reason).
5. The dedicated tunnel profile passes `tunnel-client doctor` and `/readyz`.
6. ChatGPT discovers the configured OMCP tools and `bridge_status` reports
   `platform=linux`, `sandbox.backend=bubblewrap`, and the expected workers.
7. A harmless `routine_development` local Codex task starts without approval
   and returns a terminal result. An unclassified harmless task remains
   `approval_required`; approving it locally runs it and returns a terminal result.
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

After the tunnel is enabled, run
`ORCHESTRA_TUNNEL_HEALTH_URL=<verified-local-ready-url> ./deploy/pixelbox/verify.sh --live --sha <40-character-sha>`.
Default verification mode is staging-safe and may skip absent services; only
`--live` turns those skips into cutover failures. It also requires Linux ARM64
and proves that `current`, the release directory, and its marker identify the
expected exact SHA.

Only then route the production ChatGPT app to the PixelBox tunnel. Keep the Mac
service available for rollback until the owner records a successful observation
window.

### Rollback

For a software rollback, choose a previously staged exact SHA (normally the
target of `/opt/orchestra/previous`) and obtain explicit cutover approval:

```sh
readlink /opt/orchestra/previous
ORCHESTRA_DEPLOY_APPROVED=1 ORCHESTRA_CUTOVER_APPROVED=1 \
  /opt/orchestra/current/deploy/pixelbox/install.sh \
  --rollback --sha <previous-40-character-sha>
```

Rollback revalidates configuration against that release, atomically selects
it, installs the units present in that release, and restarts the coordinator
and health timer. When the target is a pre-HTTPS release, it stops/removes the
gateway and fails closed if the service remains enabled/active or a listener
remains on port 8090. When the target includes the gateway, successful
activation waits for the socket and an authenticated backend check; an
automatic restoration after failure re-enables and restarts the prior gateway,
then verifies its health, and reports a hard failure if that cannot be done.
The release being replaced becomes `previous`, so the operation is reversible.
An activation failure also attempts this restoration automatically.

Activation and rollback use the self-contained verifier at
`/usr/local/lib/orchestra/http_gateway_verify.py`, not a verifier supplied by
the target release. For multi-step rollback, invoke the reviewed/hardened
installer from the release you are leaving (or the verified `previous` release)
rather than an older target's historical installer.

Routing rollback remains a separate, explicitly approved operation:

1. Disable the PixelBox tunnel unit.
2. Point the private ChatGPT app back to the verified Mac tunnel.
3. Leave PixelBox releases, state, and logs intact for evidence.
4. Re-run the Mac health and harmless-task checks.
5. Do not delete PixelBox state until the owner authorises retention cleanup.

## 14. Security notes and limitations

- Never commit credentials. Never print a token, API key or capability value in
  a command, log, issue or task.
- The GitHub token is repository-scoped and readable only by the dedicated
  `orchestra-github` UID. Rotate it after suspected exposure and update
  `/etc/orchestra/github-dispatch/github-actions.token` without exposing it to
  the coordinator.
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
