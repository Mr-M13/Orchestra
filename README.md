# Orchestra

Orchestra is a durable, project-agnostic orchestration system for turning goals into verified outcomes by coordinating people, AI agents, tools, services and compute.

## Why Orchestra exists

Orchestra is being developed to make complex, long-running work understandable, recoverable and increasingly autonomous without giving up human control or institutional memory.

Its core concern is orchestration:

**Goal -> plan -> tasks -> required capabilities -> workers -> artifacts -> verification -> integration -> evidence**

## Boundary

Orchestra is not Delnavazan.

Delnavazan may become one project managed by Orchestra, but Orchestra must not contain assumptions about Delnavazan, WordPress, music education, or any other individual project.

The same Orchestra should be capable of coordinating unrelated software, infrastructure, data, research and machine-learning projects.

## Foundations and first implementation

The initial foundation set is recorded before runtime implementation:

- document lessons and failure modes from the systems that preceded Orchestra;
- define principles and invariants;
- define the domain model and authoritative state model;
- design failure, recovery, security and testing models;
- design Learning Mode so important technical decisions can be understood, not merely executed;
- record consequential architectural choices as Architecture Decision Records (ADRs).

The first runtime component is the opt-in Mac Bridge v1. Its implementation and operating boundary are recorded in [ADR-001](docs/decisions/ADR-001-mac-bridge-v1.md). The proposed [OMCP task-control adapter](docs/OMCP.md) adds a direct, authenticated MCP path for task submission and control while keeping ChatGPT responsible for orchestration. Both are narrow instruments, not the complete Orchestra coordinator or authoritative workflow engine.

### Foundation documents

- [Vision](VISION.md)
- [Orchestration lessons and failure-mode audit](docs/foundation/ORCHESTRATION-LESSONS-AUDIT.md) — living evidence from prior and current orchestration experience, translated into design invariants and architectural requirements.
- [Domain Model v1](docs/foundation/DOMAIN-MODEL.md) — the provider-agnostic concepts, relationships and distinctions Orchestra uses to reason about goals, work, workers, evidence, authority, recovery and verified outcomes.
- [Authoritative State Model v1](docs/foundation/AUTHORITATIVE-STATE-MODEL.md) — defines where truth lives, which state is Orchestra-owned versus externally authoritative, what is append-only or derived, and how reconciliation, retries, staleness and coordinator authority work.
- [Failure and Recovery Model v1](docs/foundation/FAILURE-RECOVERY-MODEL.md) — classifies failure modes and defines safe retry, reconciliation, artifact preservation, escalation, crash recovery and unknown-outcome handling.
- [Security and Authority Model v1](docs/foundation/SECURITY-AUTHORITY-MODEL.md) — separates identity, authentication, capability, access and authority; defines scoped delegation, credential boundaries, human approvals, least privilege, audit and compromise containment without hard-coding project policy.
- [Testing and Verification Model v1](docs/foundation/TESTING-VERIFICATION-MODEL.md) — defines claims, evidence, verification scope/results, proof levels, freshness, independence, invalidation and outcome verification without turning narrow green checks into broad certainty.
- [Learning Mode v1](docs/foundation/LEARNING-MODE.md) — lets understanding grow through real project work using adaptive, evidence-grounded explanations while keeping teaching separate from authority, verification and orchestration.
- [Mac Bridge v1](docs/MAC-BRIDGE-V1.md) — a local outbound-only GitHub issue dispatcher for scoped worker execution.
- [ADR-001: Local outbound worker bridge](docs/decisions/ADR-001-mac-bridge-v1.md) — records the queue, trust boundary, credential, and recovery decisions for the first runtime component.
- [OMCP task control v1](docs/OMCP.md) — direct MCP tools for durable submission, status, results, availability, cancellation and approvals.
- [PixelBox migration](docs/PIXELBOX-MIGRATION.md) — Linux ARM64 coordinator, GitHub Actions workers, systemd, health, cutover and recovery runbook.
- [Execution policies and operator runbook](docs/EXECUTION-POLICIES.md) — distinguishes deployed prompt-free worker scheduling from the proposed, not-yet-active automatic merge/deploy policy.
- [ADR-002: Local MCP task-control adapter](docs/decisions/ADR-002-omcp-task-control.md) — records the direct-control transport, authority and recovery boundary.
- [ADR-004: PixelBox coordinator and GitHub Actions workers](docs/decisions/ADR-004-pixelbox-linux-actions-workers.md) — records the Linux sandbox, remote-worker, artifact and cutover decisions.

The lessons audit remains open to new evidence throughout development; completing the initial audit does not freeze what Orchestra can learn.

## Human relationship

Orchestra should increase human capability rather than obscure the system behind automation. It should preserve evidence, explain important decisions, make uncertainty visible, and keep genuine human decisions with the human.

---

**Status:** Foundation  
**Repository:** Private  
**Initialised:** 27 September 2026  
**PixelBox migration:** implementation and Linux ARM64 staging verification complete; production cutover pending explicit owner approval. See [docs/PIXELBOX-MIGRATION.md](docs/PIXELBOX-MIGRATION.md).
