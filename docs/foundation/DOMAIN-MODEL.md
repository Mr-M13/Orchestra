# Orchestra Domain Model v1

**Status:** Foundation proposal  
**Version:** 1  
**Date:** 2 October 2026  
**Purpose:** Define the durable concepts Orchestra reasons about before choosing storage, APIs, implementation language, workflow engine or user interface.

## 1. What this model is

The domain model is Orchestra's shared language for reality.

It defines **what things exist, what they mean, how they relate, and which distinctions must not be collapsed**. It does not yet define database tables, API payloads, implementation classes or the complete lifecycle of each object. Those follow from this model.

The model is deliberately project- and provider-agnostic. A GitHub pull request, trained model, configured server, research report and generated dataset can all participate without changing Orchestra's core ontology.

### 1.1 Design test

A concept belongs in the core model only when at least one of these is true:

- Orchestra must reason about it independently of a specific provider;
- confusing it with another concept has already caused or could cause orchestration failure;
- it preserves goal meaning, evidence, authority, recovery or auditability;
- multiple adapters or subsystems need a shared interpretation of it.

Provider-native detail should remain with the provider unless Orchestra needs a durable reference or observation of it.

## 2. Domain at a glance

```text
Human
  owns
Project
  contains
Goal
  is proven by
Outcome Criteria

Goal
  is pursued through
Plan Revision
  decomposes into
Tasks
  constrained by
Dependency Predicates

Task
  requires
Capability Requirements
  and may be blocked by
Human Gates / Blockers

Task
  is pursued through one or more
Execution Attempts
  assigned to
Workers
  using
Tools / Resources
  under
Access + Authority

Execution Attempt
  receives
Instruction / Handoff
  and produces or changes
Artifacts

Artifacts + External Observations + Decisions
  provide
Evidence

Evidence
  is evaluated by
Verification

Verified artifacts may enter
Integration
  producing
Integration Results

Recovery
  creates new Attempts or corrective Tasks
  without erasing history

Tasks / Artifacts / Plans
  may be
Superseded
  with provenance

Outcome Verification
  evaluates Outcome Criteria
  and determines whether the Goal
  is actually satisfied
```

No arrow above implies that every project must use every concept.

## 3. Scope and identity

### 3.1 Project

A **Project** is an orchestration boundary that groups related goals, knowledge, policies, resources and history.

A Project is not necessarily a software repository. It may span many repositories and external systems, or none.

A Project provides the scope in which Orchestra can answer:

- what are we trying to accomplish?
- what rules and authority apply?
- what work belongs together?
- what evidence and history should be considered?
- what external systems and resources are relevant?

**Must not be confused with:** repository, workspace, chat, plan or goal.

### 3.2 Goal

A **Goal** is a human-owned statement of intended change or outcome.

A Goal expresses *why* Orchestra is coordinating work. It should be understandable without knowing the implementation path.

Examples:

- make a service ready for safe public launch;
- recover a photo archive and establish reliable backup;
- determine whether a proposed process is technically viable.

A Goal can evolve, but material changes create explicit goal revisions or replacement rather than silently changing historical intent.

**Invariant:** Tasks do not define the Goal. The Goal gives meaning to Tasks.

### 3.3 Outcome Criterion

An **Outcome Criterion** is an observable condition used to determine whether a Goal has actually been achieved.

A criterion should state what evidence would count, not merely what activity should occur.

Examples:

- an unauthenticated user can complete the intended public flow on staging;
- a restore rehearsal successfully recovers a known sample;
- an independent verification confirms the required runtime behaviour.

Outcome Criteria may be mandatory, optional or explicitly waived by an authorised Decision.

**Must not be confused with:** task acceptance criteria. A task may succeed while the goal criterion remains unsatisfied.

## 4. Understanding and planning

### 4.1 Assumption

An **Assumption** is a proposition currently treated as true for planning or execution but not yet established as authoritative fact.

