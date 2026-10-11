# Orchestra MCP task control (OMCP) v1

OMCP is a local MCP server that lets an authenticated ChatGPT tunnel submit and control Orchestra worker tasks directly. It deliberately remains a narrow execution adapter: ChatGPT chooses work, workers and approvals; OMCP validates the request, records intent durably, runs one worker at a time and reports evidence.

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
| `resolve_approval` | `approvals:decide` | Approve or reject a gated task with a recorded reason. |

Task states are `accepted`, `approval_required`, `starting`, `running`, `cancel_requested`, `succeeded`, `failed`, `cancelled`, `rejected` or `interrupted`. A successful worker exit is an execution result, not independent verification.

`submit_task` records the task and request-id mapping before starting a worker. Retrying the same request and content returns the existing acknowledgement. Reusing a request ID with different content fails closed.

## Security boundary

OMCP uses local stdio transport. The secure tunnel authenticates its remote client and owns access to the OMCP process. OMCP maps that process to one fixed principal from an owner-only configuration file; it does not accept identity, credentials or permissions in tool arguments.

The configured `authentication` value is an audit description of the tunnel mechanism, not a replacement for tunnel authentication. Do not expose the OMCP stdio process through an unauthenticated relay.

Each principal receives explicit permissions. Missing authority fails closed. Task input is treated as untrusted text and cannot alter sandbox or approval settings. Workers:

- invoke the Codex CLI directly, never a shell wrapper;
- reject approval- and sandbox-bypass flags;
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
    "identity": "chatgpt:orchestra-tunnel",
    "authentication": "authenticated-mcp-tunnel",
    "permissions": [
      "status:read",
      "tasks:submit",
      "tasks:read",
      "results:read",
      "workers:read",
      "tasks:cancel",
      "approvals:read",
      "approvals:decide"
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

Configure the existing secure tunnel to launch that command with the repository as its working directory. Keep the configuration and state paths distinct from the Mac Bridge configuration and state directory.

## Verification

Run the repository suite first:

```sh
cd /path/to/Orchestra
python3 -m unittest discover -s tests -v
python3 -m compileall -q mac_bridge omcp tests
```

Then verify through the configured tunnel, not only by calling the Python classes:

1. Discover tools and confirm all nine names above are present.
2. Call `bridge_status` and `worker_availability`.
3. Submit a harmless task with a unique `request_id` and `requires_approval: true`; confirm it remains `approval_required` and no worker starts.
4. Approve it, observe `running`, then retrieve its terminal result.
5. Replay the original submission and confirm `idempotent_replay: true` with the same task ID.
6. Submit another harmless task and cancel it; confirm a terminal `cancelled` state.
7. Inspect the private audit log and worktree locally. Confirm task instructions and worker output are absent from audit entries.

Do not disable the Bridge or Desktop Commander on the strength of unit tests alone. The final replacement gate requires the authenticated ChatGPT tunnel, target Mac, real Codex profile, Git worktree creation, completion/result retrieval and cancellation path to pass end to end.

## Recovery and limits

The detached runner writes a completion record even when the MCP server restarts. On startup OMCP reconciles completion records and live runner PIDs. A disappeared runner without a completion record becomes `interrupted`; OMCP does not retry it automatically.

V1 intentionally has one global execution slot, local-file authoritative state, fixed per-process principal authority and no autonomous scheduling, retry, merge, deployment or outcome-verification decisions. Those decisions remain with ChatGPT or a human operator.
