# ADR-006: Provider-independent agent architecture

**Status:** Accepted for foundation (Phase 0 and Phase 1 only)

**Date:** 21 October 2026

**Decision owner:** Orchestra owner

**Supersedes:** None. This ADR formalises the approved ADR-005R direction without
superseding [ADR-005](ADR-005-opencode-pinned-egress.md). ADR-005 remains the
authority for endpoint-pinned OpenCode provider egress.

## 1. Context

Orchestra currently executes work through OMCP and several provider-specific
paths. Those paths share a coordinator, approval policy, scheduler and durable
state, but they do not share a provider-independent description of:

- what an agent is;
- what a task authorises;
- what an execution attempt was granted;
- what a worker reported;
- what an independent verifier established.

The result is a set of real but narrow controls whose security properties are
often specific to one adapter. A Codex process, an OpenCode gateway, a direct
DeepSeek request and a GitHub Actions run are all "workers" in conversation, yet
they have different capabilities, isolation boundaries and evidence quality.
Treating them as interchangeable would overstate both safety and verification.

The owner approved the architectural direction in ADR-005R:

> shared execution authority with pluggable agent runtimes.

This ADR converts that direction into a permanent architecture decision and
defines the first implementation slice. It does **not** authorise a change to
the live execution path.

## 2. Architectural decision

Orchestra will separate three concerns:

1. **Control plane** — scheduling, coordination, priorities, dependencies,
   progress, plans and human-facing state.
2. **Execution authority** — independent approval enforcement, task grants,
   security policy, lifecycle, cancellation and verification.
3. **Agent adapters and runtimes** — Codex, DeepSeek and future agents operating
   through a standard contract.

GitHub Actions is one supported runtime. It is not the universal execution
platform, and it must not become the definition of an Orchestra worker.

The model provider, the coding harness and the execution runtime are distinct
concepts:

| Concept | Example | Must not be conflated with |
| --- | --- | --- |
| Model provider | OpenAI, DeepSeek | The CLI or sandbox that invokes it |
| Coding harness | Codex CLI, OpenCode, none | The machine or CI service running it |
| Execution runtime | Local process, GitHub Actions, provider API | The provider account or API key |

A response-only model invocation is not a coding worker. A coding harness run
inside GitHub Actions is not thereby trusted, verified or authorised to merge.
A provider credential does not grant authority.

### 2.1 Scope of this decision

Phase 0 publishes this ADR. Phase 1 adds descriptive contracts, a provider-
independent adapter interface and a shadow-mode registry. Phase 1 does not:

- dispatch work through the new contracts;
- replace OMCP approvals;
- activate an execution-authority service;
- change worker isolation or credentials;
- change systemd, MCP Tunnel, Nextcloud, Docker or PixelBox deployment;
- enable DeepSeek as a new coding worker;
- merge or deploy anything.

## 3. Rationale

- **Security needs an enforcement point outside the coordinator.** A coordinator
  that can both propose work and silently widen its own authority is not a
  sufficient control. Approval and grant enforcement should be independently
  reviewable and independently compromised.
- **Provider-specific security belongs in adapters.** Provider endpoints, model
  names and harness quirks change quickly. Shared authority contracts should
  describe capabilities, scopes and evidence without embedding one vendor's
  rules.
- **Reuse is safer than a second control plane.** OMCP already has durable task
  state, idempotency, repository reservations, approvals, audit records,
  cancellation and recovery. The authority should reuse those where possible
  rather than create parallel schedulers or databases.
- **One small isolated authority is preferable to several brokers.** Multiple
  independent brokers increase the number of policy decision points and the
  chance of inconsistent enforcement. A single small authority with a narrow
  interface is the default unless a later security review proves that the
  credential or network boundary requires separation.
- **Evidence must remain separate from claims.** A worker saying "done" is
  input. A verifier checking evidence is a different act with different
  authority and different failure modes.
- **The migration must be reversible.** Additive contracts and a shadow
  registry let Orchestra learn and test without making production execution
  depend on unfinished security machinery.

## 4. Verified current architecture

