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

## Foundation phase

We will begin with understanding before implementation:

- document lessons and failure modes from the systems that preceded Orchestra;
- define principles and invariants;
- define the domain model and authoritative state model;
- design failure, recovery, security and testing models;
- design Learning Mode so important technical decisions can be understood, not merely executed;
- record consequential architectural choices as Architecture Decision Records (ADRs).

Application code comes after the foundations justify it.

### Foundation documents

- [Vision](VISION.md)
- [Orchestration lessons and failure-mode audit](docs/foundation/ORCHESTRATION-LESSONS-AUDIT.md) — living evidence from prior and current orchestration experience, translated into design invariants and architectural requirements.
- [Domain Model v1](docs/foundation/DOMAIN-MODEL.md) — the provider-agnostic concepts, relationships and distinctions Orchestra uses to reason about goals, work, workers, evidence, authority, recovery and verified outcomes.
- [Authoritative State Model v1](docs/foundation/AUTHORITATIVE-STATE-MODEL.md) — defines where truth lives, which state is Orchestra-owned versus externally authoritative, what is append-only or derived, and how reconciliation, retries, staleness and coordinator authority work.
- [Failure and Recovery Model v1](docs/foundation/FAILURE-RECOVERY-MODEL.md) — classifies failure modes and defines safe retry, reconciliation, artifact preservation, escalation, crash recovery and unknown-outcome handling.
- [Security and Authority Model v1](docs/foundation/SECURITY-AUTHORITY-MODEL.md) — separates identity, authentication, capability, access and authority; defines scoped delegation, credential boundaries, human approvals, least privilege, audit and compromise containment without hard-coding project policy.

The lessons audit remains open to new evidence throughout development; completing the initial audit does not freeze what Orchestra can learn.

## Human relationship

Orchestra should increase human capability rather than obscure the system behind automation. It should preserve evidence, explain important decisions, make uncertainty visible, and keep genuine human decisions with the human.

---

**Status:** Foundation  
**Repository:** Private  
**Initialised:** 27 September 2026
