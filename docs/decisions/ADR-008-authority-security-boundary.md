# ADR-008: Authenticated independent-authority security boundary

- **Status:** Shadow-only Phase 3A implementation. Not activated, not deployed.
- **Base:** `main` containing merged PR #44 (Phase 2 prototype).
- **Implementation:** `orchestra/authority_service.py`,
  `orchestra/authority_client.py`, `orchestra/peercred.py`,
  `tests/test_authority_service.py`, `scripts/linux/authority_boundary_test.py`.
- **Related:** [ADR-006](ADR-006-provider-independent-agent-architecture.md),
  [ADR-007](ADR-007-execution-authority-prototype.md).

## 1. Context

ADR-007 implemented durable grants, approvals, verification and recovery as an
in-process Python library. Its `Principal(kind=...)` value was asserted by the
caller. That was sufficient for contract tests, but it did not create a security
boundary: any coordinator process could claim to be an approver or verifier.

Phase 3A turns that library into a small standalone authority service. The
service runs under a dedicated Unix identity and derives every caller's role
from the kernel identity of the connected Unix socket. The coordinator cannot
select a principal, role, uid or policy in the request body.

The service remains **shadow-only**. Nothing in OMCP, approvals, workers,
systemd, MCP Tunnel, Nextcloud, Docker or PixelBox production configuration is
imported, changed, restarted or activated by this change.

## 2. Decision

### 2.1 Separate process and identity

The authority runs as one process under a dedicated UID (for example
`orchestra-authority`). It has:

- no provider credentials in its environment;
- no repository-write credentials;
- no deployment credentials;
- write access only to its state directory and runtime socket directory;
- read access to its own source and root-owned configuration.

The example systemd unit is deliberately not installed. It uses
`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateNetwork`,
`RestrictAddressFamilies=AF_UNIX`, `CapabilityBoundingSet=`,
`MemoryDenyWriteExecute`, `UMask=0077` and an empty credential environment.

### 2.2 Kernel-derived peer identity

Every accepted Unix socket connection is immediately inspected with:

- Linux: `SO_PEERCRED` (`struct ucred`, including peer pid/uid/gid);
- macOS: `LOCAL_PEERCRED` (`struct xucred`, uid/gid; no peer pid);
- any other platform: fail closed, no unauthenticated fallback.

The server maps the kernel UID to exactly one configured role. Unknown UIDs are
rejected before reading a request and before any authority operation. A
caller-supplied `role`, `uid`, `principal`, policy, standing authorisation or
authority id is rejected at the envelope/payload boundary.

The service constructs `Principal("uid:<kernel-uid>", role)` internally. The
Phase 2 trusted-human, trusted-verifier and trusted-operator sets are built from
the owner configuration, not from a request.

### 2.3 Independent roles

| Role | May do | Must not do |
| --- | --- | --- |
| coordinator | propose, request execution, consume grant, cancel, read state | approve, verify, reconcile unknown, mutate policy |
| human approver | approve/deny proposals, read state | propose, execute, verify, report, reconcile, mutate policy |
| verifier | read state, submit independent verification | approve, propose, execute, report, reconcile |
| worker | record dispatch receipt and untrusted report | approve, verify, consume grants, propose, reconcile |
| operator | reconcile unknown outcomes, sweep, cancel, read audit | approve, verify, report, mutate policy |
| unconfigured UID | nothing | any request |

Exactly one role per UID is enforced. The authority uid may not also be a client
peer uid. The coordinator, approver and verifier must therefore be distinct OS
identities.

### 2.4 Protocol

One request per connection, newline-delimited canonical JSON:

```json
{
  "version": 1,
  "request_id": "client-generated-safe-id",
  "operation": "request_execution",
  "payload": { "...": "operation-specific data" }
}
```

A response is either:

```json
{"version":1,"request_id":"...","ok":true,"result":{...}}
```

or:

```json
{"version":1,"request_id":"...","ok":false,
 "error":{"code":"operation_not_permitted","message":"..."}}
```

Limits: bounded message size, bounded request id, bounded response size, fixed
operation allowlist, one role check before dispatch, no traceback leakage.

### 2.5 Authenticated requests, idempotency and replay

Durable `rpc_requests` rows are keyed by `(peer_uid, request_id)` and contain the
operation, canonical payload digest, status and cached response.

- same uid + same request id + same operation/payload: return the cached
  response, never re-execute;