This section describes the repository as inspected at base commit
`addb7f3407a6de52a2ef919870a7ff42357a9d87` (21 October 2026), after the
local-worker simplification. It is not a description of the target
architecture.

### 4.1 Control plane and state

- OMCP is a standard-library Python MCP server. It binds a process to one
  configured principal and exposes task-control tools over the authenticated
  tunnel.
- Task, request, scheduler, reservation, approval and audit state are durable
  local files under an owner-only state directory. Writes are atomic and the
  coordinator holds a lock on the state directory.
- Repository profiles, workers, standing authorisations and host operations are
  owner-configured. A caller cannot add a worker, change the approval mode or
  grant standing authority.
- The scheduler supports configurable parallel capacity, one task per worker at
  a time and repository read/write reservations derived by the server.
- Detached workers and remote runners publish startup leases and completion
  records so restart reconciliation can adopt or fail closed on uncertain
  outcomes.

### 4.2 Existing execution paths

| Path | Runtime | Current status and behaviour | Current security boundary |
| --- | --- | --- | --- |
| Hamed/Ina Codex local | PixelBox local lane | The current PixelBox configuration exposes separate Ina and Hamed Codex homes. Codex CLI is launched in an isolated task worktree. | Codex command is canonicalised; the local worker runs under the configured bubblewrap or systemd transient-service sandbox. |
| OpenCode local | Local process with pinned gateway | Implemented path. OpenCode V2 runs against a one-task reverse gateway with a task-local capability URL. | Exact loopback ports, fixed provider host, TLS verification, no credential in the harness, bounded diagnostics. See ADR-005. |
| DeepSeek direct | PixelBox local lane / provider API | The current configuration exposes two direct DeepSeek lanes. A direct provider request returns bounded text. | Fixed provider/model, owner-only credential, pinned endpoint, optional literal-IP allowlist for the systemd sandbox, no workspace or Git authority. Direct requests always need a fresh local decision. |
| Codex GitHub Actions | Remote ephemeral runner | Implemented adapter, but **not a configured AI-worker backend** on the current runtime. The coordinator can dispatch one exact workflow and source SHA; the workflow returns bounded artifacts and a patch. | Read-only repository token, broker-owned credentials, exact workflow SHA, source SHA binding, no push/merge/deploy. Repository CI remains useful but is not a coding worker. |
| DeepSeek/OpenCode GitHub Actions | Remote ephemeral runner | Implemented adapter, but **not a configured AI-worker backend** on the current runtime. Same remote artifact pattern with OpenCode as the coding harness. | The same remote boundary; it is not enabled as a new coding worker merely because the adapter exists. |

The current runtime is therefore a small local-worker topology: two Codex homes,
two direct DeepSeek lanes and an initial parallel capacity of two tasks. GitHub
Actions remains an implemented execution adapter and repository CI facility, not
the universal worker platform.

### 4.3 Current approval behaviour

- `task_approval_mode` defaults to `required`.
- When the owner explicitly sets `task_approval_mode` to `disabled`, only
  `routine_development` tasks are automatically authorised. Production
  deployment, destructive operations, security changes and unclassified work
  remain in `approval_required`.
- Standing authorisation, where configured, is limited to
  `routine_development`, named repository profiles, named Codex workers and
  owner-configured writable paths. It cannot grant production deployment,
  destructive operation, security change, merge, push, credential access or
  unclassified work.
- Direct DeepSeek requests require a fresh explicit local decision even when the
  general task gate is disabled.
- GitHub Actions workers are not eligible for standing authorisation.
- Host operations always require explicit local approval.

This document does not call those controls "secure" in a universal sense. They
are the verified current controls, with the gaps identified in this ADR.

### 4.4 Current verification and grant gaps

- A worker completion record is not independent verification. OMCP reports a
  bounded worker result; it does not establish that the requested outcome was
  achieved.
- There is no provider-independent `TaskSpec`, `ExecutionGrant`,
  `ExecutionReport` or `VerificationRecord` contract.
- There is no separate execution authority that can validate and consume a
  grant independently of the coordinator.
