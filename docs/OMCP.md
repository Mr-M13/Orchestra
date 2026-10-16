# Orchestra MCP task control (OMCP) v1

OMCP is a local MCP server that lets an authenticated ChatGPT tunnel submit and observe Orchestra worker tasks directly. It deliberately remains a narrow execution adapter: ChatGPT chooses work, a consequence class, an allowlisted repository profile, and workers; server-owned policy decides whether a configured standing authorization applies, otherwise a local human decides approval. OMCP validates the request, records intent durably, runs one worker at a time and reports evidence.

OMCP does not replace Desktop Commander or the GitHub Issue Mac Bridge. Keep both available until the direct path has passed tunnel and target-Mac end-to-end testing.

## Tool contract

| Tool | Permission | Purpose |
| --- | --- | --- |
| `bridge_status` | `status:read` | Health and task counts; retained for tunnel compatibility. |
| `submit_task` | `tasks:submit` | Durable submission and immediate acknowledgement. `request_id` is the idempotency key. |
| `submit_host_operation` | `host_operations:submit` | Request one owner-enabled built-in operation for an exact local commit. No command or arguments are accepted. |
| `task_status` | `tasks:read` | Status for one task. |
| `list_tasks` | `tasks:read` | Recent tasks, optionally filtered by status. |
| `task_result` | `results:read` | Bounded final worker message for a terminal task. |
| `worker_availability` | `workers:read` | Configured workers and the single execution slot. |
| `cancel_task` | `tasks:cancel` | Cancel queued work or request termination of a running process group. |
| `list_approvals` | `approvals:read` | Tasks waiting at the execution gate. |

Task states are `accepted`, `approval_required`, `starting`, `running`, `cancel_requested`, `succeeded`, `failed`, `cancelled`, `rejected` or `interrupted`. A successful worker exit is an execution result, not independent verification.

`submit_task` requires an explicit `repository_profile` in its published MCP schema. OMCP resolves that allowlisted identifier from its owner-only configuration and never accepts a path, URL, ref, or executable in a task request. Multi-repository deployments also reject an omitted profile at runtime before persisting a task, so older or non-validating clients cannot silently select the wrong repository. The controller retains the legacy implicit `default` profile only for direct single-repository callers. Each request declares a `consequence_class`; omission defaults to `unclassified` and therefore requires explicit approval. Retrying the same request and content returns the existing acknowledgement. Reusing a request ID with different content, profile, or consequence class fails closed.

`submit_host_operation` is a separate authority boundary. Its request contains only an idempotency key, an owner-configured operation ID, and a full 40-character commit SHA already present in the operation's configured repository. It cannot name an executable, script, path, environment variable, Docker image, Docker argument, Git ref, URL, or credential. Every host operation is persisted as `approval_required`; standing authorization never applies.

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

The caller's consequence label is not the security boundary. A matching grant selects a server-owned `routine_development_v1` capability profile. That profile denies reads outside the worktree except for Codex's minimal runtime paths, denies command network access, keeps Git metadata read-only, ignores user configuration and repository rules, and permits writes only below the grant's explicit `writable_paths`. Arbitrary instructions therefore cannot acquire deployment, push, merge, credential, security-configuration, or machine-wide destructive capabilities merely by claiming to be routine. OMCP does not use keyword classification as an authority check.

`writable_paths` are owner-reviewed relative directory roots, not task-supplied paths. Absolute paths, parent traversal, `.git`, `.codex`, duplicate paths, and punctuation that could change the permission-profile structure are rejected at configuration load. A read-only routine grant uses an empty list. Keep security policy, deployment, infrastructure, credential, release, and production configuration outside every standing grant. If a repository cannot be divided into non-sensitive write roots, do not configure a standing grant for edits; use explicit approval instead. Git metadata remains read-only in routine mode, so the worker produces commit-ready changes but cannot create, merge, update, or push refs. Integration remains an independently reviewed action.

When no exact standing grant matches, the task remains `approval_required`. The task record and audit event distinguish `standing` from `explicit` authorization and bind the consequence class into the idempotency digest.

An approval is an explicit gate before worker launch. It is not permission to bypass the worker sandbox or an assertion that the completed work is verified. Cancellation of a running worker sends `SIGTERM` to its isolated process group and is reconciled durably when the runner exits.

