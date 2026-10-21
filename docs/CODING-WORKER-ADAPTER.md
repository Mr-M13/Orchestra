# Phase 3B provider-independent coding-worker adapter

`DisposableCodingWorkerAdapter` is an inactive implementation of the existing
seven-operation `AgentAdapter` contract. It runs only the bundled fake harness
in an adapter-owned temporary workspace. It is not imported by OMCP and has no
systemd unit, provider client, credential access, network access or production
dispatch path.

## Lifecycle

The adapter writes an adapter-owned durable job record before starting a fixed
subprocess. The only executable is the current Python interpreter and the only
module is `orchestra.disposable_coding_harness`; neither the task nor the grant
can select a command. A task moves through queued, running, and a terminal
worker outcome. The worker result remains an `untrusted_worker_report` and
`completion_verified` is always false.

Cancellation sends `SIGTERM` to the disposable worker's process group, checks
the recorded stable process identity before signalling, and escalates to
`SIGKILL` only for that same group. A missing process without a terminal result
is `unknown`; recovery returns `ambiguous` and never retries it. `collect()`
recomputes artifact size and SHA-256 before constructing an `ExecutionReport`,
then removes the disposable workspace and harness input.

## Process identity

A PID alone is never treated as worker identity. The adapter persists a
`ProcessIdentity` containing the PID, process group, effective UID and a
kernel-derived start time:

- **Linux:** `/proc/<pid>/stat` process group and starttime, plus the effective
  UID from `/proc/<pid>/status`.
- **macOS:** `proc_pidinfo(PROC_PIDTBSDINFO)` from `libproc`, using `pbi_pid`,
  `pbi_pgid`, `pbi_uid`, `pbi_start_tvsec` and `pbi_start_tvusec`.

No mutable `/bin/ps` command text is part of identity. On macOS this is
important because the Python framework launcher changes its visible command
from `.../bin/python3` to `.../Resources/Python.app/Contents/MacOS/Python`
during interpreter startup. Identity is captured after spawn with a bounded
retry and must remain identical across that transition.

If the kernel identity is unavailable, malformed or different, status becomes
`unknown`, cancellation is refused, and recovery returns `ambiguous`. There is
no weaker `ps` fallback for authorisation, cancellation or recovery.

Job records written by the current adapter use record version 2 and store
`process_identity` as a structured object. Version 1 records that have already
launched a process (including the former Darwin command hash in `pid_start`)
are never adopted: status becomes `unknown` and recovery returns `ambiguous`.
A version 1 record in `intent_prepared` or `consumed` may still be recovered
through the authority because no process has been launched; it is upgraded to
version 2 before launch.

## Phase 3C authenticated authority boundary

The adapter accepts an injected `ExecutionAuthorityClient`. It uses the actual
Phase 3A Unix-socket operations:

```python
validate_grant(task, grant, *, adapter_id, runtime, request_id=None) -> dict
consume_grant(task, grant, *, adapter_id, runtime, request_id=None) -> dict
request_status(target_request_id, *, request_id=None) -> dict
```

Before consumption, the adapter creates an owner-only attempt directory and
atomically persists the exact grant, proposal binding, a random consume request
ID, and an `intent_prepared` state. The authority authenticates the coordinator
from Unix peer credentials and atomically consumes the durable grant with the
exact TaskSpec, proposal, attempt, adapter and runtime binding. The adapter
validates and persists the returned consumption receipt before recording a
`launching` state and starting the fixed disposable harness.

The random consume request ID is unique to that durable intent. It is never
derived only from the grant: two adapter instances therefore cannot mistake an
authority response-cache hit for permission to launch twice. A same-intent
retry first queries `request_status`; an authority success restores the receipt,
`not_found` permits the original consume request, and an unknown or in-progress
outcome fails closed. An OS file lock serializes dispatch and recovery for
adapter processes sharing one state root; authority atomic consumption protects
adapters using independent state roots.

The authority owns grant issuance, approval, authenticated identity, atomic
single-use consumption and durable attempt state. The adapter cannot issue a
grant or make an approval decision. Worker reports remain untrusted, and only a
separate verifier identity can mark the authority attempt verified.

## Crash and cancellation semantics

| Durable adapter state | Recovery action |
|---|---|
| No intent | No execution occurred; a caller may start normally with a valid grant. |
| `intent_prepared` | Query the exact consume request. Consume only when the authority proves it was never seen. |
| `consumed` | Launch the fixed harness once from the persisted grant and task binding. |
| `launching` | Mark unknown and never retry: the process-start outcome is uncertain. |
| `running` | Adopt only a live process whose PID, process group, UID and kernel start time match the persisted `ProcessIdentity`. |
| Terminal result | Recompute bounded artifact metadata and return the same durable untrusted report. |

Cancellation targets the recorded process group. A cancellation race or a
missing process without a terminal result remains unknown/uncertain; it is not
converted into success and does not permit grant reuse.

## Provider-independent configuration

The example in `examples/coding-worker-adapters.json` keeps agent, model
provider, coding harness, runtime and adapter identity separate. Only the
disposable fake agent is operational. Codex, DeepSeek/OpenCode, Claude and
Gemini entries are examples marked non-operational; implementing one requires a
new harness binding and security review, not a change to the authority state
machine or shared contracts.

## Limitations

- This is a Unix test harness. Linux identity uses `/proc` starttime, process
  group and UID. macOS identity uses `proc_pidinfo` microsecond start time,
  process group and UID; no command text is trusted. An unavailable, malformed
  or mismatched identity fails closed, and version 1 launched records are not
  adopted. Windows is unsupported.
- It simulates one bounded file change and does not clone or modify a repository.
- It provides no OS sandbox beyond a private temporary directory; production
  isolation remains a future runtime responsibility.
- Its durable files are suitable for disposable tests, not an authority or
  tamper-resistant store.
- The coordinator and adapter are still the same OS principal in this
  disposable integration. A production adapter/worker boundary is not
  implemented or implied.
- No real provider, coding CLI, credential, Git operation or pull request is
  used.
