# Orchestra Authoritative State Model v1

**Status:** Foundation proposal  
**Version:** 1  
**Date:** 2 October 2026  
**Purpose:** Define where truth lives in Orchestra, what may change, what must remain historical, what is derived, and how state advances safely across crashes, retries, external systems and concurrent coordinators.

## 1. Core rule

Orchestra must never confuse:

- what it owns as orchestration truth;
- what an external system owns as native truth;
- what was merely observed;
- what was inferred or derived;
- what a worker claimed;
- what was actually verified.

The authoritative state model exists to preserve those distinctions.

## 2. Four classes of state

Every meaningful datum should belong to one of four classes.

### 2.1 Orchestra-owned authoritative state

Facts Orchestra itself is responsible for defining and advancing.

Examples:

- current Goal revision;
- current Plan Revision;
- Task intent and dependencies;
- currently active Human Gate;
- Decision records;
- supersession relationships;
- Execution Attempt identity;
- which Instruction belongs to which Attempt;
- which Outcome Criteria remain unsatisfied;
- orchestration authority epoch/lease;
- accepted Recovery Action;
- which external observations have been reconciled.

These facts must not be delegated implicitly to GitHub, a worker, chat history or a provider.

### 2.2 Externally authoritative native state

Facts owned by the system in which they actually exist.

Examples:

- whether a GitHub PR is open or merged;
- exact commit SHA on a branch;
- cloud deployment ID and provider-reported state;
- whether a CI job passed;
- whether a DNS record currently resolves;
- whether a worker provider reports quota unavailable;
- contents of a repository;
- an external database record.

Orchestra stores references and observations of these facts. It does not pretend to become their source of truth.

### 2.3 Append-only historical facts

Facts that, once recorded, should not be rewritten as if they never happened.

Examples:

- an Attempt started;
- an Instruction was issued;
- a Worker returned a Result;
- an Artifact Version existed;
- a Verification ran with a given scope and result;
- a Decision was made;
- a Human Gate was opened or resolved;
- an External Observation was obtained;
- a Recovery Action was chosen;
- a Task or Artifact was superseded;
- a reconciliation occurred.

Corrections are represented by later records that explain or supersede earlier interpretation.

### 2.4 Derived state

Values computed from authoritative facts and observations.

Examples:

- task readiness;
- stale / possibly stale;
- blocked;
- currently assignable;
- worker eligible;
- goal progress;
- integration suitability;
- notification needed;
- latest known availability;
- whether all mandatory Outcome Criteria are satisfied.

Derived state should be recomputable and must not silently outrank the facts from which it was derived.

## 3. Authoritative ownership table

| Concept | Orchestra authority | External authority | Notes |
|---|---|---|---|
| Project | yes | no | orchestration boundary |
| Goal | yes | no | human-owned intent recorded by Orchestra |
| Outcome Criterion | yes | no | may reference external evidence |
| Plan Revision | yes | no | immutable once current revision changes |
| Task | yes | no | task intent is Orchestra-owned |
| Dependency Predicate | yes | no | satisfaction may depend on external facts |
| Worker identity | yes/reference | provider may own account existence | Orchestra owns its worker record |
| Capability declaration | yes/observed | provider may supply evidence | must distinguish declared vs verified |
| Availability | derived from observations | provider/native system | time-sensitive |
| Access | policy/reference | provider enforces actual access | Orchestra must not infer from capability |
| Authority | yes | provider may enforce technical permission | Orchestra owns orchestration permission policy |
| Attempt | yes | no | one execution identity |
| Instruction | yes | no | immutable per issued version |
| Execution Result | historical report | worker origin | report is not verification |
| Artifact | reference/meaning | native provider | Orchestra owns semantic relationship, not native object |
| Artifact Version | reference | native provider | prefer immutable native identity |
| External Observation | historical | source system supplies fact | stamped with source/time |
| Evidence | yes | may reference external source | provenance required |
| Verification | yes | verifier may be external | Orchestra owns meaning/scope/result record |
| Decision | yes | human/tool supplies decision | authority scope required |
| Human Gate | yes | no | silence never resolves it |
| Integration Result | yes + external refs | target system owns native merge/deploy fact | reconcile both |
| Failure | historical | source may be external | scoped to operation/attempt |
| Recovery Action | yes | no | new action, never erasure |
| Supersession | yes | no | explicit relationship |
| Outcome Verification | yes | evidence may be external | goal-level truth |

