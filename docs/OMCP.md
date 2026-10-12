# Orchestra MCP task control (OMCP) v1

OMCP is a local MCP server that lets an authenticated ChatGPT tunnel submit and observe Orchestra worker tasks directly. It deliberately remains a narrow execution adapter: ChatGPT chooses work, a consequence class, an allowlisted repository profile, and workers; server-owned policy decides whether a configured standing authorization applies, otherwise a local human decides approval. OMCP validates the request, records intent durably, runs one worker at a time and reports evidence.

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

`submit_task` requires an explicit `repository_profile` in its published MCP schema. OMCP resolves that allowlisted identifier from its owner-only configuration and never accepts a path, URL, ref, or executable in a task request. Multi-repository deployments also reject an omitted profile at runtime before persisting a task, so older or non-validating clients cannot silently select the wrong repository. The controller retains the legacy implicit `default` profile only for direct single-repository callers. Each request declares a `consequence_class`; omission defaults to `unclassified` and therefore requires explicit approval. Retrying the same request and content returns the existing acknowledgement. Reusing a request ID with different content, profile, or consequence class fails closed.

## Security boundary

OMCP uses local stdio transport. Secure MCP Tunnel is a transport and access boundary, not a per-user identity bridge. OpenAI documents authentication of the tunnel client to the control plane, Platform organization-level Tunnels permissions, and association with ChatGPT workspaces. ChatGPT custom-app access is governed separately by workspace permissions and security restrictions. The tunnel documentation does not promise that an individual ChatGPT caller identity is passed to a local stdio process.

OMCP therefore supports a fixed process principal from an owner-only configuration file; it does not accept identity, credentials or permissions in tool arguments. This principal identifies the dedicated OMCP service/workspace authority, not the human who invoked the app. All callers who can use the app share that authority. OpenAI documents workspace-wide app availability for ChatGPT Business; per-role app access controls are documented for Enterprise/Edu. A fixed principal is acceptable only if the app remains in private development mode and its creator-only access is verified, or every member inside a separately verified workspace boundary has been explicitly authorised for the same OMCP permissions. In the initial deployment, both current members of the ChatGPT Business workspace are authorised to share this authority. Membership changes require a new access review before continued use. If the app or workspace boundary cannot be verified, do not expose OMCP mutating tools. Keep the tunnel on the read-only Bridge server.

The configured `authentication` value is an audit description only. Environment identity values bind the process to the configured principal; they do not prove the identity of an individual ChatGPT caller. A manually set marker does not make an unauthenticated relay safe.

