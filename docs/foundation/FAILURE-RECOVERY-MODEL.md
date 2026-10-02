# Orchestra Failure and Recovery Model v1

**Status:** Foundation proposal  
**Version:** 1  
**Date:** 2 October 2026  
**Purpose:** Define how Orchestra classifies failures, preserves useful work, recovers safely, decides when to retry or reconcile, and escalates when automation should stop.

## 1. Core recovery principle

Recovery should restore **correct orchestration**, not merely restart execution.

A failure does not automatically mean:

- the Task failed;
- the Artifact is unusable;
- the Worker is unusable;
- the provider is unavailable;
- the Goal is blocked;
- the operation did not happen.

The first recovery question is:

> What is still true, what is uncertain, and what evidence do we have?

Only then should Orchestra choose retry, reconcile, reassign, replan, escalate or stop.

## 2. Recovery objectives

Recovery should optimise for, in this order:

1. preserve truth;
2. prevent duplicated or unsafe side effects;
3. preserve useful work;
4. restore authoritative coordination;
5. resume progress;
6. minimise unnecessary repeated work;
7. explain what happened and what changed.

Speed is secondary to correctness when the outcome is uncertain or the action is consequential.

## 3. Failure taxonomy

Failures are classified by **where they occur** and **what they imply**.

### 3.1 Worker failure

The assigned Worker cannot continue or return a valid result.

Examples:

- agent crashes;
- session disappears;
- process exits;
- human becomes unavailable;
- provider quota blocks execution.

Worker failure does not imply the Task or Artifact failed.

### 3.2 Attempt execution failure

A particular Attempt cannot satisfy its execution contract.

Examples:

- implementation error;
- command exits non-zero;
- tool misuse;
- unsatisfied runtime prerequisite;
- worker returns an explicit failed result.

Another Attempt may still succeed.

### 3.3 Tool failure

A tool used by a Worker fails while the Worker itself remains viable.

Examples:

- browser automation error;
- Git client error;
- compiler unavailable;
- database CLI unavailable.

Recovery may replace or repair the tool without replacing the Worker.

### 3.4 Resource failure

A required Resource is unavailable or degraded.

Examples:

- host offline;
- GPU unavailable;
- disk full;
- database unavailable;
- repository temporarily unreachable.

Recovery may wait, switch resource or replan.

### 3.5 Adapter failure

Orchestra cannot communicate correctly with an external system.

Examples:

- API error;
- connector bug;
- malformed response;
- authentication failure;
- unsupported endpoint;
- webhook delivery gap.

The external system may still be functioning normally.

### 3.6 Access failure

The actor cannot technically reach the required resource.

Examples:

- missing permission;
- expired credential;
- repository inaccessible;
- environment login unavailable.

Access failure must not be misclassified as lack of Capability.

### 3.7 Authority failure

The actor has technical Access but lacks permission under Orchestra policy to perform the action.

Examples:

- Worker can merge but is not authorised to merge;
- deployment credentials exist but production approval is missing;
- payment API is reachable but no spending authority exists.

Authority failure is not retriable by technical retry.

### 3.8 Dependency failure

A required predicate is false or can no longer be established.

Examples:

- required PR is not merged;
- required dataset version is missing;
- required Decision was rejected;
- required environment no longer matches expected version.

Recovery normally means wait, satisfy dependency, replan or supersede.

### 3.9 Verification failure

Evidence shows the evaluated claim is false.

Examples:

- test fails;
- runtime behaviour differs from contract;
- browser QA exposes defect;
- security review rejects candidate.

Verification failure should not be converted into infrastructure failure.

### 3.10 Integration failure

Valid standalone work cannot safely integrate into the current authoritative target.

Examples:

- incompatible branch drift;
- semantic conflict;
- conflicting migrations;
- target contract changed.

Recovery may adapt, rebase, partially reuse or supersede.

### 3.11 External side-effect failure

An external operation definitively did not achieve the intended effect.

Examples:

- provider rejects deployment;
- DNS write rejected;
- PR creation request definitively fails.