It records:

- the proposition;
- why it is being assumed;
- evidence or source, if any;
- confidence/freshness where relevant;
- what work depends on it;
- how it can be validated.

**Invariant:** Assumptions must remain distinguishable from verified facts.

### 4.2 Unknown

An **Unknown** is a material question whose answer may affect planning, execution, risk or outcome verification.

Unknowns may produce investigation Tasks or Decisions.

### 4.3 Plan

A **Plan** is Orchestra's current reasoned route from a Goal toward its Outcome Criteria.

A Plan is not merely a task list. It includes decomposition, dependencies, assumptions, decision points and intended verification.

### 4.4 Plan Revision

A **Plan Revision** is an immutable historical version of a Plan.

When material new evidence changes the route, Orchestra creates a new revision and records why. Earlier revisions remain available for audit and learning.

**Invariant:** Replanning must not rewrite the history under which earlier work was executed.

## 5. Work

### 5.1 Task

A **Task** is a bounded unit of intended work that advances a Plan.

A Task describes **what must be accomplished**, not who will do it or the particular run that tries to do it.

A Task may:

- require capabilities;
- own a declared work scope;
- depend on predicates;
- have acceptance conditions;
- require one or more attempts;
- produce no artifact at all if its purpose is investigation or verification;
- be blocked, cancelled or superseded without being a failure.

**Critical distinction:**

```text
Task = intended work
Attempt = one execution of that work
```

One failed Attempt does not make the Task failed if another valid route remains.

### 5.2 Work Scope

A **Work Scope** declares the surfaces a Task or Attempt is expected or permitted to affect.

Examples include repository paths, infrastructure components, datasets, environments, documents or external services.

Work Scope supports:

- collision detection;
- safe parallelism;
- review;
- authority enforcement;
- change attribution.

It may include both expected-write scope and explicitly protected/no-touch scope.

### 5.3 Acceptance Condition

A **Task Acceptance Condition** states what must be true for Orchestra to consider the Task's intended work satisfied.

It is narrower than an Outcome Criterion.

For example:

```text
Task acceptance:
  authentication PR integrated and source contracts pass

Goal outcome criterion:
  real users can authenticate and reach the correct portal on staging
```

### 5.4 Dependency Predicate

A **Dependency Predicate** is a specific condition that must hold before a Task or transition may proceed.

Dependencies must describe required truth rather than a vague predecessor status.

Examples:

- artifact X exists;
- verification Y passed within the required scope;
- integration candidate is on authoritative main;
- staging deployment contains version Z;
- Decision D approved the production cutover;
- dataset version V is available.

**Invariant:** `Task A complete` is not sufficient when the real dependency is more specific.

### 5.5 Blocker

A **Blocker** is a currently unsatisfied condition that prevents intended progress.

A Blocker records:

- what cannot proceed;
- the concrete reason;
- supporting evidence;
- whether resolution is technical, external or human;
- whether it has a known expiry/recheck condition.

A Blocker is not automatically a failure.

## 6. Capability and participation

### 6.1 Capability

A **Capability** is a provider-neutral description of something that can be done.

Examples:

- PHP implementation;
- browser interaction;
- code review;
- repository write;
- GPU inference;
- infrastructure deployment;
- human product decision;
- visual inspection.

Capabilities may have attributes such as version, environment, performance, trust or specialisation.

### 6.2 Capability Requirement

A **Capability Requirement** states what a Task or Attempt needs.

It may express multiple required capabilities and constraints without naming a particular provider.

### 6.3 Worker

A **Worker** is an actor that can be assigned responsibility for an Execution Attempt.

A Worker may be:

- a human;
- an AI agent;
- an automated service;
- a managed process.

A Worker has identity independent of the current Attempt.

A Worker may expose capabilities but is not defined solely by them.

### 6.4 Tool

A **Tool** is a capability-bearing instrument used by a Worker or Orchestra but not independently accountable for the Attempt.