## 4. The write rule

An authoritative Orchestra state change is legal only when all of the following hold:

1. the caller is operating under current orchestration authority;
2. the target object version is current or the command declares the expected prior version;
3. required Dependency Predicates are satisfied;
4. required Authority is present;
5. required evidence exists;
6. the transition is valid for that object;
7. the write is durable before side effects are treated as accepted;
8. the transition emits or preserves enough history to reconstruct why it occurred.

This is a semantic rule, not yet a database design.

## 5. Optimistic versioning

Mutable Orchestra-owned records should conceptually carry a revision/version.

A transition should be applied against an expected prior revision.

Example:

```text
Task revision 12
  -> reconcile stale artifact
command expects revision 12

if Task is now revision 13
  reject stale command
```

This prevents old workers or coordinators from advancing state based on an outdated view.

The storage mechanism may later use integers, event sequence numbers, compare-and-swap, database versions or another equivalent mechanism.

## 6. Orchestration authority

### 6.1 Authority epoch

Every actively coordinated scope should have a current **orchestration authority epoch**.

Conceptually:

```text
project/scope
current_epoch = 41
current_controller = controller-A
lease_expires_at = ...
```

A coordinator advancing state must prove it is operating in the current epoch.

When leadership changes, a new epoch is created.

Old coordinators may still finish external work, but they must not directly advance authoritative Orchestra state after losing authority.

### 6.2 Why an epoch is required

Without it:

- a resumed process can continue issuing stale instructions;
- two schedulers may both assign the same task;
- an old control plane may mark a Task complete after a newer Plan has superseded it;
- retries can race with late results from an earlier Attempt.

### 6.3 Late results

A result from a stale coordinator or old Attempt is not discarded.

It becomes historical input to reconciliation.

Orchestra may:

- reuse its Artifact;
- verify it;
- integrate it;
- supersede it;
- reject it as no longer applicable.

But receipt of the late result does not restore the old coordinator's authority.

## 7. Mutable state versus history

### 7.1 Mutable pointers

Some objects need a current pointer:

- current Goal revision;
- current Plan Revision;
- current Task disposition;
- current unresolved Blockers;
- current Human Gate state;
- current orchestration epoch;
- current canonical Instruction for an unstarted Attempt;
- current supersession target;
- current reconciliation status.

### 7.2 Historical records

The following should be append-preserving:

- previous Goal/Plan revisions;
- all Attempts;
- issued Instruction versions;
- Execution Results;
- External Observations;
- Verifications;
- Decisions;
- Human Gate transitions;
- Failures;
- Recovery Actions;
- Supersession records;
- reconciliation runs;
- Integration Results;
- Outcome Verifications.

A correction appends new truth rather than editing away the previous record.

## 8. Task state is a projection, not one magic field

A Task should not rely on one coarse status to carry all meaning.

Its user-facing state can be derived from several independent facts:

```text
intent exists
dependency readiness
human-gate readiness
active attempt(s)
accepted artifact(s)
verification state
integration state
supersession state
acceptance-condition satisfaction
```

A practical projection may expose labels such as:

- planned;
- ready;
- running;
- blocked;
- awaiting-human;
- reconciling;
- satisfied;
- cancelled;
- superseded.

But these labels are convenience projections. They must not erase underlying facts.

## 9. Attempt lifecycle

An Attempt has its own state independent of the Task.

Conceptual lifecycle:

```text
created
  -> assigned
  -> dispatched
  -> acknowledged
  -> running
  -> result_returned
  -> reconciled

terminal execution outcomes may include:
  succeeded_execution
  failed_execution
  cancelled
  timed_out
  abandoned
  superseded_before_start
```

Important:

- `succeeded_execution` means the worker completed the execution contract;
- it does not mean the Task is accepted;
- `result_returned` without reconciliation is not completion;
- reconciliation may occur after coordinator restart.