This is distinct from unknown outcome.

### 3.12 Unknown outcome

Orchestra cannot determine whether an external side effect occurred.

Examples:

- timeout after form submission;
- lost connection after push;
- deployment API returns no final acknowledgement;
- payment request connection drops after send.

This class requires reconciliation before retry.

### 3.13 Staleness failure

The assumptions/base/context under which work was assigned are no longer current enough for safe continuation.

Examples:

- main advanced;
- policy changed;
- Human Gate context changed;
- target environment changed.

The work may still be useful.

### 3.14 Coordinator failure

The active Orchestra coordinator loses the ability to safely advance authoritative state.

Examples:

- process crash;
- host failure;
- lease expiry;
- split-brain detection;
- persistent storage unavailable.

Recovery must restore orchestration authority before normal scheduling resumes.

### 3.15 Persistence failure

Orchestra cannot durably record authoritative state.

This is a hard stop for any action whose safe semantics require durable intent before side effects.

If Orchestra cannot record intent safely, it must not proceed with consequential side effects.

## 4. Failure severity

Severity is based on consequence, not technical drama.

Suggested classes:

### S0 — informational

No progress impact; record only if useful.

### S1 — local recoverable

A single tool/attempt failure with safe automatic recovery.

### S2 — task-blocking

Task cannot continue until recovery, but project safety is intact.

### S3 — project-impacting

Multiple tasks, authoritative integration, or key resources are blocked.

### S4 — safety/authority critical

Risk of duplicated side effects, production impact, money, credential misuse, destructive action, split-brain, corrupted authoritative state or unknown consequential outcome.

S4 normally suppresses blind automation until reconciliation or authorised human review.

## 5. Retryability classes

Every recoverable failure should be classified as one of:

### 5.1 Safe immediate retry

The operation is read-only or provably idempotent and failure is transient.

Examples:

- re-fetch PR status;
- retry read-only API query;
- rerun deterministic parser.

### 5.2 Safe retry after backoff

Transient provider/resource failure where repetition cannot duplicate a consequential side effect.

Examples:

- rate-limited read;
- unavailable test runner;
- temporary host outage.

### 5.3 Retry only after reconciliation

The previous operation may have succeeded externally.

Examples:

- deploy timeout;
- push disconnect;
- payment send timeout;
- browser form submit timeout.

### 5.4 Retry with changed execution context

The prior path is unsuitable but Task remains valid.

Examples:

- switch Worker;
- switch Tool;
- change Resource;
- update Instruction;
- rebase on current target.

### 5.5 Not automatically retriable

Retry cannot fix the cause.

Examples:

- human rejected Decision;
- missing Authority;
- verification proves design wrong;
- contractual constraint violated;
- Task superseded.

These require replan, new Decision, correction or cancellation.

## 6. Retry budget

Retry budget is policy, not an arbitrary universal count.

It may consider:

- operation cost;
- consequence;
- historical reliability;
- expected transient duration;
- provider limits;
- human interruption cost;
- artifact preservation;
- whether failure mode changed.

Repeated identical retries without new evidence are discouraged.

A retry budget exhaustion should create a Blocker or escalation, not silently mark the Goal failed.

## 7. Recovery decision order

When a failure occurs, Orchestra should evaluate in this order:

```text
1. Did a consequential external side effect possibly occur?
   yes -> reconcile first

2. Is authoritative Orchestra state durable and trustworthy?
   no -> restore state authority/persistence before continuing

3. Is the current Instruction/base/context still valid?
   no -> reconcile/replan/supersede

4. Does useful output already exist?
   yes -> preserve and register it

5. Is the same Worker/Tool/Resource still suitable?
   no -> change execution context

6. Is retry technically safe and policy-authorised?
   yes -> create new Attempt/retry action

7. Is a human Decision required?
   yes -> Human Gate

8. Otherwise:
   create explicit Blocker and stop automatic progress
```

## 8. Preserve before replace

Before terminating or replacing a failed Worker/Attempt, Orchestra should attempt to preserve:

- working tree/workspace;
- produced files;
- commits;
- draft PRs;
- logs;
- test outputs;
- partial datasets;
- checkpoints;
- model weights;
- provider operation IDs;
- external object references.

Preservation must not itself create unsafe side effects.

## 9. Artifact salvage

A failed Attempt may still produce valid Artifacts.

Artifact salvage process:

1. identify artifact/version;
2. record provenance from failed Attempt;
3. assess integrity;
4. verify independently where needed;
5. mark role as reusable, partial, stale or unsuitable;
6. explicitly attach to Recovery Action/new Attempt if reused.

Useful work should never be lost merely because its producing Attempt failed.

## 10. Unknown outcome recovery

Unknown outcome has strict semantics.

### 10.1 Rules

- do not claim success;
- do not claim failure;
- do not repeat the operation blindly;
- preserve operation identity;
- query the authoritative external system;
- inspect target state;
- reconcile before retry.

### 10.2 Resolution states

Unknown outcome resolves into:

- confirmed_success;
- confirmed_failure;
- confirmed_partial;
- still_unknown.

If still unknown and consequence is high, automation stops and may require human review.

## 11. Reconciliation-first operations

The following should default to reconciliation before retry when acknowledgement is lost:

- money movement;
- destructive operations;
- production deployment;
- DNS/domain changes;
- messages or notifications where duplicates matter;
- account creation;
- external booking/order creation;
- merge/integration actions;
- irreversible data migration;
- provider resource creation.

## 12. Backoff

Backoff is appropriate when the likely cause is transient.

It may consider:

- provider retry-after guidance;
- exponential backoff;
- jitter;
- known reset windows;
- operation urgency;
- cost.

Backoff must not replace evidence. A Worker should not sleep for a guessed quota reset when availability can be observed directly later.

## 13. Worker replacement

Worker replacement is justified when:

- Worker unavailable beyond useful threshold;
- repeated execution failures are Worker-specific;
- missing Capability;
- missing Access;
- cost/latency policy selects another Worker;
- Task requires independent review.

Replacement creates a new Attempt.

The predecessor Attempt remains historical.

## 14. Tool replacement

Tool replacement is preferred over Worker replacement when the failure is clearly tool-local.

Example:

```text
Worker A
  browser automation fails

Alternative:
  same Worker + different browser/tool

not automatically:
  abandon Worker A
```

## 15. Resource failover

Failover may switch:

- host;
- database replica;
- compute;
- test environment;
- provider region;
- model/provider.

Failover is allowed only if semantic requirements remain satisfied.

Different resource != equivalent resource unless policy/evidence establishes equivalence.

## 16. Replan versus retry

Retry is appropriate when the **route remains valid** and execution failed.

Replan is appropriate when the **route itself is no longer justified**.

Signals for replan:

- assumptions invalidated;
- dependency graph materially changed;
- repeated failures expose architectural weakness;
- better route discovered;
- cost/risk changed materially;
- Goal changed;
- external system capability changed;
- integration target evolved beyond cheap adaptation.

## 17. Supersession versus cancellation

### Supersede

Use when new work or intent replaces earlier work while preserving historical value.

### Cancel

Use when the work should no longer proceed and no successor is implied.

Neither means prior artifacts are invalid.

## 18. Human escalation triggers

Human escalation is required when:

- policy explicitly reserves the Decision to a human;
- recovery changes Goal intent;
- production/destructive/money action requires approval;
- unknown outcome remains unresolved for high-consequence action;
- credentials/identity compromise is suspected;
- conflicting authoritative sources cannot be resolved;
- automatic recovery would incur material cost/risk;
- repeated recovery attempts reveal a product or strategic decision;
- evidence is insufficient to choose safely.

Human escalation should include:

- what happened;
- what is known;
- what is unknown;
- what has been preserved;
- safe options;
- consequences/trade-offs;
- recommendation only if policy permits a technical recommendation;
- exact action the Decision unlocks.

## 19. Escalation should be rare but meaningful

Orchestra should not escalate routine reversible technical failures.

Examples suitable for autonomous recovery:

- rerun failed source test;
- refresh stale PR observation;
- reassign from unavailable Worker;
- recreate disposable test environment;
- reconcile completed external work.

Examples unsuitable for silent autonomy:

- production DNS cutover after uncertain state;
- charge card again after payment timeout;
- delete data to unblock migration;
- change user's stated Goal;
- accept security risk;
- spend beyond delegated budget.

## 20. Recovery actions

A Recovery Action should state:

- triggering Failure/Blocker;
- selected strategy;
- preserved state/artifacts;
- reset state;
- new Attempt or Task if created;
- changed Worker/Tool/Resource;
- retry/reconciliation operation ID;
- authority basis;
- expected success signal;
- stop/escalation condition.

## 21. Crash recovery

After Orchestra restarts:

1. acquire a new valid orchestration authority epoch;
2. load durable state;
3. identify in-flight Attempts;
4. identify unresolved external operations;
5. identify open Human Gates;
6. mark volatile observations stale where appropriate;
7. reconcile external systems;
8. ingest late Worker results;
9. rebuild projections;
10. only then resume scheduling.

No blanket conversion of in-flight work to failed is allowed.

## 22. Coordinator failover

When a coordinator dies:

- new coordinator obtains new epoch;
- old epoch becomes invalid for writes;
- in-flight Workers may continue externally;
- their late results remain acceptable as observations/results;
- all uncertain operations are reconciled;
- no duplicate Attempts are created until current external state is understood.

## 23. Split-brain response

If two coordinators appear active:

1. authoritative epoch decides which may write;
2. stale coordinator commands are rejected;
3. side effects initiated by stale coordinator are treated as external reality;
4. reconciliation discovers any divergence;
5. high-consequence divergence may trigger Human Gate;
6. stale coordinator is isolated before normal scheduling continues.

## 24. Persistence outage

If authoritative persistence is unavailable:

Allowed:

- read-only observation where safe;
- collection of non-authoritative diagnostics;
- preservation of external evidence.

Not allowed:

- new consequential side effect requiring durable intent;
- new Human Gate resolution;
- authoritative Task transition;
- new production action;
- new spend/destructive action.

Once persistence returns, observations gathered during outage are reconciled.

## 25. Credential failure

Credential failure is classified separately from provider failure.

Recovery:

- do not repeatedly hammer authentication;
- identify which Access Grant failed;
- determine expiry/revocation/misconfiguration;
- use alternate credential only if explicitly authorised;
- never broaden permission automatically;
- escalate if privileged/manual reauthentication is required.

## 26. Security-sensitive failure

Potential compromise indicators include:

- unexpected privileged action;
- credential misuse;
- unexplained external state mutation;
- worker identity mismatch;
- invalid signature;
- unauthorised coordinator writes.

Response:

1. stop affected authority path;
2. preserve evidence;
3. revoke/contain where authorised;
4. avoid destructive cleanup that erases evidence;
5. require Security/Authority policy handling.

Detailed incident response belongs in Security Model.

## 27. Verification failure recovery

Verification failure should produce correction work, not repeated verification of unchanged artifacts.

Valid responses:

- create corrective Task;
- return to producing Attempt/Worker;
- assign independent specialist;
- supersede Artifact;
- revise Plan.

Invalid response:

- rerun identical test indefinitely hoping it turns green without any changed condition.

## 28. Integration failure recovery

Possible strategies:

- refresh base;
- rebase;
- merge target changes;
- adapt candidate;
- split candidate;
- reuse unaffected Artifacts;
- create dedicated integration Task;
- supersede candidate.

Original standalone verification remains historical.

## 29. External drift recovery

External drift means native state changed outside Orchestra.

Examples:

- human merged PR manually;
- DNS changed outside workflow;
- deployment modified manually;
- branch force-pushed;
- account permission changed.

Response:

1. observe authoritative external truth;
2. preserve drift evidence;
3. classify authorised vs unexpected;
4. reconcile orchestration meaning;
5. update/replan/reverify as needed;
6. investigate authority violation if applicable.

