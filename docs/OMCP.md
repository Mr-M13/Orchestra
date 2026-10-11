# Orchestra MCP task control (OMCP) v1

OMCP is a local MCP server that lets an authenticated ChatGPT tunnel submit and observe Orchestra worker tasks directly. It deliberately remains a narrow execution adapter: ChatGPT chooses work and workers; a local human decides approvals. OMCP validates the request, records intent durably, runs one worker at a time and reports evidence.

OMCP does not replace Desktop Commander or the GitHub Issue Mac Bridge. Keep both available until the direct path has passed tunnel and target-Mac end-to-end testing.

## Tool contract

| Tool | Permission | Purpose |
| --- | --- | --- |
| `bridge_status` | `status:read` | Health and task counts; retained for tunnel compatibility. |
| `submit_task` | `tasks:submit` | Durable submission and immediate acknowledgement. `request_id` is the idempotency key. |
| `task_status` | `tasks:read` | Status for one task. |
| `list_tasks` | `tasks:read` | Recent tasks, optionally filtered by status. |
| `task_result` | `results:read` | Bounded final worker message for a terminal task. |
| `worker_availability` | `workers:read` | Configured workers and the single execution slot. |
| `cancel_task` | `tasks:cancel` | Cancel queued work or request termination of a running process group. |
| `list_approvals` | `approvals:read` | Tasks waiting at the execution gate. |

Task states are `accepted`, `approval_required`, `starting`, `running`, `cancel_requested`, `succeeded`, `failed`, `cancelled`, `rejected` or `interrupted`. A successful worker exit is an execution result, not independent verification.

`submit_task` records the task and request-id mapping before starting a worker. Retrying the same request and content returns the existing acknowledgement. Reusing a request ID with different content fails closed.

## Security boundary

OMCP uses local stdio transport. Secure MCP Tunnel is a transport and access boundary, not a per-user identity bridge. OpenAI documents authentication of the tunnel client to the control plane, Platform organization-level Tunnels permissions, and association with ChatGPT workspaces. ChatGPT custom-app access is governed separately by workspace permissions and security restrictions. The tunnel documentation does not promise that an individual ChatGPT caller identity is passed to a local stdio process.

OMCP therefore supports a fixed process principal from an owner-only configuration file; it does not accept identity, credentials or permissions in tool arguments. This principal identifies the dedicated OMCP service/workspace authority, not the human who invoked the app. All callers who can use the app share that authority. OpenAI documents workspace-wide app availability for ChatGPT Business; per-role app access controls are documented for Enterprise/Edu. A fixed principal is acceptable only if the app remains in private development mode and its creator-only access is verified, or if a separately verified access boundary limits use to the intended operator(s). Do not publish a fixed-principal app to a multi-member Business workspace: every member allowed to use that app would share the same OMCP authority. If the app access boundary cannot be verified, do not expose OMCP mutating tools. Keep the tunnel on the read-only Bridge server.

The configured `authentication` value is an audit description only. Environment identity values bind the process to the configured principal; they do not prove the identity of an individual ChatGPT caller. A manually set marker does not make an unauthenticated relay safe.