- Existing "standing authorisation" is an owner-configured policy object in the
  coordinator. It is not an independently enforced, signed, single-use attempt
  grant.
- Worker isolation is real but heterogeneous. It must not be described as
  uniform across local Codex, pinned-egress OpenCode and GitHub Actions.

### 4.5 Baseline verification evidence

At base `addb7f3`, before Phase 1 changes, the repository's standard library
suite passed:

```text
python3 -m unittest discover -s tests -v
Ran 185 tests
OK
```

After the additive Phase 1 changes, the same suite passed:

```text
python3 -m unittest discover -s tests -v
Ran 223 tests
OK
```

This is repository evidence for the existing implementation. It is not
end-to-end provider verification, target-host sandbox verification or production
verification.

## 5. Security threat model

### 5.1 Assets

- owner approval authority and policy configuration;
- provider credentials and GitHub dispatch credentials;
- repository integrity and protected branches;
- task instructions and project data;
- execution grants, attempt identity and audit history;
- worker artifacts, patches and verification evidence;
- availability of the coordinator and runtime capacity.

### 5.2 Adversaries and failure sources

- a compromised or buggy coordinator that tries to widen its own authority;
- a compromised worker, harness or provider response;
- a malicious repository file or task instruction attempting prompt injection;
- a caller replaying, copying or redirecting a grant;
- a worker producing a plausible but false success report;
- a stale or ambiguous external provider outcome;
- credential leakage through prompts, logs, environment, artifacts or contracts;
- supply-chain compromise of a harness, action or model endpoint;
- operator error during configuration or migration.

### 5.3 Threats and required response

| Threat | Required response |
| --- | --- |
| Coordinator attempts to approve its own sensitive action | Authority is a separate decision point; coordinator identity alone cannot create approval. |
| Coordinator proposes a broader task than the human approved | Approval and grant bind to the exact canonical proposal digest, repository, base commit, scope and agent identity. |
| Grant is replayed after success | Grants are single-use records with nonce, expiry and attempt identity; the authority records consumption. |
| Grant is used by another adapter or runtime | Grant audience includes the exact adapter and runtime; mismatch fails closed. |
| Worker reports success without evidence | Report is untrusted; verified completion requires a separate `VerificationRecord`. |
| Worker forges a verification record | Verification has its own verifier identity and authority; it is not accepted merely because a worker produced it. |
| Repository content tells an agent to ignore policy | Repository content is data, not authority or instruction. It cannot rewrite grants, approval policy or scope. |
| Model provider or harness is swapped underneath a task | Task and grant bind model provider, model ID and coding harness, not just a friendly agent name. |
| Response-only agent is selected for coding work | Agent class and capability claims distinguish response-only from coding; selection fails closed. |
| Credential value appears in a contract, log or artifact | Contracts carry references only; secret values remain in the existing credential boundary. |
| Duplicate agent IDs cause ambiguous routing | Registry rejects duplicate IDs and conflicting definitions atomically. |
| Stale approval is reused after candidate changes | Approval and grant bind to the proposal digest and base commit; a changed candidate requires re-evaluation. |
| External outcome is unknown after a crash | Reconcile with the authoritative provider before retrying; never report success from a missing result. |
| Cross-project authority leaks | Repository, project and scope are explicit; authority in one project does not imply another. |

### 5.4 Out of scope for this threat model

This ADR does not claim to solve provider-side account compromise, malicious
model weights, macOS kernel compromise, GitHub platform compromise, physical
device compromise or supply-chain attacks against arbitrary dependencies. Those
require separate controls and evidence.

## 6. Trust boundaries

```text
Human owner
  |  explicit approval / owner-only policy
  v
Control plane (OMCP scheduling, coordination, durable task state)
  |  proposal, never self-approval
  v
Execution authority (independent policy, grant, cancellation, verification)
  |  validated single-use grant
  v
Agent adapter (provider-independent seven-operation contract)
  |  exact runtime audience
  v
Execution runtime (local process, GitHub Actions, provider API)
  |  provider / sandbox / network boundary
  v
Model provider and external systems

Return path:
runtime -> adapter -> untrusted ExecutionReport
                         |
                         +--> independent VerificationRecord
```