Do not overwrite external reality merely to make Orchestra's projection look consistent.

## 30. Stale-work recovery

When work is stale:

- preserve the old Attempt/Artifact;
- identify changed assumptions/base;
- evaluate compatibility;
- reuse unchanged portions;
- create reconciliation/integration Task;
- supersede only when justified.

Stale does not mean discard.

## 31. Timeouts

Timeout is an observation about elapsed time, not proof of failure.

Different timeout types:

- worker heartbeat timeout;
- operation acknowledgement timeout;
- execution deadline timeout;
- provider request timeout.

Each must specify whether the underlying work may still be running or may have completed.

Recovery follows that semantic, not the word "timeout".

## 32. Heartbeats

Heartbeats can indicate liveness but not useful progress.

A healthy heartbeat must not prevent detection of:

- stuck execution;
- stale instruction;
- endless loop;
- no artifact production;
- blocked dependency.

Progress evidence should be distinct from liveness.

## 33. Stuck work

Potential stuck-work signals:

- no progress evidence beyond threshold;
- repeated identical output;
- no changed external state;
- expired dependency observation;
- Worker alive but no milestone advancement.

Recovery may:

- request checkpoint;
- reconcile;
- pause;
- reassign;
- escalate.

Thresholds are policy, not hard-coded universal constants.

## 34. Partial success

An Attempt may partially satisfy intended work.

Partial success should record:

- completed subset;
- incomplete subset;
- valid artifacts;
- invalid/unverified artifacts;
- remaining dependencies;
- whether new Task decomposition is warranted.

Do not flatten partial success into binary failure.

## 35. Recovery provenance

Every recovery should preserve linkage:

```text
Failure
  -> Recovery Action
      -> new Attempt / Task / Reconciliation
          -> Result
              -> Verification
```

This allows later learning about which recovery patterns are effective.

## 36. Recovery policy layers

Recovery policy can exist at:

- global Orchestra level;
- Project level;
- environment level;
- Task type;
- operation type;
- specific resource/provider.

More specific policy may tighten but should not silently weaken mandatory safety rules.

## 37. Autonomous recovery boundary

Automatic recovery is allowed when:

- action remains within delegated Authority;
- consequence is bounded;
- side effect is reversible or idempotent;
- required state is durable;
- unknown outcome is resolved;
- retry/recovery policy permits it;
- no Human Gate is required.

If these conditions fail, stop and escalate.

## 38. Recovery from exhausted worker/provider quotas

Quota exhaustion should be based on fresh evidence.

Possible recovery:

- choose another Worker/provider with equivalent Capability;
- wait for verified availability;
- replan non-blocked work;
- preserve current Attempt state;
- avoid falsely marking Task failed.

Predicted reset time is advisory until observed.

## 39. Recovery from missing environment

If a required environment is unavailable:

- do not fake the environment;
- run only verification scopes that are genuinely supported;
- record what was not tested;
- use alternative equivalent environment only if equivalence is established;
- leave higher-level acceptance pending.

## 40. Recovery from bad instruction

If Instruction itself is incorrect or stale:

- stop current Attempt where safe;
- preserve work;
- supersede Instruction;
- create corrected Instruction/new Attempt;
- record why;
- do not blame Worker for correctly following a bad instruction.

## 41. Recovery from ambiguous ownership

If two Tasks/Workers may be modifying the same scope:

- pause overlapping work;
- reconcile current artifacts;
- determine ownership;
- create integration/replan work;
- resume only after scopes are safe.

This converts collision risk into explicit coordination.

## 42. Project-level degradation mode

A Project may enter degraded mode when:

- key provider unavailable;
- external authority uncertain;
- coordinator recovering;
- persistence degraded;
- multiple critical Blockers exist.

In degraded mode:

- safe read-only/reversible work may continue;
- risky side effects are suspended;
- human is notified only if actionable;
- recovery progress remains visible.

Degraded is not Goal failure.

## 43. Disaster recovery

Disaster recovery concerns loss of Orchestra infrastructure itself.