## Allowlisted host operations

Host operations are not worker capabilities. The Codex worker profile remains unchanged: command network is `false`, external credentials are `false`, Git metadata is read-only for standing-authorized routine work, and no Docker socket or host credential is exposed inside the worker. A host operation is instead a distinct controller task that shares the one global execution slot and requires a fresh local human approval.

The first built-in type is `delnavazan_mariadb_acceptance_v1`. The owner-only configuration binds an operation ID to the `delnavazan` repository profile, a full immutable `harness_commit`, and a bounded timeout. The caller separately supplies the exact `target_commit` to test. OMCP resolves both commits locally without a fetch, shows both SHAs in the local approval review, and creates a detached worktree at the pinned harness commit. This separation prevents a target commit from replacing the host-side harness scripts.

The host runner derives its command internally: pinned `platform/runtime/bin/run-all.sh`, followed by pinned `destroy.sh YES` in a cleanup path. It forwards no tunnel, OpenAI, proxy, GitHub, or other credential variables. A private Docker argument gate permits only the three cached harness images, exact Compose up/down forms, disposable containers, mounts below the operation's private state directory, and the PHP entrypoint. Pull, build, push, login, privileged mode, host networking, socket mounts, caller-selected images, and arbitrary Docker/Compose subcommands fail closed. Direct test containers receive `--network none`; the WordPress/MariaDB Compose network is overlaid as `internal: true`. WordPress remains loopback-only on a dynamically selected port, MariaDB is unpublished, and teardown targets only the harness's validated private state.