- same uid + same request id + different parameters: `request_id_conflict`;
- identical request still running: `request_in_progress`;
- crash before the response cache is written: startup marks the row `unknown`;
  a retry returns `request_outcome_unknown` and tells the caller to query
  durable state. It is never silently re-executed;
- different UIDs using the same request id are isolated from each other.

Phase 2's grant idempotency and single-use consumption remain the second line of
defence. A new request id using an already-consumed grant is rejected with
`grant_replay`; the same request id returns the cached response and does not
consume again.

### 2.6 Durable state and recovery

The service reuses the Phase 2 SQLite database through
`PrototypeExecutionAuthority`. It adds only service-scoped tables for RPC
idempotency and service audit events. On startup it:

1. verifies the process UID and private directories;
2. acquires an exclusive lock (`flock`) and refuses a second instance;
3. opens the authority database and verifies the bound policy digest;
4. runs stale-attempt/expired-grant recovery;
5. marks unrecorded in-progress RPCs `unknown`;
6. binds the socket, removes only a genuinely stale socket owned by the
   authority UID;
7. writes a `service_started` audit event.

On SIGTERM/SIGINT it stops accepting, joins request threads, writes
`service_stopping`, unlinks only the socket inode it owns, releases the lock and
closes the authority. SIGKILL leaves a stale socket; the next start detects that
it is not live and replaces it.

### 2.7 Ownership and filesystem permissions

| Object | Owner | Group | Mode | Notes |
| --- | --- | --- | --- | --- |
| `/var/lib/orchestra-authority` | authority | authority | `0700` | no client traversal |
| `authority.sqlite3`, `-wal`, `-shm` | authority | authority | `0600` | audit and lifecycle state |
| `/run/orchestra-authority` | authority | clients | `0750` | clients may traverse, not write |
| `authority.sock` | authority | clients | `0660` | clients connect; cannot unlink/replace |
| `authority.lock` | authority | authority | `0600` | one instance |
| `/etc/orchestra/authority.json` | root | authority | `0640` | owner policy, not readable by clients |
| source tree | root | root | `0755`/`0644` | read-only to all |

The service refuses to start if:

- its effective UID differs from `service_uid`;
- it runs as UID 0;
- the state directory is not owned by it or has group/other access;
- the runtime directory has other access, group write, or the wrong client
  group;
- the socket path is a symlink or non-socket;
- another instance holds the lock or a live socket;
- the database/config file ownership or config write permissions are unsafe.

`/proc/<authority-pid>/environ` is not readable by worker or coordinator UIDs.
The service is launched with an empty inherited environment plus `PATH`,
`PYTHONPATH`, `PYTHONDONTWRITEBYTECODE`, `LANG` and `HOME`; no provider
credentials are passed.

## 3. Demonstrated security properties

### 3.1 In-process protocol and unit evidence

`python3 -m unittest tests.test_authority_service -v` (21 tests) covers:

- real kernel peer credentials match the current identity;
- config validation, one role per UID, service/peer UID separation;
- server-derived role in health and lifecycle operations;
- coordinator denial for approval, verification and operator recovery;
- worker denial for approval/verification and approver denial for verification;
- caller-supplied identity/policy fields rejected;
- unknown operation and unconfigured peer rejected;
- identical replay cached; conflicting replay rejected;
- in-progress request never re-executed and recovered as unknown;
- non-socket and symlink socket replacement refused;
- stale socket replaced; live second instance refused;
- database mode `0600` and graceful socket/lock cleanup;
- malformed/oversized requests rejected without internal details.

These tests use a test credential reader because the host test process cannot
create arbitrary UIDs. They are **not** the boundary evidence on their own.
The full repository suite also passes: `Ran 282 tests ... OK`.

### 3.2 Real separate-UID Linux evidence

`scripts/linux/authority_boundary_test.py` was executed as root inside a
disposable `python:3.13-slim` Linux container. It created real Unix users and
groups and ran every process through `setpriv` with `--no-new-privs`, an empty
bounding/inheritable/ambient capability set and a cleared environment:

```text
orchestra-authority   uid 2101  gid 2101  supplementary clients
orchestra-coordinator uid 2102  gid 2100
orchestra-approver    uid 2103  gid 2100
orchestra-verifier    uid 2104  gid 2100
orchestra-worker      uid 2105  gid 2100
orchestra-operator    uid 2106  gid 2100
orchestra-rogue       uid 2107  gid 2100  (not in config)
```

Result: **88 PASS checks**, including:

- service runs as uid 2101; `SO_PEERCRED` reports coordinator uid 2102;
- rogue uid 2107 is rejected as an unauthorised socket peer;
- coordinator cannot approve, verify, update policy, or perform operator
  recovery;
- approver cannot act as verifier; worker cannot approve or verify;
- coordinator cannot read, write or list the database/state directory;
- worker cannot open the database or read the authority process environment;
- operator can read append-only service audit events; coordinator cannot;
- coordinator and worker cannot read the root-owned policy config; coordinator
  cannot modify it;
- coordinator cannot unlink the socket;
- caller-supplied role and policy fields are rejected;
- full contract -> proposal -> approval -> grant -> consumption -> dispatch ->
  untrusted report -> independent verification lifecycle succeeds;
- worker success remains `reported`/unverified until verifier uid 2104 acts;
- idempotent and conflicting request replays behave as specified;
- a new request id replaying a consumed grant is rejected;
- a simulated crash between durable consumption and response caching recovers
  the RPC as `unknown`; retry is refused and the grant remains consumed exactly
  once;
- graceful restart preserves the verified task;
- non-socket and symlink socket replacements are refused without deletion;
- stale sockets after SIGKILL are replaced safely; verified state survives;
- a second authority instance is refused;
- graceful shutdown removes socket and lock; unclean crash leaves a recoverable
  stale socket.

The container is disposable and runs no Orchestra production service. Docker is
used only as the Linux kernel environment for real UID tests; no Docker
configuration or production container was changed. Reproduce with:

```bash
docker run --rm -v "$PWD:/repo:ro" -w /repo python:3.13-slim \
  python3 scripts/linux/authority_boundary_test.py
```

The script requires root inside the container so it can create the seven
disposable UIDs with `useradd`/`groupadd` and launch processes with `setpriv`.
It exits `2` rather than claiming success if it is not running as root on Linux.

## 4. What is still not proven

1. **Not activated.** No live OMCP dispatch, approval, worker, PixelBox,
   MCP Tunnel, Nextcloud or Docker production path uses this service.
2. **Local Unix socket only.** There is no remote transport, mTLS, host
   authentication, coordinator election or multi-host deployment.
3. **UID-level granularity.** Every process running as a role UID inherits that
   role. The boundary protects roles from each other, not compromised processes
   within the same role. SELinux/AppArmor label verification is not implemented.
4. **Unsigned grants.** Phase 2 grants remain unsigned; database write access is
   authority-equivalent. The boundary prevents ordinary coordinator/worker
   access, not root or a compromised authority process.
5. **Root bypass.** Root can read/write the DB, ptrace the service or change the
   config. The production unit reduces service privilege but cannot defend
   against host root compromise.
6. **No credential broker.** The harness proves no provider credentials are
   passed, but production secret injection, rotation and brokering remain
   deferred.
7. **Verification evidence.** The authority binds verifier identity, report,
   proposal, attempt and evidence references, but does not independently fetch
   external evidence. The trusted verifier UID remains a trust anchor.
8. **Semantic approval.** Approval classification is structural (consequence,
   approval class, operations and scope). A trusted human/verifier UID can still
   approve or verify work that is harmful but structurally valid. UID separation
   does not validate intent.
9. **Python service surface.** CPython, the standard library and SQLite are part
   of the trusted computing base. The example systemd hardening has not been
   exercised on the production host or under seccomp policy tests.
10. **Host coverage.** Real separate-UID evidence is Linux-container evidence.
    macOS peer credentials are unit-tested with `LOCAL_PEERCRED`, but the macOS
    boundary and the target PixelBox host have not been tested.
11. **Availability.** SQLite single-writer behaviour, disk exhaustion, request
    cache growth, thread exhaustion and active-attempt cancellation on service
    death are not production-hardened.

## 5. Rollback

This change is additive:

- new service/client/peercred modules;
- new tests and Linux scripts;
- new example deployment assets (not installed or enabled);
- the only edit to existing Phase 2 code is public dictionary adapters;
- no OMCP, worker, systemd, tunnel, Nextcloud, Docker or PixelBox change.

Rollback is to stop and remove the shadow service process and revert the branch.
No production database migration, socket, credential or service was created.

## 6. Verdict

**PASS** for the shadow-only Phase 3A authenticated authority boundary, based on
88 real separate-UID checks in a disposable Linux environment plus the
in-process protocol suite.

**BLOCKED** for production activation and Phase 3B until the remaining items in
section 4 are reviewed and the authority is exercised on the intended host with
its production hardening, credential model and rollback rehearsal.