Boundary rules:

- The control plane may propose work and read state. It may not silently turn a
  proposal into its own approval.
- The execution authority is the only component authorised to issue, validate or
  consume a production grant. Phase 1 implements no such production authority.
- An adapter may execute only the exact task and audience named in a validated
  grant. It may not broaden scope because a task instruction asks it to.
- A runtime may return an untrusted report. It may not return a trusted
  verification record.
- Verification must be performed by a verifier with its own identity and
  evidence, not by copying a worker's success claim.
- Credentials cross a dedicated credential boundary. Contract serialisation
  must contain references, never secret values.

## 7. Common agent and task contracts

Phase 1 implements these contracts in `orchestra/contracts.py`.

### 7.1 AgentManifest

An immutable description of an agent:

- identity, display name and version;
- agent class: `coding` or `response_only`;
- model provider and supported model IDs;
- coding harness (or `none` for response-only);
- supported execution runtimes;
- capability claims with per-capability verification status;
- tool requirements;
- filesystem, credential, Git-metadata, sandbox and network isolation
  requirements;
- credential references only;
- resource limits, concurrency and cost characteristics;
- overall verification status and evidence reference.

Invariants include:

- unknown capabilities, runtimes, isolation values and credential-reference
  schemes are rejected;
- a response-only agent may claim only `respond`, must have no coding harness,
  no workspace tools, no filesystem access and no Git metadata access;
- a coding agent must declare a coding harness, at least one coding capability
  and a workspace-bearing runtime;
- a `verified` capability requires an evidence reference;
- a manifest marked `verified` may not contain unverified capabilities;
- duplicate capability claims are rejected.

### 7.2 TaskSpec

An immutable canonical proposal containing:

- task and request IDs;
- repository and full 40-character base commit;
- exactly one of inline instructions or an immutable instruction reference;
- agent ID, model provider, model ID and coding harness;
- permitted execution runtimes;
- authorised operations and repository-relative read/write file scope;
- network policy and isolation constraints;
- resource and timeout limits;
- approval class and consequence class.

Canonicalisation sorts unordered collections, normalises timestamps and paths,
uses sorted JSON keys with no insignificant whitespace, and rejects non-finite
numbers. The proposal digest is SHA-256 over that canonical form. Every
security-relevant field is included. Equivalent serialisation of the same
proposal produces the same digest; any change to scope, base commit, agent,
model, harness, instructions, network, limits or approval classification changes
it.

Approval classification cannot downgrade a sensitive consequence.
`production_deployment`, `destructive_operation` and `security_change` require
the `sensitive` approval class. Edits require an explicit write scope. Network
access requires a network policy and the `access_network` operation.

### 7.3 ExecutionGrant

A grant binds one proposal to one attempt:

- proposal digest;
- attempt, task and request identity;
- authorised adapter and runtime audience;
- repository, base commit, operations and file scope;
- agent, model provider, model ID and coding harness;
- random nonce and issue/expiry timestamps;
- single-use semantics;
- issuer identity.

Validation checks structure, expiry, audience, proposal digest and every bound
security field. A consumed grant is rejected. A grant that is not single-use is
rejected.

The Phase 1 grant is explicitly `unsigned_contract_only`. It contains no
production signing key and no signature. Structural validation does **not**
prove that an authority issued it, that a caller did not copy it, or that a
runtime cannot replay it. Those properties require the independent execution
authority and cryptographic design deferred to a later phase. No production
execution path uses these grants yet.

### 7.4 ExecutionReport

An untrusted worker report containing:

- report, task, proposal and attempt identity;
- provider-run identity;
- runtime and adapter identity;
- agent, model and harness identity;
- execution outcome;
- artifact references with SHA-256 hashes;
- commit and pull-request references where applicable;
- test evidence;
- bounded error information;
- resource usage where available.

