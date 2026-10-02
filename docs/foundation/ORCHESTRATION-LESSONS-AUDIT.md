# Orchestration Lessons and Failure-Mode Audit

**Status:** Living foundation document  
**Initial audit:** 2 October 2026  
**Purpose:** Convert observed orchestration experience into durable design requirements for Orchestra.

## 1. Why this document exists

Orchestra should not be designed from an idealised workflow. It should be shaped by failures and successes observed while coordinating real long-running work.

Delnavazan is used here as evidence because it has exercised multiple coordination models: chat-led coordination, local control-plane automation, deterministic supervision, multiple AI workers, GitHub branches and pull requests, review gates, staging work, human decisions, provider limits and recovery from failed orchestration attempts.

The lessons are project-agnostic. Orchestra must not contain Delnavazan-specific assumptions.

This document is intentionally living. New evidence may refine a lesson, add a failure mode, or invalidate an earlier assumption.

## 2. What was already learned before this audit

The existing Orchestra vision already captures the most important high-level conclusions:

- Orchestra must preserve goal fidelity rather than equating activity with success.
- providers, models, repositories, servers and workflow engines are capabilities, not Orchestra's identity;
- the human owns the goal and consequential human decisions;
- evidence, uncertainty, decisions and recovery are first-class concerns;
- Orchestra should integrate mature systems rather than unnecessarily reimplement them;
- Learning Mode should increase human capability while work proceeds.

This audit does not replace those principles. It makes their operational consequences explicit.

## 3. Observed failure modes and required invariants

### 3.1 Multiple control planes create split reality

**Observed pattern:** More than one queue, watcher, bridge, continuity process or coordinator can believe it owns the same work. Stopping one process is insufficient if another service respawns it or another queue continues issuing work.

**Failure:** Two systems can legitimately report different task state, wake different workers, or advance incompatible work.

**Required invariant:** Every project execution scope has exactly one authoritative orchestration lifecycle at a time. Other systems may observe or execute, but they may not independently advance authoritative state.

**Design consequence:** Authority identity and lease/epoch must be explicit. Orchestra must be able to detect and reject stale coordinators.

### 3.2 Recorded state must not outrank physical evidence

**Observed pattern:** A ledger reported exhaustion or failure after the underlying work had succeeded; old failure records survived a valid retry; quota telemetry produced blockers that were not grounded in actual provider availability.

**Failure:** The orchestrator acts on stale interpretation instead of current reality.

**Required invariant:** State transitions that matter require evidence. Derived status must be recomputable from durable facts wherever practical.

**Design consequence:** Separate observation, interpretation and authoritative transition. Preserve the evidence used for every consequential transition.

### 3.3 Later failures must not erase accepted success

**Observed pattern:** Valid completed work was later obscured by retry errors or downstream failures.

**Failure:** A successful artifact becomes effectively lost because a later step failed.

**Required invariant:** Accepted artifacts and verified milestones are monotonic historical facts. Later failure may block integration or the goal, but must not rewrite earlier verified work as if it never happened.

**Design consequence:** Task attempt state, artifact acceptance, integration state and goal state must be separate concepts.

### 3.4 Completion is not one state

**Observed pattern:** Work can be implemented but unreviewed, reviewed but not integrated, integrated but not deployed, deployed but not runtime-verified, or all of those while still failing the original user outcome.

**Failure:** A single `complete` flag creates false certainty.

**Required invariant:** Orchestra must distinguish at least execution completion, artifact verification, integration, deployment/activation where relevant, outcome verification and goal satisfaction.

**Design consequence:** Completion should be evidence-backed and layered, not boolean.

### 3.5 Dependency state must be semantically correct

**Observed pattern:** Downstream work was allowed to run while a predecessor was only `merge_pending`.

**Failure:** A dependency graph based on coarse status allows work to start from an authority or artifact that is not yet actually available.

**Required invariant:** Dependencies name the condition they require: artifact produced, reviewed, merged, deployed, activated, human-approved, data available, etc.

**Design consequence:** Dependencies should bind to capabilities/evidence predicates, not merely another task's generic status.

### 3.6 Work identity and correlation are mandatory

**Observed pattern:** Wake paths and result-return paths became reliable only when instructions and results could be correlated exactly.

**Failure:** A worker can return valid work that the coordinator cannot safely associate with the instruction, attempt or base state that produced it.

**Required invariant:** Every execution attempt has durable identity linking instruction, project state/base, worker, artifacts, result, verification and follow-up.

**Design consequence:** Handoffs need machine-readable envelopes even when the payload is human-readable.

### 3.7 Ownership boundaries prevent parallelism from becoming collision

**Observed pattern:** Adjacent agents modified overlapping surfaces, direct-to-main work complicated integration, and unclear ownership caused duplicated or conflicting effort.