Each principal receives explicit permissions. Missing authority fails closed. At startup, OMCP requires both `OMCP_TUNNEL_AUTHENTICATED=1` and an `OMCP_AUTHENTICATED_CALLER` value equal to the owner-only configured principal identity. These values are a local operator assertion that the dedicated tunnel/app boundary was verified; Secure MCP Tunnel does not supply or attest them. A launcher must not set them merely because the tunnel client has a valid runtime key or because a request arrived over the tunnel. Record the access-control evidence and review it whenever app or workspace membership changes. This implementation supports one fixed principal per dedicated tunnel process, not individual per-request user identities. See the [Secure MCP Tunnel permissions and association model](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels#permissions-and-access) and [workspace association guidance](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels#associate-tunnels-with-the-right-organizations-and-workspaces).

Every submitted task requires human approval; the request cannot disable the gate. The remote MCP surface cannot decide approvals. A local operator must run `python3 -m omcp.approval --config <config> --task-id <id> --decision approve|reject` from an interactive Mac terminal and enter a reason. The local approval actor is separate from the tunnel submitter. Task input is untrusted and cannot alter sandbox or approval settings. Workers:

- invoke only the canonical Codex CLI executable, resolved and verified at configuration load and again by the runner; configured CLI arguments and wrappers are rejected;
- receive only the explicitly permitted `PATH`, `HOME`, `TMPDIR`, `LANG`, `LC_ALL` and `CODEX_HOME` variables, plus `GIT_TERMINAL_PROMPT=0`; tunnel credentials, caller markers and proxy variables are not forwarded;
- run in `workspace-write` with interactive escalation disabled;
- use a fresh `omcp/task-<id>` Git branch and worktree from the configured base ref;
- retain prompts, logs, results and worktrees in the private state directory for inspection.

The state directory and its files are owner-only. State updates are atomic. `audit.jsonl` is append-only and fsynced for each event; it records principal, authentication context, action, outcome and safe metadata, but not task instructions or worker output. Result retrieval is size-bounded.

An approval is an explicit gate before worker launch. It is not permission to bypass the worker sandbox or an assertion that the completed work is verified. Cancellation of a running worker sends `SIGTERM` to its isolated process group and is reconciled durably when the runner exits.

## Configure on the worker Mac

Use Python 3.11 or newer and a current Codex CLI. Create a state directory outside the project checkout and an owner-only configuration outside the repository, for example `~/Library/Application Support/Orchestra/omcp.json`:

```json
{
  "repository": "Mr-M13/Orchestra",
  "project_root": "/Users/ACCOUNT/Projects/Orchestra",
  "state_dir": "/Users/ACCOUNT/Library/Application Support/Orchestra/omcp-state",
  "base_ref": "origin/main",
  "principal": {
    "identity": "orchestra:dedicated-workspace",
    "authentication": "fixed-principal-behind-secure-mcp-tunnel",
    "permissions": [
      "status:read",
      "tasks:submit",
      "tasks:read",
      "results:read",
      "workers:read",
      "tasks:cancel",
      "approvals:read"
    ]
  },
  "workers": {
    "ina": {
      "command": ["codex"],
      "codex_home": "/Users/ACCOUNT/.codex-ina"
    },
    "codex": {
      "command": ["codex"],
      "codex_home": "/Users/ACCOUNT/.codex"
    }
  },
  "max_task_bytes": 65536,
  "max_result_bytes": 65536
}
```

```sh
chmod 600 "$HOME/Library/Application Support/Orchestra/omcp.json"
cd /Users/ACCOUNT/Projects/Orchestra
python3 -m omcp.server --config "$HOME/Library/Application Support/Orchestra/omcp.json"
```

Only configure a dedicated OMCP profile after verifying and recording the intended ChatGPT workspace membership/app access and Platform organization Tunnels Use access. The user of the app is not individually identified to the stdio server; all invocations run as the fixed principal. In ChatGPT Business, do not publish the app to the multi-member workspace unless every member should have the same submit/read/cancel authority. Then launch a dedicated server process with the repository as its working directory and these environment values set by the local operator-controlled launcher:
```sh
OMCP_TUNNEL_AUTHENTICATED=1
OMCP_AUTHENTICATED_CALLER=orchestra:dedicated-workspace
```
The value must exactly match the configured principal identity. These environment values are not caller authentication; they only bind the process to the configured fixed principal after the operator has verified the access boundary. Do not rely on the control-plane runtime key or audit `authentication` label as proof of a ChatGPT user's identity. Keep the OMCP config and state paths distinct from the Mac Bridge config and state directory. Preserve the existing read-only Bridge tunnel until the dedicated app/workspace access boundary and live lifecycle tests below pass.

Approvals are local human decisions only. The tunnel exposes no `resolve_approval` tool. A human on the Mac runs the interactive approval command above after reviewing the task.

## Verification

Run the repository suite first:

```sh
cd /path/to/Orchestra
python3 -m unittest discover -s tests -v
python3 -m compileall -q mac_bridge omcp tests
```

Then verify through the configured tunnel, not only by calling the Python classes:

1. Discover tools and confirm all eight names above are present.
2. Call `bridge_status` and `worker_availability`.
3. Submit a harmless task with a unique `request_id`; confirm it remains `approval_required` and no worker starts, even if the caller sends `requires_approval: false`.
4. Review the task and approve it from the local interactive Mac terminal, observe `running`, then retrieve its terminal result.
5. Replay the original submission and confirm `idempotent_replay: true` with the same task ID.
6. Submit another harmless task and cancel it; confirm a terminal `cancelled` state.
7. Inspect the private audit log and worktree locally. Confirm task instructions and worker output are absent from audit entries.

Do not disable the Bridge or Desktop Commander on the strength of unit tests alone. The final replacement gate requires the authenticated ChatGPT tunnel, target Mac, real Codex profile, Git worktree creation, completion/result retrieval and cancellation path to pass end to end.

## Recovery and limits

The detached runner writes a completion record even when the MCP server restarts. On startup OMCP reconciles completion records and live runner PIDs. A disappeared runner without a completion record becomes `interrupted`; OMCP does not retry it automatically.

V1 intentionally has one global execution slot, local-file authoritative state, fixed per-process principal authority and no autonomous scheduling, retry, merge, deployment or outcome-verification decisions. Those decisions remain with ChatGPT or a human operator.