The report's trust level is fixed to `untrusted_worker_report`. A worker report
can never independently establish verified completion. `completion_verified` is
always false. A report can be verified only by a separate
`VerificationRecord` that matches its task, proposal and attempt.

### 7.5 VerificationRecord

A logically separate result containing:

- verification, task, proposal and attempt identity;
- verifier identity and method;
- evidence checked, with stable references and hashes where available;
- one or more validation outcomes;
- verification status using the foundation vocabulary (`pass`, `fail`,
  `inconclusive`, `not_run`, `blocked`, `invalid`, `stale`);
- failure reasons and scope.

A passing record requires checked evidence, at least one outcome, every outcome
passing and no failure reasons. A record verifies a report only when its
identity fields match and its status passes. Verification is not a renamed
worker success flag.

### 7.6 Adapter interface

`orchestra/adapters.py` defines a provider-independent protocol with exactly:

- `describe`;
- `preflight`;
- `dispatch`;
- `status`;
- `cancel`;
- `collect`;
- `recover`.

`describe` and `preflight` must not create external side effects. `dispatch` may
run only after an independent authority validates a grant. `collect` returns an
untrusted report, never a verification claim. `recover` must reconcile with the
external provider rather than guessing.

## 8. Independent execution-authority design

### 8.1 Shape

Prefer **one small, isolated execution-authority component** over multiple
independent brokers. It should:

- run under a separate service identity from the coordinator;
- have no ambient provider, repository-write or deployment credentials beyond
  its narrow duty;
- accept proposals and approvals in a bounded protocol;
- own grant issue, validation and consumption state;
- dispatch through adapters or hand a validated grant to the existing OMCP
  runner;
- own cancellation and recovery decisions for active attempts;
- accept verification results from authorised verifiers;
- write append-preserving audit records.

It should not become a second scheduler, a general shell, a credential gateway,
or a place where model output is treated as permission.

### 8.2 Reuse existing components

Where practical, the authority reuses:

- OMCP durable task and request state;
- existing repository profiles and reservations;
- existing approval request/decision transport;
- existing local control socket ownership checks;
- existing worker startup leases, completion records and cancellation;
- existing provider-specific sandbox and egress controls;
- existing GitHub dispatch broker and source-SHA binding.

A new database, daemon, network service or deployment configuration is **not**
part of Phase 1.

### 8.3 Authority invariants

1. A proposal is not an approval.
2. A worker report is not a verification.
3. A credential is not authority.
4. A grant is bound to one proposal digest, one attempt, one adapter and one
   runtime.
5. A grant is single-use and expires.
6. The coordinator cannot widen its own policy, scope, audience or credential
   access.
7. A sensitive action requires an explicit human decision unless a narrow,
   pre-existing standing policy covers the exact routine case.
8. Missing, stale, contradictory or unverifiable authority fails closed.
9. Recovery may replan around a constraint; it may not erase the constraint.
10. Privileged side effects record intent before execution and outcome after.

### 8.4 Compromise containment

A compromised coordinator must not be able to:

- mint its own grants;
- change the approval policy;
- add a worker or runtime audience;
- extend its own writable paths or network hosts;
- approve a sensitive operation;
- convert a worker report into verification;
- use another project's authority.

A compromised adapter or runtime must not be able to:

- change the proposal it was granted;
- use a grant for another attempt, adapter or runtime;
- access credentials outside its declared references;
- claim verified completion;
- expand the repository or file scope.

These are design requirements, not claims that Phase 1 enforces them. Phase 1
implements validation and honest contract fields only.

## 9. Pluggable runtime architecture

Each adapter describes one agent and one or more supported runtimes. The same
agent may have multiple adapters if its runtime boundary differs materially.
Adapter identity and runtime identity are part of the grant audience.

Runtime selection considers:

- agent class and capability;
- model provider, model ID and coding harness;
- supported runtime;
- isolation and network requirements;
- approval class and consequence class;
- resource limits and concurrency;
- cost characteristics;
- verification status and freshness;
- current availability and repository reservations.