Examples may include a browser, Git client, database client or compiler.

The distinction is about orchestration responsibility, not intelligence. If an autonomous service receives an assignment and returns accountable results, it may be modelled as a Worker rather than a Tool.

### 6.5 Resource

A **Resource** is something consumed, reserved, addressed or required by work.

Examples include compute, repository, environment, dataset, credential-backed service, device or budget.

### 6.6 Availability Observation

An **Availability Observation** is evidence about whether a Worker, Tool or Resource can currently participate.

It records:

- observed available / unavailable / unknown;
- source;
- observation time;
- expiry/freshness;
- optional confidence or advisory prediction.

**Invariant:** Estimated future availability is not current authoritative availability.

### 6.7 Access Grant

An **Access Grant** states that an actor is technically able to reach or operate on a Resource within a defined scope.

Access is not Authority.

### 6.8 Authority

**Authority** is permission to make a consequential change or decision.

Examples:

- write to a repository;
- merge to an authoritative branch;
- deploy staging;
- change production DNS;
- spend money;
- approve a product-direction decision.

A Worker can have Capability and Access while lacking Authority.

**Critical distinction:**

```text
Capability = can do
Availability = can do now
Access = can reach
Authority = may do
```

## 7. Execution

### 7.1 Execution Attempt

An **Execution Attempt** is one concrete effort to satisfy a Task.

It binds together:

- Task;
- Worker;
- instruction version;
- relevant Plan Revision;
- starting/base context;
- capability/resource selection;
- work scope;
- start/end observations;
- outputs;
- failures;
- evidence.

Retries create new Attempts. They do not erase or secretly reset the previous Attempt.

### 7.2 Instruction / Handoff

An **Instruction** is the durable assignment given to a Worker for a specific Attempt.

A **Handoff** is the transfer of responsibility or context between orchestration participants.

The instruction should identify enough authoritative context to prevent accidental execution of stale work.

At minimum, future implementation should be able to correlate:

```text
Project
Task
Attempt
Instruction version
Plan revision
relevant base/reference state
Worker
expected result channel
```

Human-readable prose may remain the payload; the identity envelope is machine-readable.

### 7.3 Execution Result

An **Execution Result** is the Worker's report and returned outputs for an Attempt.

It is an observation from the execution boundary, not automatically authoritative proof that the Task succeeded.

**Invariant:** `worker says done` is evidence to reconcile, not the final state transition.

## 8. Artifacts and external reality

### 8.1 Artifact

An **Artifact** is a durable output or changed object that can be identified and reasoned about.

Examples:

- source commit;
- pull request;
- document;
- dataset;
- trained model;
- configuration bundle;
- deployment package;
- infrastructure snapshot;
- generated media.

An Artifact may live entirely in an external system. Orchestra need only retain the identity, meaning and evidence it requires.

A human Decision is **not** an Artifact in this model; it has its own semantics.

### 8.2 Artifact Version

An **Artifact Version** identifies a specific immutable or sufficiently stable version of an Artifact.

Verification should target versions, not ambiguous moving names, whenever possible.

For example, a pull request number may identify an Artifact while a commit SHA identifies the exact Artifact Version evaluated.

### 8.3 External Reference

An **External Reference** links an Orchestra concept to a provider-native object without importing that provider's entire ontology.

Examples:

- GitHub PR URL/ID;
- cloud deployment ID;
- CI run ID;
- dataset object/version ID.

### 8.4 External Observation

An **External Observation** is a time-stamped statement obtained from outside Orchestra.

Examples:

- PR is open and 27 commits behind main;
- CI check passed;
- worker quota is unavailable;
- staging reports build X;
- service endpoint returned response Y.

Observations carry source and freshness. They may later become stale without being historically false.

**Invariant:** Observation is not interpretation. Orchestra may derive a Blocker, Verification or state transition from it, but should preserve the source observation.

## 9. Evidence and verification