This operation is acceptance evidence only. It does not merge, push, deploy, contact production, use provider credentials, or prove anything beyond the exact target commit and harness run. Do not add a generic command host operation. A new operation type needs code, tests, documentation, a separate permission/risk review, and the same local approval boundary.

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
      "approvals:read",
      "host_operations:submit"
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
      "workers": ["codex"],
      "writable_paths": ["docs", "tests"]
    }
  ]
}
```

Enable the Delnavazan operation only in a multi-repository configuration that already has a `delnavazan` profile. Pin `harness_commit` to a reviewed Delnavazan commit containing the trusted runtime harness; updating that pin is an owner configuration change and should follow review. The operation type and fields are fixed—configuration cannot substitute a command or arguments:

```json
{
  "host_operations": {
    "delnavazan-mariadb-acceptance": {
      "type": "delnavazan_mariadb_acceptance_v1",
      "repository_profile": "delnavazan",
      "harness_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "timeout_seconds": 3600
    }
  }
}
```

Replace the illustrative all-`a` SHA above with the reviewed commit. Request the operation with a unique `request_id`, the configured operation ID, and a full local target commit. Review and decide it through the same owner-only local approval channel. The approval screen must show the intended repository, pinned harness SHA, and target SHA before approval. Retrieve the bounded summary with `task_result`; inspect the owner-only `host-operation.log` locally for full test output.

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

Approvals are local human decisions only. The tunnel exposes no `resolve_approval` tool. A human may use the interactive approval command above or the authenticated local approval interface described below.

## Approval notifications and interface

`macos/install-approval-services.sh` installs two small per-user LaunchAgents: a loopback-only approval portal and a notification projector. The projector observes the coordinator's durable state, records a separate owner-only delivery ledger, and submits one native notification per `(task_id, requested_at)` identifier. It records delivery intent before invoking the helper, retries failures with the same deterministic identifier, and removes delivered notifications when a request is approved, rejected, cancelled, removed, or expires. Reusing the identifier makes a crash between delivery and acknowledgement replace the existing notification rather than create a second one.

The native notification names the task, repository, and worker. Approve, Reject, and View details all open the local portal; no notification action itself possesses approval authority. The portal shows the complete instructions, repository/ref, worker, consequence class, expiry, and a risk summary. A decision requires a reason, a valid short-lived signed session, and a CSRF token. The portal sends the decision through the existing owner-only Unix socket and capability file. It never calls an MCP approval tool because none exists.

Install after deploying the exact reviewed source tree:

```sh
cd /path/to/deployed/Orchestra
./macos/install-approval-services.sh
```

The installer compiles an accessory macOS app, binds the portal to `127.0.0.1:8767`, and starts both agents. macOS may ask the signed-in user to allow notifications the first time. Logs are owner-only under `~/Library/Application Support/Orchestra/omcp-service/approval/`. The approval token, state, specs, socket, and audit remain owner-only under the configured state directory. Notification payloads contain no task instructions or bearer token; the helper reads the token file only after the user acts.

### Future PixelBox and Tailscale placement

The portal already fails closed for non-loopback bindings. Remote mode requires all of: an explicit non-wildcard interface address, `--allow-remote`, a server certificate/key, a client CA, TLS 1.3, and a valid client certificate. Authentication, session validation, CSRF, task lookup, and authorization remain server-side. The local approval socket is never bound, proxied, or exposed to the network; only the portal process may use it.

Remote mode is deliberately not installed or enabled by the Mac installer. Before moving this component to PixelBox, provision device-bound client identities, terminate TLS in the approval component itself or a comparably trusted local boundary, verify Tailscale ACLs as defense in depth, test revocation and expiry, and define how the remote component sends an authenticated decision to the Mac coordinator without forwarding the socket. Until that protocol is implemented and reviewed, PixelBox/iPhone approval is design-ready but unavailable.

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
3. Submit an `unclassified` harmless task with a unique `request_id`; confirm it remains `approval_required` and no worker starts, even if the caller sends `requires_approval: false`.
4. Review the task and approve it from the local interactive Mac terminal, observe `running`, then retrieve its terminal result.
5. Replay the original submission and confirm `idempotent_replay: true` with the same task ID.
6. Submit another harmless task and cancel it; confirm a terminal `cancelled` state.
7. In a non-production test configuration, add one exact `routine_development` standing grant with narrow `writable_paths`. Confirm the effective permission profile denies home-directory reads, network, Git metadata changes, and writes outside those roots. Confirm a matching task starts without entering `list_approvals`, while profile/worker mismatches and every protected consequence class remain `approval_required`.
8. Inspect the private audit log and worktree locally. Confirm the authorization mode, grant ID, and consequence class are recorded, while task instructions and worker output are absent.
9. Submit a harmless explicit-approval task. Confirm exactly one macOS notification appears, clicking each action opens the matching full-detail page, reject one request, approve another, and retrieve its terminal result through OMCP. Restart the coordinator and both approval agents with a pending request and confirm it remains visible without duplicate delivery or execution.
10. In a non-production configuration, enable the Delnavazan operation with a reviewed harness commit. Confirm unknown operation IDs, abbreviated or missing commits, caller command fields, uncached images, pulls, non-private mounts, host networking, and unapproved requests all fail closed. Approve one exact harmless commit locally; verify the audit binds both SHAs, the Docker network is internal, direct containers use no network, teardown runs, and the result/log contain no credentials. Cancel a second run and confirm terminal reconciliation. Do not use a production checkout or production data.

Do not disable the Bridge or Desktop Commander on the strength of unit tests alone. The final replacement gate requires the authenticated ChatGPT tunnel, target Mac, real Codex profile, Git worktree creation, completion/result retrieval and cancellation path to pass end to end.

## Recovery and limits

The detached runner writes a completion record even when the MCP server restarts. On startup OMCP reconciles completion records and live runner PIDs. A disappeared runner without a completion record becomes `interrupted`; OMCP does not retry it automatically.

V1 intentionally has one global execution slot shared by workers and host operations, local-file authoritative state, fixed per-process principal authority and no autonomous scheduling, retry, merge, deployment or outcome-verification decisions. Standing authorization permits only bounded routine worker execution; host operations always require local approval. Those later decisions remain with ChatGPT or a human operator.

### Migration to individual identity and roles

The fixed principal is confined to the authentication adapter: tool arguments contain no identity or permissions, authorization is centralized in `TaskController._authorize`, and every audit entry records the resolved principal. When Secure MCP Tunnel or a trusted front end can supply a cryptographically verified per-request identity, replace the fixed-principal resolver with a request-scoped principal resolver and map verified identities to the existing permission names. Pass that resolved principal into controller calls and audit entries; keep task storage, approval records, worker isolation, result handling and the MCP tool contract unchanged. Never migrate by trusting a caller-supplied tool argument or header that the local endpoint cannot verify.