Adapters do not decide approval policy. They may reject a task in `preflight`,
but they may not upgrade an unapproved task into an approved one. The shared
contracts contain no provider-specific endpoint, model allowlist or credential
rule; those belong in adapter configuration and evidence.

## 10. Approval and execution policy

### 10.1 Current policy remains unchanged

This ADR does not change the deployed approval mode. The current OMCP rules
described in section 4 remain authoritative until a separately reviewed
migration is activated.

### 10.2 Target policy

- **Routine, low-risk work** should eventually support standing authorisation
  under tightly bounded owner policies: exact repository profiles, workers,
  consequence class, file roots, runtime, model/harness and time window.
- **Sensitive operations** must require explicit human approval. At minimum this
  includes production deployment, destructive operations, security changes,
  merge/push to protected branches, credential access and unclassified work.
- **Standing authorisation must never let a compromised coordinator expand its
  own permissions.** Policies are owner-only, versioned and bound to exact
  scopes. The authority validates the policy; the coordinator cannot mutate it.
- **A caller cannot select, widen or enable policy.** Request fields may propose;
  authority decides.
- **Changing policy requires a new reviewed configuration change**, not a task.

### 10.3 Approval classification

| Consequence class | Minimum approval class | Standing authorisation |
| --- | --- | --- |
| `routine_development` | `routine` or `sensitive` | Eligible only under an exact owner policy |
| `production_deployment` | `sensitive` | Never in the initial policy |
| `destructive_operation` | `sensitive` | Never in the initial policy |
| `security_change` | `sensitive` | Never in the initial policy |
| `unclassified` | `unclassified` | Never |

A task whose approval class is weaker than its consequence requires is
structurally invalid. This is implemented in `TaskSpec`; it is not yet enforced
on the live path because nothing dispatches from `TaskSpec`.

## 11. Durable lifecycle and recovery

The target lifecycle is:

```text
proposal (TaskSpec + digest)
  -> approval decision
  -> attempt record
  -> single-use ExecutionGrant
  -> dispatch through adapter/runtime
  -> provider-run identity
  -> untrusted ExecutionReport
  -> independent VerificationRecord
  -> integration decision
```

Durability requirements for the later authority:

- persist proposal, approval, attempt and grant before dispatch;
- persist the exact adapter/runtime audience and provider-run identity before
  polling;
- persist grant consumption and result before acknowledging completion;
- make idempotency keys stable across retries and restarts;
- reconcile unknown external outcomes before retry;
- never retry an unknown side effect blindly;
- preserve evidence and historical records during recovery;
- expire stale grants and approvals fail-closed;
- keep audit append-preserving and free of secret values.

Phase 1 provides the contract shapes and validation needed to design that
lifecycle. It does not create durable grant state, an attempt database or an
authority daemon.

## 12. Runtime selection

The initial shadow catalogue represents existing paths only:

| Agent | Class | Provider | Harness | Runtime | Current status | Coding? |
| --- | --- | --- | --- | --- | --- | --- |
| Hamed/Ina Codex local | coding | OpenAI | Codex CLI | local process | configured local lane | yes |
| Codex GitHub Actions | coding | OpenAI | Codex CLI | GitHub Actions | implemented, not currently configured as an AI worker | yes |
| DeepSeek/OpenCode GitHub Actions | coding | DeepSeek | OpenCode | GitHub Actions | implemented, not currently configured as an AI worker | yes, when separately enabled |
| DeepSeek direct response-only | response-only | DeepSeek | none | provider API | configured local lane | **no** |

The registry explicitly answers:

- which agents are configured (the two GitHub Actions manifests remain
  registered and discoverable but are marked `configured=false` on the current
  local-worker runtime);
- which capabilities each claims;
- which runtimes each supports;
- which capabilities are verified versus unverified;
- which known combinations are unavailable and why.

All initial capability claims are marked **unverified**. Existing repository
tests are evidence about code paths, not independent end-to-end verification of
each provider. The response-only DeepSeek adapter is not advertised as a coding
worker and has no workspace, Git or verification capability.