## 10. Instruction lifecycle and staleness

An Instruction is immutable once issued.

If work requirements change:

- do not silently edit the old Instruction;
- supersede it with a new Instruction or new Attempt.

Before beginning consequential work, a Worker should validate:

- Task still exists and is not superseded/cancelled;
- Attempt is current and assigned to it;
- Instruction version matches;
- required Plan Revision is still valid for this work;
- required base/external reference still matches or is within accepted drift;
- Human Gates remain satisfied;
- orchestration authority still recognises the Attempt.

If validation fails, the Worker should not blindly continue. It should return a reconciliation-needed result.

## 11. External observations

### 11.1 Observation record

Every material external observation should carry:

- source adapter;
- source identity;
- observed object;
- observed value;
- source-native version if available;
- observed time;
- retrieval time;
- freshness/expiry policy;
- confidence only if applicable;
- raw reference where safe.

### 11.2 Observations do not mutate history

If GitHub reports:

```text
10:00 PR open
10:10 PR merged
```

the 10:00 observation remains historically valid.

The current projection uses the later observation.

### 11.3 Freshness

Each observation class may have a freshness rule.

Examples:

- immutable commit SHA: effectively non-expiring;
- PR merge status: refresh before integration;
- worker quota: short-lived;
- DNS state: refresh before cutover;
- deployment health: refresh before declaring outcome success.

A stale observation cannot satisfy a predicate that requires current truth.

## 12. Reconciliation

Reconciliation is the bridge between external reality and Orchestra state.

### 12.1 Reconciliation loop

Conceptually:

```text
observe
  -> compare with known state
  -> classify difference
  -> preserve observation
  -> derive consequences
  -> perform authorised Orchestra transitions
  -> schedule follow-up if needed
```

### 12.2 Difference classes

A reconciliation may discover:

- no material change;
- expected progress;
- unexpected completion;
- unexpected failure;
- external drift;
- stale instruction/base;
- supersession candidate;
- missing artifact;
- duplicated side effect;
- unknown external state;
- authority conflict.

### 12.3 Reconciliation is mandatory after

- worker result return;
- coordinator restart;
- prolonged disconnection;
- external webhook gap;
- failed side-effect acknowledgement;
- provider timeout;
- integration;
- deployment;
- Human Gate resolution;
- detected staleness.

## 13. Side effects and durable intent

Orchestra often needs to cause external side effects:

- create PR;
- push commit;
- deploy;
- send message;
- modify DNS;
- start compute;
- spend money.

The semantic ordering should be:

```text
1. establish authorised intent
2. record durable operation identity
3. perform external side effect
4. observe/reconcile actual result
5. record accepted outcome
```

A timeout between steps 3 and 4 is **unknown outcome**, not automatic failure.

The recovery path should first reconcile using idempotency keys, provider operation IDs or external facts before retrying.

## 14. Idempotency

Consequential external operations should have a durable Orchestra operation identity whenever the provider or adapter supports safe correlation.

A retry must answer:

- is this a new intended operation?
- or a retry to discover/complete the same operation?

Example:

```text
operation_id = deploy-184

provider timeout

recovery:
  query deploy-184 or target environment

not:
  immediately create deploy-185
```

This prevents duplicate payments, deployments, messages or destructive operations.

## 15. Evidence-backed transitions

Transitions that materially advance trust should cite evidence.

Examples:

### Task satisfied

Requires evidence that Task Acceptance Conditions are met.

### Artifact verified

Requires Verification record targeting the exact Artifact Version.

### Integration accepted

Requires external target evidence plus integration verification where policy requires it.

### Human Gate resolved

Requires a Decision from the authorised human identity.

### Goal satisfied

Requires current Outcome Verification covering all mandatory Outcome Criteria.

The exact evidence policy belongs to later implementation/policy design.

## 16. Verification state

Verification should not mutate an Artifact into a universal `verified=true`.

Instead:

```text
Artifact Version X

Verification A
  claim: parses under PHP 8.3
  scope: source
  PASS

Verification B
  claim: works in disposable WordPress runtime
  scope: runtime
  PASS

Verification C
  claim: intended UX works on staging
  scope: browser/staging
  NOT RUN
```