**Successful correction:** Per-agent branches, explicit surface ownership, scoped commits, early pushes, draft PRs and no silent boundary crossing made parallel work safer.

**Required invariant:** Concurrent work must declare its write scope and authority boundaries. Orchestra must detect likely overlap before or during execution.

**Design consequence:** Task planning needs ownership/scope metadata and conflict detection. Parallelism is conditional, not automatically desirable.

### 3.8 Integration is first-class work

**Observed pattern:** Independently valid candidates required deliberate conflict resolution, preservation of provenance, combined verification and sometimes adaptation of tests to the integrated truth.

**Failure:** Treating merge as clerical work hides one of the highest-risk reasoning steps.

**Required invariant:** Integration can itself be a task with inputs, owner, conflict decisions, verification and evidence.

**Design consequence:** Orchestra's model must represent candidate artifacts and integration artifacts separately.

### 3.9 Capability, availability and authority are different

**Observed pattern:** A worker may technically be able to code but lack quota; may be available but lack repository access; may have access but not be authorised to merge/deploy; a human may be the only valid authority for a consequential decision.

**Required invariant:** Selection must independently evaluate:
1. capability — can this worker/tool perform the work?
2. availability — can it perform it now?
3. access — can it reach required resources?
4. authority — is it permitted to perform the consequential action?

**Design consequence:** Worker selection cannot be a single priority list.

### 3.10 Availability must be evidence-based and time-aware

**Observed pattern:** Estimated quota percentages and reset times became false blockers. At other times a worker was genuinely unavailable for hours.

**Required invariant:** Availability should be represented as observed available, observed unavailable, or unknown, with evidence and freshness. Predictions may be advisory but must not silently become authority.

**Design consequence:** Resource/quota telemetry needs provenance, expiry and confidence.

### 3.11 Wake-up is not orchestration

**Observed pattern:** Browser wake mechanisms, hourly polling, scheduled agents and CLI launching can make a worker run, but none guarantees it received the current instruction, has the right context, or can safely advance the project.

**Required invariant:** Scheduling/waking is a transport concern. Before execution, a worker must reconcile authoritative task state and instruction identity.

**Design consequence:** Workers should pull/validate current work after waking rather than trusting stale pushed context.

### 3.12 Recovery should preserve work before replacing workers

**Observed pattern:** Blind retries and terminating failed children risked losing useful workspace state. Better recovery preserved the candidate/workspace, terminated only the exhausted execution path and repaired the handoff.

**Required invariant:** Recovery is artifact-preserving by default.

**Design consequence:** Attempt failure, worker failure and artifact invalidity are separate. Retry policy should reuse valid evidence and outputs where safe.

### 3.13 Retries require explicit reset semantics

**Observed pattern:** Previous failure records contaminated later attempts and produced false exhaustion.

**Required invariant:** A retry must declare what state is inherited, what state is reset and why.

**Design consequence:** Retry is a new attempt linked to history, not a hidden mutation of the old attempt.

### 3.14 Human gates must be durable state, not conversational memory

**Observed pattern:** Some actions legitimately required human decisions or authorisation; chat context alone was not a safe place to preserve that boundary.

**Required invariant:** A human gate records the question, available evidence/options, decision owner, decision, time, scope and what work it unlocks.

**Design consequence:** Human decisions become durable project evidence. Workers must not infer approval from silence or unrelated progress.

### 3.15 Notifications should represent actionable state transitions

**Observed pattern:** Notification mechanisms became noisy when they reflected implementation chatter rather than terminal blocks or genuine human decisions.

**Required invariant:** Notification policy is separate from event generation. Orchestra may record many events while notifying the human only when attention is useful.

**Design consequence:** Events need severity/actionability classification and deduplication.

### 3.16 GitHub is an excellent instrument, not universal authority

**Observed pattern:** GitHub branches, PRs, checks and immutable commit identity substantially improved coordination and auditability for code work. Other work — infrastructure state, browser actions, external services, human decisions, datasets and compute — cannot be truthfully reduced to GitHub.

**Required invariant:** Orchestra may use GitHub as an authoritative source for GitHub-owned facts while retaining project meaning, cross-system coordination and goal evidence in its own model.

**Design consequence:** Adapters expose external facts without making one provider Orchestra's ontology.

### 3.17 Verification must record what was not tested

**Observed pattern:** Delnavazan candidates sometimes passed source-level contracts while WordPress/MariaDB/browser/runtime suites could not execute in that environment. Good review records explicitly preserved those gaps instead of converting partial success into full success.

**Required invariant:** Verification evidence includes scope, environment, result and exclusions.

**Design consequence:** A PASS without its verification scope is incomplete data.

### 3.18 Fail-closed behavior protects truth when authority is missing

**Observed pattern:** Portal work became safer when workers were instructed not to invent missing business rules, fake data, unavailable product states or authority that belonged to the Platform.