The registry is a catalogue, not an execution interface. It has no dispatch,
status, cancel, collect or recover operation. Registering an adapter calls only
`describe`; it never calls `preflight`, `dispatch` or any other execution
method. No production execution depends on the registry.

## 13. New-agent onboarding

A new agent is onboarded in this order:

1. **Manifest.** Define identity, class, provider, models, harness, runtimes,
   capabilities, tools, isolation, credential references, limits, concurrency
   and cost.
2. **Capability evidence.** Mark capabilities `unverified` until an independent
   verification record supports them. Never self-certify.
3. **Adapter.** Implement the seven-operation contract with fail-closed
   preflight and untrusted collection.
4. **Shadow registration.** Register the manifest and explicit unavailable
   combinations. Observe catalogue output; do not dispatch.
5. **Negative tests.** Prove unsupported runtime, model, harness, capability,
   scope, audience and response-only/coding confusion are rejected.
6. **Controlled verification.** Run the adapter in an isolated environment with
   disposable credentials and no production authority.
7. **Policy enablement.** Add owner-only policy only after review, evidence and a
   rollback plan. Enabling a new coding worker is a separate decision from
   adding its manifest.

Adding a speculative provider integration as "operational" is prohibited.

## 14. Migration phases

### Phase 0 — architecture decision (this change)

Publish ADR-006. Preserve ADR-005. No runtime change.

### Phase 1 — shared contracts and shadow registry (this change)

- `AgentManifest`, `TaskSpec`, `ExecutionGrant`, `ExecutionReport`,
  `VerificationRecord` and the adapter protocol.
- Deterministic TaskSpec canonicalisation and digest.
- Contract validation, including negative and adversarial cases.
- Shadow registry describing existing agents without dispatch.
- No production authority, database, daemon, network service, credential
  gateway or deployment change.

### Phase 2 — independent authority prototype (not authorised)

Design and test a small isolated authority that can:

- accept an immutable proposal;
- enforce approval policy;
- issue and consume an unsigned or test-signed grant in a disposable
  environment;
- bind dispatch to the exact audience;
- record unknown outcomes and recover without guessing.

Phase 2 requires a separate approval. It must not touch production until its
threat model, durable state, secret handling and recovery tests are reviewed.

### Phase 3 — adapter migration

Move one narrow existing path at a time behind the authority. Keep the old path
available as a fail-closed fallback during review. Do not migrate all providers
at once.

### Phase 4 — bounded standing authorisation

Only after Phases 2 and 3 pass independent review. Start with one exact routine
scope, one worker/runtime and a short expiry. Sensitive classes remain explicit.

### Phase 5 — cutover and evaluation

Enable the authority for the reviewed path, monitor, and retain rollback. Do not
describe the system as secure or verified beyond the evidence actually
collected.

## 15. Verification requirements

### 15.1 Phase 1 evidence

The Phase 1 tests must cover, at minimum:

1. manifest validation and invalid capabilities;
2. canonical TaskSpec hashing;
3. digest stability across equivalent serialisation;
4. digest changes after every security-relevant field change;
5. invalid, expired, consumed and mismatched grant contracts;
6. adapter/runtime audience mismatch;
7. duplicate IDs and conflicting registry definitions;
8. report schema validation;
9. worker-reported success remaining unverified;
10. adapter capability discovery;
11. unsupported model, harness and runtime combinations;
12. response-only DeepSeek not advertised as a coding worker;
13. no credential values in serialised contracts;
14. shadow registry having no dispatch side effects;
15. backward compatibility with the existing task and worker suite.

Tests must include negative and adversarial cases. Passing unit tests do not
prove provider integration, target-host sandbox behaviour or end-to-end
orchestration.

### 15.2 Later verification gates

Before the authority or a new adapter is activated, require:

- independent code and authority-boundary review;
- adversarial tests for replay, audience confusion, scope widening, duplicate
  IDs, stale approvals, credential leakage and response-only/coding confusion;
- disposable end-to-end execution through the exact runtime;
- evidence that the provider run is bound to the exact source commit;
- independent verification of artifacts and claims;
- restart, cancellation and unknown-outcome recovery tests;
- a documented rollback rehearsal.