Higher-level predicates inspect the required set of claims/scopes.

## 17. Human Gate state

Conceptual lifecycle:

```text
open
  -> resolved_approved
  -> resolved_rejected
  -> resolved_deferred
  -> superseded
  -> cancelled
```

A Gate may be reopened only as a new Gate or explicit new revision if the decision context materially changes.

Changing evidence after approval may invalidate the predicate that depended on the old Decision without rewriting the historical Decision.

## 18. Decision durability

A Decision remains true as a historical act even if later superseded.

Example:

```text
Decision D1:
  deploy candidate X
  approved at 15:00

new evidence at 15:05:
  security verification failed

Result:
  D1 remains historical
  its execution predicate is no longer satisfiable
  a new Decision may be required
```

This is safer than mutating D1 into "not approved".

## 19. Failure semantics

Failures are scoped.

Examples:

- Attempt failure;
- adapter failure;
- verification failure;
- integration failure;
- deployment failure;
- reconciliation failure.

A failure record states:

- what operation failed;
- when;
- evidence;
- whether outcome is known or unknown;
- retry/recovery classification.

Failure does not automatically imply:

- Task failed;
- Artifact invalid;
- Goal failed;
- provider unavailable forever.

## 20. Unknown outcome

`unknown_outcome` is a first-class condition for external operations where Orchestra cannot tell whether the side effect occurred.

Examples:

- API timeout after payment submission;
- network loss after deploy request;
- worker disconnected after push;
- browser automation lost after form submit.

Required response:

1. do not assume failure;
2. do not blindly repeat;
3. reconcile external reality;
4. only retry if policy proves it is safe.

## 21. Retry semantics

A retry creates a new Attempt or operation attempt.

It must record:

- predecessor attempt;
- reason;
- inherited artifacts/context;
- reset context;
- changed worker/tool/resource;
- changed instruction if any;
- same or new external operation identity;
- authority for retry.

Retrying does not delete earlier Failure or Result records.

## 22. Supersession semantics

Supersession changes what is current, not what existed.

Example:

```text
Task A -> superseded by Task B

Task A:
  historical
  may contain useful Artifact A

Task B:
  current route

Artifact A:
  may still be reused after verification
```

The successor should state whether it:

- replaces intent;
- absorbs completed work;
- narrows scope;
- expands scope;
- invalidates assumptions;
- simply makes earlier work unnecessary.

## 23. Plan revision semantics

A new Plan Revision is created when the route materially changes.

Existing Tasks may be:

- retained unchanged;
- revised through new Task versions;
- superseded;
- cancelled;
- newly created.

Attempts retain the Plan Revision under which they ran.

This allows later analysis of whether an execution was sensible given what was known at the time.

## 24. Goal state

Goal state should be derived primarily from Outcome Criteria.

A possible projection:

```text
defined
in_progress
blocked
awaiting_decision
verification_pending
satisfied
abandoned
superseded
```

`satisfied` requires a current Outcome Verification showing all mandatory criteria met.

A Goal is not satisfied merely because all planned Tasks are marked satisfied.

## 25. Outcome Criterion state

Each criterion may derive:

- not_evaluated;
- evidence_missing;
- verification_pending;
- satisfied;
- not_satisfied;
- waived;
- stale_evidence.

A waived criterion requires an authorised Decision.

If evidence expires or relevant state changes, a previously satisfied criterion may become `stale_evidence` without deleting the historical successful verification.

## 26. Blocker state

A Blocker is current only while its condition remains unsatisfied.

Blockers should be derived or reconciled from facts where possible.

Example:

```text
Blocker:
  Worker unavailable

New availability observation:
  Worker available

Projection:
  blocker resolved
```

The historical blocker record remains useful for audit but no longer blocks execution.

## 27. Availability state

Availability is always time-bound.

A projection should prefer:

1. fresh direct observation;
2. fresh provider-derived observation;
3. explicit scheduled unavailability;
4. otherwise unknown.

Predicted reset times or quota estimates are advisory unless verified by current observation.

`unknown` must not be silently treated as available.

## 28. Integration state

