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
the recorded Linux process start identity before signalling, and escalates to
`SIGKILL` only for that same group. A missing process without a terminal result
is `unknown`; recovery returns `ambiguous` and never retries it. `collect()`
recomputes artifact size and SHA-256 before constructing an `ExecutionReport`,
then removes the disposable workspace and harness input.

## Phase 3A authority boundary

The adapter accepts an injected `ExecutionGrantValidator`. Its single method is:

```python
validate_grant(task, grant, *, adapter_id, runtime) -> None
```

The Phase 3A implementation must provide this method through its own
authenticated authority client. Returning means the authority has authenticated
the caller, found the exact durable grant, validated proposal and audience
binding, expiry, revocation and single-use state, and authorised this exact
dispatch. Unavailability, authentication failure, rejection or an ambiguous
response must raise. The adapter fails closed when no validator is configured.

This Python protocol deliberately specifies no socket, URL, wire format or
temporary endpoint. Phase 3A owns authenticated IPC, durable consumption and
principal identity. Phase 3B does not issue grants, approve tasks, store the
authority database, or reinterpret authority decisions. Phase 3A must define
idempotent validation semantics for an already-created matching adapter job so
that crash recovery does not consume or dispatch a second grant.

## Provider-independent configuration

The example in `examples/coding-worker-adapters.json` keeps agent, model
provider, coding harness, runtime and adapter identity separate. Only the
disposable fake agent is operational. Codex, DeepSeek/OpenCode, Claude and
Gemini entries are examples marked non-operational; implementing one requires a
new harness binding and security review, not a change to the authority state
machine or shared contracts.

## Limitations

- This is a Unix test harness; process-group and `/proc` identity checks have
  not been ported to Windows or macOS.
- It simulates one bounded file change and does not clone or modify a repository.
- It provides no OS sandbox beyond a private temporary directory; production
  isolation remains a future runtime responsibility.
- Its durable files are suitable for disposable tests, not an authority or
  tamper-resistant store.
- No real provider, coding CLI, credential, Git operation or pull request is
  used.