At minimum, design must eventually support recovery of:

- authoritative state store;
- historical/audit records;
- encrypted configuration references;
- adapter configuration;
- project identities;
- unresolved operations;
- Human Gates;
- orchestration epoch ownership.

Backups must be tested by restore, not assumed valid.

Exact RPO/RTO are future deployment decisions.

## 44. Recovery testing requirements

Recovery must be tested, not only documented.

Future tests should include:

- coordinator crash mid-Attempt;
- crash after external side effect but before acknowledgement;
- duplicated webhook;
- delayed Worker result;
- stale coordinator command;
- credential expiration;
- provider read outage;
- persistence outage;
- branch drift during execution;
- failed verification with useful Artifact;
- repeated quota exhaustion;
- manual external mutation;
- recovery after restart with unresolved Human Gate.

## 45. Worked scenario tests

### 45.1 Agent goes idle, useful branch exists

```text
Attempt A
  Worker unavailable
  branch with commits exists

Failure
  worker_failure

Recovery
  preserve branch
  register Artifact Version
  reconcile against current target
  create Attempt B with new Worker
  inherit Artifact

Task
  remains active
```

### 45.2 Quota estimate says exhausted but provider works

```text
Old observation
  quota unavailable

New direct observation
  Worker can execute

Recovery
  invalidate stale availability projection
  remove blocker
  resume work

Historical old observation
  preserved
```

### 45.3 Deploy timeout

```text
operation deploy-92
provider request sent
connection lost

State
  unknown_outcome

Recovery
  query provider/environment
  if deployed -> record success
  if absent -> retry same intended operation safely
  if ambiguous -> escalate based on consequence
```

### 45.4 Source test failure due stale build guard

```text
Verification fails
investigation shows test itself stale

Recovery
  do not classify Artifact as defective automatically
  verify failure source
  correct stale guard under authorised Task
  rerun appropriate verification
```

### 45.5 Human approval exists but production state changed

```text
Decision
  approved candidate X

later observation
  candidate/target changed

Recovery
  previous Decision remains historical
  approval predicate invalidated
  open new Human Gate if production action still desired
```

### 45.6 Coordinator crashes while worker finishes

```text
Coordinator epoch 14 dies
Worker completes PR

New coordinator epoch 15
  reconciles worker result + GitHub
  registers Artifact
  verifies
  advances Task

No duplicate worker assignment required
```

## 46. Recovery anti-patterns

Orchestra should explicitly avoid:

- retry everything;
- kill and restart everything;
- mark every timeout failed;
- discard failed-worker artifacts;
- assume provider status from stale quota estimates;
- overwrite historical failures after successful retry;
- treat human silence as approval;
- continue stale Instructions because Worker already started;
- automatically broaden credentials;
- re-run unchanged failing verification endlessly;
- declare Goal failure because one Task route failed;
- duplicate consequential side effects after unknown outcome;
- trust liveness heartbeat as proof of progress.

## 47. Metrics worth learning from

Future Orchestra may learn from:

- failure frequency by class;
- recovery success rate;
- retries before success;
- artifact salvage rate;
- time spent blocked vs executing;
- false-positive availability blockers;
- unknown-outcome incidence;
- human escalation frequency;
- stale-work frequency;
- integration conflict frequency;
- repeated failure signatures.

Metrics should improve policy, not silently redefine user goals.

## 48. Relationship to future Learning Mode

Recovery is a valuable learning surface.

Learning Mode may explain:

- what failed;
- why Orchestra chose reconcile vs retry;
- what was preserved;
- why a human decision is required;
- what evidence resolved uncertainty;
- how the same failure could be diagnosed manually.

Explanation remains separate from authoritative recovery state.

## 49. Next foundation step

The next document should define the **Security and Authority Model**:

- identity of humans, Workers and coordinators;
- authentication;
- Access versus Authority;
- credential handling;
- least privilege;
- approval policies;
- privileged operations;
- audit protection;
- compromise containment.

That model should use this recovery framework for security-sensitive failure and containment, rather than inventing separate semantics.