Integration should track at least:

- candidate identity;
- intended target;
- target base/version;
- reconciliation status;
- conflict findings;
- combined verification;
- external native result;
- accepted/rejected/partial outcome.

If the target moves, the candidate may become stale even if all prior verifications remain historically valid.

## 29. Notification state

Notifications are derived delivery records.

An event becoming notify-worthy should create a notification intent.

Delivery state may include:

- pending;
- sent;
- acknowledged;
- failed;
- suppressed;
- deduplicated.

Failure to deliver a notification must not erase the underlying Human Gate, Blocker or Decision requirement.

## 30. Crash recovery

After restart Orchestra should be able to reconstruct:

- current orchestration epoch;
- current Goals and Plan Revisions;
- active Tasks;
- outstanding Human Gates;
- in-flight Attempts;
- unresolved external operations;
- last observations;
- pending reconciliation work.

For any in-flight Attempt or operation, Orchestra should default to **reconcile first** rather than assume it failed during downtime.

## 31. Worker reconnection

A returning Worker should present its Attempt identity and last known Instruction.

Orchestra checks:

- Attempt still exists;
- Worker is still assigned;
- Attempt has not been cancelled/superseded;
- instruction is still current;
- result has not already been reconciled;
- current authority permits continuation.

If any check fails, work should stop or return for reconciliation rather than silently continue.

## 32. Webhooks, polling and event streams

External change delivery mechanisms are advisory transport.

Whether Orchestra learns about a change through:

- webhook;
- polling;
- manual observation;
- worker report;
- connector;
- scheduled reconciliation;

the same reconciliation semantics apply.

No transport is itself authoritative merely because it is real-time.

## 33. Conflict between Orchestra and external reality

If Orchestra says:

```text
PR expected open
```

but GitHub authoritatively reports:

```text
PR merged
```

Orchestra must not defend its stale projection.

It should:

1. preserve the new observation;
2. reconcile meaning;
3. update derived state;
4. evaluate whether any Task, Attempt, Integration or Plan consequence follows;
5. record any unexpected external action if authority policy was violated.

External systems own their native facts. Orchestra owns interpretation and orchestration consequence.

## 34. Conflict between two external sources

When two sources disagree:

- prefer the system authoritative for the native fact;
- preserve both observations if materially relevant;
- do not collapse uncertainty into false certainty;
- create a reconciliation Blocker if authority cannot be established.

Example: a worker says a PR merged while GitHub says open. GitHub wins for merge state; the worker report remains historical.

## 35. Minimal event vocabulary

This is not a commitment to event sourcing, but the system must be able to represent equivalent facts to:

```text
goal_defined
plan_revised
task_created
task_superseded
attempt_created
attempt_assigned
instruction_issued
attempt_result_returned
external_observed
verification_recorded
decision_recorded
human_gate_opened
human_gate_resolved
failure_recorded
recovery_chosen
artifact_registered
integration_recorded
reconciliation_completed
outcome_verification_recorded
goal_satisfied
```

Whether these become literal events, audit records or transactional history is deferred.

## 36. Transactional boundaries

Within Orchestra-owned state, transitions that must remain consistent should commit atomically.

Examples:

- opening a Human Gate and marking its dependency unsatisfied;
- creating a retry Attempt and linking it to the prior Failure;
- accepting a Result and scheduling reconciliation;
- superseding a Task and making the successor current;
- recording an Outcome Verification and updating goal projection.

External side effects cannot usually share the same database transaction. Those require operation identity + reconciliation.

## 37. Security implications

The state model requires later security design for:

- who may issue authoritative commands;
- how coordinator epochs are authenticated;
- worker identity;
- Decision owner identity;
- external adapter credentials;
- audit immutability;
- secret minimisation;
- least-privilege Access;
- Authority enforcement independent of mere possession of credentials.

Credential possession must never automatically imply orchestration Authority.

## 38. State model scenario tests

### 38.1 Worker says done; coordinator was offline

1. Attempt remains in-flight.
2. Worker result arrives or is later discovered.
3. Result is appended.
4. Reconciliation observes external artifact.
5. Verification runs if required.
6. Task projection changes only after acceptance predicates are met.