A worker's own success message is never sufficient evidence for any of these
gates.

## 16. Rollback strategy

Phase 0 and Phase 1 are strictly additive:

- ADR-005 is unchanged.
- Existing OMCP modules and dispatch paths are unchanged.
- New code lives in the `orchestra` package and new tests.
- No state schema migration is introduced.
- No database, daemon, socket, service, credential or deployment configuration
  changes.
- The shadow registry has no execution methods and is not imported by OMCP.

Rollback for this phase is to revert the ADR-006/contracts commit. Existing
tasks, state, approvals, workers, systemd units, tunnel configuration and
credentials are unaffected.

For later phases, rollback must:

- disable the authority through an owner-only flag that defaults to off;
- keep the previous reviewed execution path available;
- stop new grants without deleting historical evidence;
- preserve audit records and in-flight attempt identity;
- reconcile or quarantine unknown side effects rather than retrying blindly;
- verify the restored path before declaring rollback complete.

A failed or unverifiable rollback is a critical terminal condition requiring
operator intervention.

## 17. Explicitly deferred functionality

The following are deliberately not implemented or authorised by this ADR:

- production grant signing keys, signatures or key rotation;
- a production execution-authority daemon or network service;
- a new database or state store for grants, attempts or verification;
- cryptographic replay prevention or hardware-backed nonces;
- a credential gateway, secret broker or vault integration;
- automatic standing authorisation for any new or sensitive class;
- automatic merge, push or deployment authority;
- migration of live OMCP dispatch to `TaskSpec` or `ExecutionGrant`;
- replacing existing approvals or worker isolation;
- enabling DeepSeek or OpenCode as a new coding worker;
- multi-host coordinator election or distributed leases;
- changes to systemd, MCP Tunnel, Nextcloud, Docker or PixelBox;
- a user interface for the new authority;
- provider-specific policy rules inside the shared contracts;
- end-to-end verification of model providers or target hosts;
- deleting or rewriting existing historical records.

## 18. Consequences

### Positive

- One vocabulary for every future agent.
- Security-relevant task identity is canonical and digest-bound.
- Provider, harness and runtime are no longer conflated.
- Response-only and coding agents are structurally distinguishable.
- Grant audience and single-use requirements are explicit before enforcement is
  built.
- Worker claims and independent verification are separate from the start.
- Existing execution remains untouched and reversible.

### Negative and risks

- The contracts are descriptive until Phase 2; they do not make current paths
  secure.
- Unsigned grants can be copied; only the future authority can enforce
  authenticity and consumption.
- A dishonest adapter can still lie in its manifest until independent
  verification exists.
- The registry's initial capability claims are unverified, so it is a catalogue
  of intent and existing configuration rather than proof of quality.
- Splitting the authority from the coordinator adds operational complexity and
  requires careful secret, socket and recovery design.

These risks are accepted for a foundation phase because no production execution
depends on the new contracts.

## 19. References

- [ADR-001: Local outbound worker bridge](ADR-001-mac-bridge-v1.md)
- [ADR-002: Local MCP task-control adapter](ADR-002-omcp-task-control.md)
- [ADR-003: Local approval notifications and capability-bounded standing authority](ADR-003-omcp-approval-notifications.md)
- [ADR-004: PixelBox coordinator with GitHub Actions workers](ADR-004-pixelbox-linux-actions-workers.md)
- [ADR-005: Endpoint-pinned OpenCode provider egress](ADR-005-opencode-pinned-egress.md)
- `docs/foundation/DOMAIN-MODEL.md`
- `docs/foundation/SECURITY-AUTHORITY-MODEL.md`
- `docs/foundation/AUTHORITATIVE-STATE-MODEL.md`
- `docs/foundation/FAILURE-RECOVERY-MODEL.md`
- `docs/foundation/TESTING-VERIFICATION-MODEL.md`
- `docs/EXECUTION-POLICIES.md`
- `orchestra/contracts.py`
- `orchestra/adapters.py`
- `tests/test_provider_independent_contracts.py`