**Required invariant:** Missing authority or canonical truth must be visible as unavailable/blocked/unknown rather than fabricated to make a workflow appear complete.

**Design consequence:** Orchestra should distinguish an implementation blocker from permission to invent a substitute.

### 3.19 Instructions become stale as the project moves

**Observed pattern:** Long-running agents and branches can begin from a valid base and become stale while other work integrates.

**Current evidence:** On 2 October 2026 Delnavazan PR #15 remained open from base `5f5c3042` while `main` had advanced 27 commits; its branch was reported as diverged. The work may still contain useful changes, but its original assumptions and integration plan can no longer be accepted without reconciliation.

**Required invariant:** Before consequential execution or integration, Orchestra checks whether the instruction's assumptions, dependencies and base evidence are still current.

**Design consequence:** Staleness is first-class. A task may need rebase/reconciliation, replanning, supersession or cancellation rather than a blind continuation.

### 3.20 Superseded work is not failed work

**Observed pattern:** Newer integrated work can absorb or invalidate the need for an older candidate. Leaving such work merely `open` or `failed` loses meaning.

**Required invariant:** Orchestra must represent supersession explicitly and preserve the relationship between old intent and the newer work that replaced or absorbed it.

**Design consequence:** Task/artifact lifecycle needs `superseded` with provenance, not just closed/failed.

### 3.21 The coordinator must notice completion

**Observed pattern:** Agents completed work while the coordinating layer remained idle or unaware until a human checked.

**Required invariant:** Result discovery and reconciliation are part of orchestration. A worker finishing is not enough; the authoritative lifecycle must ingest and evaluate the result.

**Design consequence:** Orchestra needs deterministic reconciliation after external activity, not dependence on conversational attention.

### 3.22 Autonomy should be bounded by reversibility and consequence

**Observed pattern:** Routine reversible sequencing and implementation choices can proceed efficiently without human interruption, while production changes, destructive operations, money, credentials and product-direction decisions require stronger authority.

**Required invariant:** Decision authority is policy-driven and scoped by consequence, reversibility, cost and user intent.

**Design consequence:** Human involvement should be neither constant nor absent; it should be triggered by the decision class.

## 4. What the newer GitHub workflow has taught us

The current Delnavazan stage is not merely another failure source. It demonstrates several practices Orchestra should preserve:

- short-lived scoped branches make work inspectable;
- pull requests create durable handoff objects;
- explicit ownership allows useful parallelism;
- source-level CI catches cheap failures early;
- runtime/staging verification remains a separate gate;
- conflict resolution deserves its own evidence;
- canonical authority boundaries prevent UI or integration work from inventing business truth;
- a worker can safely stop at PR-ready while another capability owns review/integration;
- production/DNS/destructive boundaries can remain untouched while substantial staging progress continues.

The important conclusion is not that Orchestra should reproduce GitHub. It is that Orchestra should preserve these properties even when the underlying instrument is something else.

## 5. Initial architectural requirements derived from the audit

The domain/state model should be able to represent, without provider-specific assumptions:

- Goal and Outcome Criterion
- Plan and Plan Revision
- Task and Dependency Predicate
- Execution Attempt
- Worker
- Capability
- Access Grant / Resource Access
- Authority / Decision Policy
- Instruction / Handoff
- Artifact and Artifact Version
- Verification and Verification Scope
- Evidence
- Decision and Human Gate
- Integration Candidate and Integration Result
- External Observation
- Notification
- Failure / Blocker
- Recovery Action
- Supersession relationship
- Goal-level outcome verification

The state model must avoid collapsing these into one task-status field.

## 6. Questions deliberately left open

This audit should constrain the architecture without prematurely choosing implementation details. The following still require design work:

- Which facts must Orchestra store itself versus reference in external systems?
- What is the minimal authoritative event/state model?
- How are coordinator authority and stale-coordinator rejection implemented?
- How are dependency predicates represented without creating an overly complex workflow language?
- How should leases and long-running external work interact?
- What is the minimum evidence required for each transition?
- How are human decisions versioned when later information changes the decision context?
- How should Orchestra reconcile external systems after downtime?
- Which events warrant human notification by default?
- How much planning history is retained versus compacted?
- How should Learning Mode attach explanations to real decisions and artifacts?

These belong in the domain model, state model, failure/recovery model and ADRs that follow.

## 7. Audit rule going forward

Every significant orchestration failure or unexpectedly successful pattern should produce one of four outcomes:

1. no architectural consequence — record only if useful;
2. refinement of an existing lesson;
3. a new invariant or failure mode;
4. evidence that an existing invariant is wrong and must be changed.

The objective is not to accumulate rules. It is to make Orchestra progressively harder to fool about the real state of work while preserving useful autonomy and human control.