Each principal receives explicit permissions. Missing authority fails closed. At startup, OMCP requires both `OMCP_TUNNEL_AUTHENTICATED=1` and an `OMCP_AUTHENTICATED_CALLER` value equal to the owner-only configured principal identity. These values are a local operator assertion that the dedicated tunnel/app boundary was verified; Secure MCP Tunnel does not supply or attest them. A launcher must not set them merely because the tunnel client has a valid runtime key or because a request arrived over the tunnel. Record the access-control evidence and review it whenever app or workspace membership changes. This implementation supports one fixed principal per dedicated tunnel process, not individual per-request user identities. See the [Secure MCP Tunnel permissions and association model](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels#permissions-and-access) and [workspace association guidance](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels#associate-tunnels-with-the-right-organizations-and-workspaces).

Every submitted task is evaluated by server-owned approval policy; the request cannot disable that policy. Only `routine_development` may match a standing authorization, and only for the exact repository profiles and workers named in the owner-only configuration. `production_deployment`, `destructive_operation`, `security_change`, and `unclassified` are never standing-authorizable. They always require a fresh explicit decision.

The remote MCP surface cannot decide explicit approvals. A local operator must run `python3 -m omcp.approval --config <config> --task-id <id> --decision approve|reject` from an interactive Mac terminal while the OMCP service is running. The command communicates with the active coordinator through an owner-only Unix socket and a per-process owner-only capability file in `state_dir`; neither is exposed as an MCP tool or passed to workers. It displays the task ID, request ID, fixed principal, worker, repository, consequence class, title, complete instructions, request time, expiry time and intended decision. The operator must type the exact decision plus task ID and enter a reason. Pending requests expire after `approval_ttl_seconds` (30 minutes by default); expired requests are rejected, audited and cannot start a worker. The local approval actor is separate from the tunnel submitter. Task input is untrusted and cannot alter sandbox or approval settings. Workers:

- invoke only the canonical Codex CLI executable, resolved and verified at configuration load and again by the runner; configured CLI arguments and wrappers are rejected;
- receive only the explicitly permitted `PATH`, `HOME`, `TMPDIR`, `LANG`, `LC_ALL` and `CODEX_HOME` variables, plus `GIT_TERMINAL_PROMPT=0`; tunnel credentials, caller markers and proxy variables are not forwarded;
- run in `workspace-write` with interactive escalation disabled;
- use a fresh `omcp/task-<id>` Git branch and worktree from the configured base ref;
- retain prompts, logs, results and worktrees in the private state directory for inspection.

The state directory and its files are owner-only. State updates are atomic. `audit.jsonl` is append-only and fsynced for each event; it records principal, authentication context, action, outcome and safe metadata, but not task instructions or worker output. Result retrieval is size-bounded.

## Repository profiles

V1 configuration remains valid and describes one implicit `default` profile. A multi-repository deployment adds `repository_profiles` and `default_profile`. Every profile binds a stable ID to one GitHub repository, one existing local checkout, and one `origin/<branch>` base ref. OMCP validates the selected ID before persisting a task, records the resolved profile, repository, and ref in task status and local approval review, checks the checkout's HTTPS GitHub origin at worktree creation, and creates the worktree under a profile-specific private state path.

The current global execution limit remains one. Repository profiles prepare task isolation and auditing for independent lanes; they do not enable concurrent execution. Any future lane limit must retain per-task approval, distinct worktrees and branches, durable shared-state admission, cancellation and restart recovery tests before it can increase above one.

## Standing authorization

Standing authorization is an owner-configured execution grant, not a caller-supplied approval. A grant has a stable ID and is bound to the fixed principal process, one consequence class, named repository profiles, and named workers. The initial policy permits grants only for `routine_development`. Configuration loading rejects attempts to grant standing authority for production deployment, destructive operations, security changes, or unclassified work.

The fixed workspace principal is responsible for accurate consequence classification. Deliberately labelling consequential work as routine is an authorization violation; OMCP does not pretend that keyword matching can reliably classify arbitrary natural-language instructions. Standing-authorized workers remain confined to isolated worktrees, cannot use interactive escalation, and do not receive merge or deployment authority. Completed changes still require independent review before integration.

When no exact standing grant matches, the task remains `approval_required`. The task record and audit event distinguish `standing` from `explicit` authorization and bind the consequence class into the idempotency digest.

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
  "max_result_bytes": 65536,
  "approval_ttl_seconds": 1800,
  "standing_authorizations": [
    {
      "id": "routine-development-v1",
      "consequence_class": "routine_development",
      "repository_profiles": ["orchestra"],
      "workers": ["codex"]
    }
  ]
}
```

```sh
chmod 600 "$HOME/Library/Application Support/Orchestra/omcp.json"
cd /Users/ACCOUNT/Projects/Orchestra
python3 -m omcp.server --config "$HOME/Library/Application Support/Orchestra/omcp.json"
```

Only configure a dedicated OMCP profile after verifying and recording the intended ChatGPT workspace membership/app access and Platform organization Tunnels Use access. The user of the app is not individually identified to the stdio server; all invocations run as the fixed principal. In ChatGPT Business, publish the app only when every current workspace member is authorised for the same submit/read/cancel authority, and repeat that review whenever membership changes. Then launch a dedicated server process with the repository as its working directory and these environment values set by the local operator-controlled launcher:
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
3. Submit an `unclassified` harmless task with a unique `request_id`; confirm it remains `approval_required` and no worker starts, even if the caller sends `requires_approval: false`.
4. Review the task and approve it from the local interactive Mac terminal, observe `running`, then retrieve its terminal result.
5. Replay the original submission and confirm `idempotent_replay: true` with the same task ID.
6. Submit another harmless task and cancel it; confirm a terminal `cancelled` state.
7. In a non-production test configuration, add one exact `routine_development` standing grant. Confirm a matching task starts without entering `list_approvals`, while profile/worker mismatches and every protected consequence class remain `approval_required`.
8. Inspect the private audit log and worktree locally. Confirm the authorization mode, grant ID, and consequence class are recorded, while task instructions and worker output are absent.

Do not disable the Bridge or Desktop Commander on the strength of unit tests alone. The final replacement gate requires the authenticated ChatGPT tunnel, target Mac, real Codex profile, Git worktree creation, completion/result retrieval and cancellation path to pass end to end.

## Recovery and limits

The detached runner writes a completion record even when the MCP server restarts. On startup OMCP reconciles completion records and live runner PIDs. A disappeared runner without a completion record becomes `interrupted`; OMCP does not retry it automatically.

V1 intentionally has one global execution slot, local-file authoritative state, fixed per-process principal authority and no autonomous scheduling, retry, merge, deployment or outcome-verification decisions. Standing authorization permits only bounded routine worker execution; those later decisions remain with ChatGPT or a human operator.

### Migration to individual identity and roles

The fixed principal is confined to the authentication adapter: tool arguments contain no identity or permissions, authorization is centralized in `TaskController._authorize`, and every audit entry records the resolved principal. When Secure MCP Tunnel or a trusted front end can supply a cryptographically verified per-request identity, replace the fixed-principal resolver with a request-scoped principal resolver and map verified identities to the existing permission names. Pass that resolved principal into controller calls and audit entries; keep task storage, approval records, worker isolation, result handling and the MCP tool contract unchanged. Never migrate by trusting a caller-supplied tool argument or header that the local endpoint cannot verify.