No human has to manually "remember" that the worker completed.

### 38.2 Old coordinator wakes up

1. Old coordinator holds epoch 18.
2. Current project epoch is 21.
3. Its state-changing command is rejected.
4. Any external results it discovered can be submitted as observations.
5. Current coordinator reconciles them.

Split-brain cannot legally advance Orchestra truth.

### 38.3 Deploy call times out

1. durable operation `deploy-41` exists;
2. provider call times out;
3. operation becomes unknown outcome;
4. reconciliation queries provider/environment;
5. if deployment occurred, record success;
6. if definitively absent, retry according to policy;
7. never blindly duplicate the deployment.

### 38.4 PR passed source tests, then main moved

1. previous Verification records remain PASS;
2. new observation shows base drift;
3. integration suitability becomes stale;
4. task enters reconciliation projection;
5. source tests are not rewritten as failures;
6. new integration verification may be required.

### 38.5 Human approved, then evidence changed

1. Decision remains historical;
2. new evidence invalidates a dependency predicate;
3. production action remains blocked;
4. new Human Gate may be opened;
5. old approval is not falsified, but no longer sufficient.

### 38.6 Retry after partial useful work

1. Attempt 1 fails.
2. Artifact A remains registered.
3. Recovery chooses retry.
4. Attempt 2 explicitly inherits Artifact A.
5. Attempt 2 result produces Artifact B.
6. verification targets B.
7. Task may become satisfied while Attempt 1 remains failed.

## 39. Minimal persistence obligations

Before Orchestra can safely execute real autonomous work, it must durably preserve at least:

- project identity;
- Goal and Outcome Criteria;
- current and historical Plan Revisions;
- Tasks and dependency predicates;
- Attempts and Instructions;
- orchestration epoch/authority;
- Decisions and Human Gates;
- Artifact references/versions;
- External Observations used for consequential decisions;
- Verification records;
- Failures and Recovery Actions;
- supersession links;
- unresolved external operation identities;
- reconciliation history sufficient for recovery;
- Outcome Verifications.

Caching and projections can be rebuilt.

## 40. What can remain external

Orchestra should not copy external state merely to feel complete.

Examples that can remain external with durable references/observations:

- Git repository content;
- full CI logs;
- complete provider job objects;
- cloud infrastructure inventory;
- large datasets;
- model binaries;
- external tickets;
- deployment logs.

Import only what Orchestra needs for identity, evidence, recovery, reasoning or audit.

## 41. State authority rule of thumb

When uncertain, ask:

> If this fact changed outside Orchestra, which system would actually make it true?

If the answer is GitHub, cloud provider, database, filesystem or another external system, that system owns the native fact.

Then ask:

> What does that fact mean for the Goal, Plan, Task, Authority, Verification or Recovery?

That meaning is Orchestra's responsibility.

## 42. Consequences for implementation

This model strongly suggests, without yet mandating:

- durable transactional persistence;
- immutable history for consequential changes;
- optimistic concurrency/version checks;
- coordinator epoch/lease semantics;
- adapter-based external observations;
- explicit operation IDs for side effects;
- idempotent reconciliation;
- rebuildable projections;
- first-class unknown outcome;
- first-class supersession;
- policy-driven authority;
- evidence-linked transitions.

These implications should be tested in ADRs before selecting the implementation architecture.

## 43. Next foundation work

The next foundation documents should define:

1. **Failure and Recovery Model** — classification, retry policy, unknown outcomes, preserved artifacts, escalation and disaster recovery.
2. **Security and Authority Model** — identities, credentials, permissions, trust boundaries and human authority.
3. **Testing Model** — proof levels from unit/source through external/runtime/outcome verification.
4. **Learning Mode Model** — how explanations and knowledge adaptation attach to real work without becoming authority.
5. **ADRs** — only after the above establish enough constraints to choose persistence and implementation architecture.

The first ADR should likely answer the most consequential architectural question exposed here:

> What persistence/state architecture best supports transactional authoritative state, append-preserving history, rebuildable projections, optimistic concurrency and reliable reconciliation without making Orchestra unnecessarily complex?
