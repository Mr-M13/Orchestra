# Future Orchestra Architecture — Working Notes

> Status: **parking lot / design memory**, not an implementation commitment.
>
> These notes capture lessons from operating Delnavazan and the Mac Bridge in October 2026 so they can be revisited after launch. They intentionally avoid turning today's experiments into premature architecture.

## 1. Preserve the layers

The emerging model is:

```text
Human goal
   ↓
Orchestra / Coordinator
(reasoning, decomposition, dependencies, decisions, verification)
   ↓
Relay / durable control plane
(PixelBox initially; queues, state, routing, events)
   ↓
Capability Bridges / adapters
(Mac Bridge, future machine/service adapters)
   ↓
Execution lanes
   ↓
Workers / providers / tools
(Codex, OpenAI API, DeepSeek, Gemini, local models, browsers, GitHub, etc.)
```

The important separation is that **decision-making belongs above the relay and bridges**. A relay moves coordination state. A bridge adapts a resource into executable capabilities. Neither should silently become a second orchestrator.

## 2. PixelBox as a replaceable Relay

PixelBox is attractive because it is small, low-power and always on. Its likely role is a lightweight relay/control-plane node rather than a heavy execution machine.

Good PixelBox responsibilities may include:

- durable queues and event journal;
- worker/bridge registration and health;
- task/result routing;
- compact coordination state and cursors;
- wake/event signalling;
- lightweight status API/UI;
- notifications and operational metadata.

PixelBox should **not** be assumed to perform heavyweight inference, large builds, browser fleets or other work better suited to Macs, servers, APIs or specialised compute.

The Relay must be replaceable. PixelBox could later become a mini-PC, NAS, VPS or another always-on host without changing Orchestra's core model.

## 3. The Mac still needs a Bridge

Moving the relay to PixelBox does not remove the Mac Bridge.

The Mac owns capabilities that remote coordination cannot magically provide: local files, Git worktrees, shell processes, Codex installations, browser sessions, credentials and other machine-local resources.

The Mac Bridge therefore becomes a thinner **capability adapter**:

```text
Relay → Mac Bridge → local capability/worker
```

Over time a common Bridge protocol may include:

- registration and identity;
- advertised capabilities;
- task acceptance;
- lifecycle/status;
- bounded results/artifacts;
- cancellation;
- health/heartbeat.

Do not design that protocol prematurely. Learn it from the working Mac Bridge first.

## 4. Bridge lifecycle is not outcome semantics

A key operational lesson:

`bridge-done` means **the Bridge attempt finished**. It does not mean the user's requested outcome succeeded, was verified, was pushed, was merged, or was integrated.

Keep separate concepts:

```text
transport accepted
→ execution started
→ execution finished
→ result available
→ coordinator interpreted outcome
→ outcome verified
→ integrated
→ goal evidence satisfied
```

A worker may exit 0 while producing an unusable or undelivered result. Conversely, a worker may return a useful diagnosis rather than a code change. Bridge should report facts, not decide what those facts mean.

The coordinator owns interpretation, retries, escalation and verification.

## 5. Bounded result awareness

The current Bridge keeps full stdout/stderr local. That is a good privacy and simplicity default, but the coordinator needs enough durable result information to continue intelligently.

Future direction: publish a **bounded, sanitised, idempotent result envelope** while keeping full logs local. Candidate fields:

- task/attempt ID;
- bridge/worker/lane;
- branch/worktree reference where safe;
- exit status and duration;
- concise final worker result;
- artifact/commit/PR references;
- truncation marker;
- timestamp.

This is evidence transport, not decision logic.

## 6. Event-driven wake, not polling-as-awareness

Experiments with hourly schedules and browser/watch loops showed an important distinction:

**polling is not waking, and scheduling is not awareness.**

Preferred future flow:

```text
worker/bridge state changes
→ Relay records durable event
→ tiny wake gateway emits “state changed; reconcile”
→ Coordinator run starts
→ Coordinator reconstructs delta state
→ decisions/tasks are produced
```

The wake signal should contain no orchestration intelligence. It should only trigger reconciliation.

A low-frequency safety heartbeat may exist as insurance, but periodic polling should not be the primary coordination mechanism.

## 7. Checkpoint + delta resume

A fresh coordinator run should not reread the entire project history.

Maintain a compact coordinator checkpoint containing operational resume state such as:

- schema/version;
- generated timestamp;
- current main SHA;
- last processed event/result cursor;
- active tasks/lane assignments;
- relevant open PRs and check state;
- current blockers/gates;
- pending human decisions;
- next coordination actions.

On wake:

```text
load checkpoint
→ read events/results since cursor
→ inspect only affected objects
→ reconcile
→ write checkpoint N+1
```

Missing, stale or corrupt checkpoints should fall back to bounded broader reconciliation.

The coordinator writes this state. Bridge should not acquire dependency or planning intelligence to maintain it.

## 8. Execution lanes, not named agents

Orchestra should model configurable **execution lanes / worker slots**, not hard-code permanent identities such as “Ina” or “DeepSeek”.

A lane is isolated execution capacity. Providers/models are interchangeable resources that can fill lanes when eligible.

Example:

```text
configured maximum: 6 lanes

lane 1 → OpenAI/Codex
lane 2 → OpenAI/Codex
lane 3 → another provider
lane 4 → local model
lane 5 → idle
lane 6 → idle
```

The configured maximum is a ceiling, not a utilisation target. Orchestra should only create concurrency where useful independent work exists.

The same provider/model may back multiple lanes if each lane has isolated process/context/worktree/branch/output and provider concurrency/rate limits permit it.

Provider disappearance should reduce or replace available capacity, not alter the project dependency graph.

Selection direction:

```text
task → required capabilities → eligible providers/resources → lane allocation → isolated execution
```

For the current scale, a small number of concurrent lanes (roughly 4–6) is likely more useful than chasing very high counts. The architecture should not prevent 1, 10 or 100 lanes, but at larger scales Git contention, provider quotas, dependencies and coordinator overhead become the real limits.

## 9. Capability ownership

Delnavazan is a project using Orchestra, not the owner of the worker infrastructure.

Long term, capabilities such as workers, GitHub access, WordPress tooling, browsers and machine bridges should be Orchestra-managed/integrated resources that projects request by capability.

Avoid coupling project architecture to today's provider names or plugins.

## 10. Human authority and autonomy

Orchestra should autonomously handle routine, reversible technical coordination where evidence is adequate.

Human gates remain appropriate for consequential choices such as:

- production/cutover;
- live money/provider activation;
- destructive or irreversible actions;
- credentials/security boundary changes;
- meaningful expenditure;
- product/business decisions with real trade-offs.

One blocked decision should not stop unrelated safe work.

## 11. Lessons to carry forward

1. Keep Bridge dumb.
2. Do not equate process success with goal success.
3. Durable state/results matter more than keeping one chat/process alive.
4. Event → invocation is more valuable than increasingly clever polling.
5. Separate capacity (lanes) from provider identity.
6. Prefer capability discovery/selection over named permanent workers.
7. Isolate concurrent work so global locking does not unnecessarily serialise independent tasks.
8. Preserve evidence linking execution back to the goal.
9. Use mature systems (Git/GitHub/CI/etc.) rather than rebuilding them inside Orchestra.
10. Resist architecture work when a live project has higher-priority launch work.

## 12. Deliberately deferred

These notes do **not** authorise immediate implementation of:

- PixelBox Relay;
- wake gateway;
- multi-machine coordinator leases;
- provider abstraction framework;
- large lane scheduler;
- broad Bridge protocol redesign.

The next Orchestra implementation session should re-read these notes alongside real operational evidence from the current Bridge and decide the smallest justified next step.