### 9.1 Evidence

**Evidence** is durable support for a claim Orchestra may rely upon.

Evidence can originate from:

- artifacts;
- external observations;
- test results;
- logs;
- measurements;
- human attestations;
- decisions;
- reviews.

Evidence records provenance and the claim/context it supports.

### 9.2 Verification

A **Verification** is an evaluation of a claim against evidence within an explicit scope.

It records:

- claim being evaluated;
- target identity/version;
- method;
- environment;
- evidence;
- result;
- exclusions / not-tested areas;
- verifier;
- time.

A result such as PASS without scope is incomplete.

### 9.3 Verification Scope

A **Verification Scope** defines what a Verification does and does not establish.

Examples:

- PHP source-level contracts only;
- disposable WordPress/MariaDB runtime;
- browser UX on staging;
- production smoke check;
- human visual review.

Different scopes may all be required before a higher-level claim is established.

### 9.4 Outcome Verification

An **Outcome Verification** evaluates one or more Goal Outcome Criteria using current evidence.

It is deliberately separate from Task Verification.

**Invariant:** Green tasks do not imply a satisfied Goal.

## 10. Integration

### 10.1 Integration Candidate

An **Integration Candidate** is a set of Artifact Versions proposed to become part of an authoritative target state.

The candidate records its inputs and the target/base against which integration is intended.

### 10.2 Integration

**Integration** is the deliberate activity of reconciling candidate artifacts with an authoritative target.

It may include:

- conflict resolution;
- adaptation;
- combined verification;
- provenance preservation;
- rejection or partial acceptance.

Integration may itself be represented by a Task and Attempts.

### 10.3 Integration Result

An **Integration Result** records what actually became authoritative, what did not, conflict decisions and verification evidence.

A valid standalone Artifact may fail integration without becoming historically invalid.

## 11. Decisions and human control

### 11.1 Decision

A **Decision** is a durable choice between meaningful alternatives.

It records:

- question;
- context;
- options considered where useful;
- decision;
- rationale/evidence;
- decision owner;
- authority scope;
- time;
- consequences and conditions.

Not every implementation choice needs a durable Decision. The model is for choices whose rationale or authority matters later.

### 11.2 Decision Policy

A **Decision Policy** describes who or what may decide a class of question and under what conditions.

Policies may consider:

- consequence;
- reversibility;
- cost;
- risk;
- user intent;
- environment;
- delegated authority.

### 11.3 Human Gate

A **Human Gate** is a durable pending decision that requires a human authority before specified work may proceed.

It is not merely a notification.

A Human Gate links:

- the Decision required;
- the owner;
- relevant evidence;
- blocked predicates/tasks;
- expiry or reconsideration conditions if applicable.

**Invariant:** Silence does not satisfy a Human Gate.

## 12. Failure, recovery and supersession

### 12.1 Failure

A **Failure** records that a specific operation or Attempt did not achieve its intended execution result.

Failure belongs to the thing that failed. It should not automatically propagate semantic failure to Task, Artifact, Plan or Goal.

### 12.2 Recovery Action

A **Recovery Action** is a deliberate response to a Failure, Blocker, stale state or reconciliation finding.

Examples:

- retry with a new Attempt;
- reassign Worker;
- reuse a valid Artifact;
- reconcile an external state;
- create a corrective Task;
- replan;
- escalate to a Human Gate.

Recovery records what historical state is preserved and what new work is created.

### 12.3 Supersession

**Supersession** records that one concept has been replaced or made non-current by another while preserving its historical validity.

Likely supersedable concepts include:

- Plan Revision;
- Task;
- Instruction;
- Artifact / Artifact Version in a particular intended role.

Supersession records:

- predecessor;
- successor;
- reason;
- authority/evidence;
- whether useful outputs were carried forward.

**Invariant:** Superseded is not synonymous with failed, invalid or deleted.

### 12.4 Staleness

**Staleness** is a derived condition indicating that information, instruction, assumption, observation, candidate or base context may no longer be safe to rely on without reconciliation.

Staleness is generally not an intrinsic terminal state. It is a reason to re-observe, reconcile, replan or supersede.

## 13. Coordination and reconciliation

### 13.1 Orchestration Authority

**Orchestration Authority** identifies the lifecycle instance currently permitted to advance authoritative orchestration state for a scope.

The implementation mechanism is deferred, but the domain requires a way to distinguish the current coordinator from stale coordinators.

### 13.2 Reconciliation

**Reconciliation** compares Orchestra's current understanding with authoritative external facts and incorporates the result without erasing history.

Reconciliation is required after:

- worker completion;
- external changes;
- downtime;
- stale observations;
- integration;
- failed communication;
- coordinator recovery.

It may produce new Observations, Blockers, Verifications, Recovery Actions, Supersession or Plan Revisions.

### 13.3 Notification

A **Notification** is a delivery of selected orchestration information to a recipient.

Notifications are projections of state/events for attention. They are not authoritative state themselves.

A notification policy may consider actionability, severity, deduplication, quiet periods and escalation.

## 14. Knowledge and learning

### 14.1 Project Knowledge

**Project Knowledge** is durable context that materially improves planning, decisions or execution.

It may include constraints, conventions, architecture, operating knowledge and verified facts.

Project Knowledge should preserve source/provenance where correctness depends on it.

### 14.2 Explanation

An **Explanation** is a learning-oriented interpretation attached to real project work, such as a Decision, Task, Verification or Artifact.

Explanation is separate from the authoritative fact it explains. Learning Mode can vary explanation depth without changing project truth.

### 14.3 Knowledge Profile

A **Knowledge Profile** is an optional private representation of what a human appears to already understand or has chosen to learn.

Its purpose is to adapt explanations, not to decide the human's goals or authority.

Detailed privacy and consent design is deferred.

## 15. Relationships that must never be collapsed

The following distinctions are architectural invariants for v1:

| Do not collapse | Why |
|---|---|
| Goal vs Plan | the route can change without changing the human outcome |
| Outcome Criterion vs Task Acceptance Condition | successful work does not prove the goal |
| Task vs Execution Attempt | retries and worker changes require preserved history |
| Worker vs Capability | multiple workers can provide the same capability |
| Capability vs Availability | capable does not mean currently usable |
| Access vs Authority | technical reach does not imply permission |
| Instruction vs Task | instructions can become stale or be superseded while intent remains |
| Execution Result vs Verification | a worker's report is not independent proof |
| Artifact vs Artifact Version | verification must know exactly what was evaluated |
| Artifact validity vs Integration suitability | good work can become stale against a moving target |
| Observation vs authoritative interpretation | external facts can be stale or misread |
| Evidence vs Verification | evidence supports a claim; verification evaluates it |
| Task completion vs Goal satisfaction | activity is not outcome |
| Failure vs Blocker | blocked work may not have failed |
| Failed Attempt vs Failed Task | another attempt or route may succeed |
| Superseded vs Failed | replacement does not invalidate historical work |
| Human Gate vs Notification | notifying a human is not receiving authority |
| External system state vs Orchestra meaning | providers own native facts; Orchestra owns orchestration meaning |

## 16. Worked scenario tests

These scenarios are not special cases in the model. They test whether the abstractions represent situations already encountered.

### 16.1 Agent completes work but the branch becomes stale

```text
Goal
  improve public authentication

Task
  implement authentication UX

Attempt A
  worker: Agent
  result: completed

Artifact
  pull request at commit X

Verification
  source contracts: PASS
  scope: source only

External Observation later
  authoritative main advanced substantially
  branch diverged

Derived condition
  integration suitability: stale

Task
  not rewritten as failed

Recovery
  reconcile candidate against current main
  then integrate, adapt, partially reuse or supersede
```

This is the model's treatment of the current Delnavazan PR #15 class of problem.

### 16.2 Worker is temporarily unavailable

```text
Task
  remains valid

Capability Requirement
  coding + repository access

Worker A
  capability: yes
  access: yes
  availability observation: unavailable

Worker B
  capability: yes
  access: yes
  availability observation: available

Scheduler/orchestrator
  may assign a new Attempt to Worker B
  if Work Scope and Authority permit
```

Worker A's unavailability does not mutate the Task into failure.

### 16.3 Source tests pass but runtime was never exercised

```text
Artifact Version
  commit Y

Verification 1
  scope: PHP source contracts
  result: PASS

Verification 2
  scope: WordPress/MariaDB runtime
  result: NOT PERFORMED

Task Acceptance Condition
  may or may not be satisfied depending on task contract

Goal Outcome Criterion
  staging runtime behaves correctly
  NOT YET VERIFIED
```

The model cannot accidentally turn a narrow PASS into universal success.

### 16.4 Production change needs the human

```text
Task
  prepare production cutover

Technical Attempts
  may complete reversible preparation

Dependency Predicate
  production cutover decision approved

Human Gate
  owner: human
  evidence: staging verification + risks + rollback plan

Decision
  approve / reject / defer

Only approved Decision
  satisfies predicate allowing production authority
```

The notification asking for the decision is not itself approval.

### 16.5 First attempt fails; useful work survives

```text
Task
  implement feature

Attempt 1
  execution failure

Artifact A
  useful partial implementation
  preserved

Recovery Action
  create Attempt 2
  explicitly inherit Artifact A
  reset worker/runtime context

Attempt 2
  completes Artifact B

Verification
  evaluates B

History
  Attempt 1 remains failed
  Task may still become satisfied
```

No false reset and no lost work are required.

### 16.6 New work replaces an older candidate

```text
Task A
  older approach

Artifact A
  valid historical work

Task B
  newer approach satisfying the same need

Evidence
  B fully covers A's required outcome

Supersession
  A -> B
  reason recorded

Result
  Task A / Artifact A are not labelled defective merely because B became current
```

## 17. Minimal core versus supporting concepts

Not every concept needs a first-version database table or service.

A likely **minimal semantic core** is:

```text
Project
Goal
Outcome Criterion
Plan Revision
Task
Dependency Predicate
Execution Attempt
Worker
Capability Requirement
Instruction
Artifact Version
Evidence
Verification
Decision / Human Gate
External Observation
Recovery Action
Supersession
Outcome Verification
```

Supporting concepts such as Tool, Resource, Availability Observation, Access Grant, Integration Candidate, Notification, Explanation and Knowledge Profile may be introduced incrementally where the first vertical slice requires them.

This is a semantic prioritisation only. The authoritative state model will determine what must actually be persisted.

## 18. What this model intentionally does not decide

Domain Model v1 does **not** yet choose:

- programming language;
- database technology;
- event sourcing versus another persistence pattern;
- queue or workflow engine;
- API protocol;
- UI framework;
- deployment model;
- GitHub as mandatory infrastructure;
- exact task status vocabulary;
- exact transition graph;
- lease implementation;
- identifier format;
- retry limits;
- scheduling algorithm;
- worker scoring;
- notification provider;
- AI provider.

Those choices should follow from the domain and state requirements rather than define them.

## 19. Next design step

The **Authoritative State Model** should now answer:

1. Which of these concepts have authoritative mutable state?
2. Which facts are append-only history?
3. Which values are derived projections?
4. Which external systems remain authoritative for their native facts?
5. What evidence is required for each meaningful transition?
6. What transitions are legal?
7. How are concurrent/stale coordinators rejected?
8. How does reconciliation change Orchestra's understanding without rewriting history?
9. How are retries, supersession and recovery represented?
10. How is Goal satisfaction derived from Outcome Verification?

The state model should use this vocabulary rather than inventing a second one.
