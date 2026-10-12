# Orchestra v1 — Portable Core architecture proposal

**Status:** Proposed for review (not an implementation claim)  
**Date:** 12 October 2026  
**Target primary host:** PixelBox (Linux ARM64)  
**Alternative host:** Linux AMD64 or ARM64 server/VPS

## Decision and scope

Orchestra is **one product**. OMCP becomes its MCP/API ingress module; it is not a competing coordinator. The Mac Bridge is a legacy adapter to retire after verified parity. The Mac is an optional worker, not an authority or runtime dependency. PixelBox hosts the initial coordinator but is replaceable. No production changes or tool retirement are authorised by this document.

Preserve the existing project-agnostic Vision, Domain Model, Authoritative State Model, Failure/Recovery Model, Security/Authority Model, and Testing/Verification Model. GitHub owns source, commits, PRs and CI facts; Orchestra owns task intent, approvals, attempts, dependencies and orchestration decisions. A worker's exit code is not proof that a goal was achieved.

## Runtime boundaries

1. **Coordinator (portable core):** project/goal/task state, dependency evaluation, scheduler, durable attempt lifecycle, approval policy, evidence references, reconciliation, audit, and worker eligibility. No platform-specific process launching.
2. **Ingress adapters:** MCP for ChatGPT and future clients; versioned authenticated API for dashboard/other agents. Ingress authenticates the caller, checks scoped authority, validates task/project/repository selection, and commits intent before dispatch.
3. **Worker adapters:** provider-specific execution (Codex/Ina, future DeepSeek/cloud APIs, GitHub Actions, optional macOS/Windows/Linux hosts). Each advertises capabilities and availability; receives an immutable, bounded execution envelope; returns status, artifact references and evidence. Do not treat model names as identities or credentials as authority.
4. **Project adapters:** GitHub and other external systems. They expose observations and permitted actions without becoming Orchestra's authoritative task database.
5. **Operator surface:** review pending approvals, status, failures, audit and recovery from an authenticated browser/device independent of the Mac.

## Task execution contract

Every accepted task binds: project ID, immutable task ID, idempotency request ID, repository profile and resolved remote/ref where applicable, worker capability requirements, scope, instructions revision, approval policy, and expected artifacts/verification. Reject unknown or ambiguous repository profiles **before** accepting work. A retry with the same request ID but changed parameters fails closed. The MCP submit schema must expose `repository_profile` and tests must verify it arrives intact.

Durable states distinguish requested, approval-required, queued, leased/running, succeeded-as-reported, failed, cancelled, interrupted, unknown-outcome and verified. Attempt identity differs from task identity. Only one active coordinator authority/lease may schedule a task; stale worker responses cannot silently overwrite newer state. Worker timeouts require reconciliation before any side-effectful retry. Bounded parallelism requires explicit concurrency limits and isolated workspaces.

## Security and human authority

Use authenticated encrypted transport, least-privilege service identities, per-project permissions, short-lived/scoped worker credentials, encrypted secrets stored outside Git and container images, and redacted logs. Human approvals bind the precise task, attempt, worker, project/repository, proposed action and expiry. A disconnected operator does not imply approval. Keep local approval semantics during transition; do not bypass the existing gate. Production deployment, credential access and consequential external actions require separately defined policy.

Remote workers should initiate outbound authenticated connections where possible; no public inbound access to the home LAN. Treat all agent output, repository content and task text as untrusted.

## Portable deployment and state

Use a reproducible container build for Linux ARM64 and AMD64, with a small Docker Compose deployment, explicit volumes, health checks and resource limits. The coordinator must not depend on macOS Keychain, Mac filesystem paths, Desktop Commander, the PixelBox dashboard, Nextcloud, or Raspberry Pi GPIO. Keep existing PixelBox services isolated.

Start with SQLite only if its single-writer/backup constraints are measured and acceptable; define storage interfaces and a migration path to PostgreSQL before implementing concurrent writers. Store durable task/attempt/event records and schema versions. Keep large artifacts in external storage with checksummed references. Secrets and encryption keys need an independently recoverable encrypted backup, not plaintext repository files.

Publish a stable endpoint independent of the physical host. Build an operator-tested migration: drain new submissions, reconcile running attempts, take a consistent database snapshot, restore onto a different architecture, restore secrets securely, validate checksums/schema/permissions, redirect the endpoint, then resume. Avoid claiming zero downtime until a failover design and rehearsal prove it.

## Reuse and retirement

**Reuse/refactor:** OMCP idempotent submission, task ledger and status/result tools, repository allowlisting, isolated Git worktree principles, human gate, cancellation and crash recovery tests. Place worker process launch and worktree creation in worker adapters.

**Retire after proof:** GitHub Issue Mac Bridge queue, Mac-centric coordinator assumptions and Desktop Commander as a required Orchestra control path. Do not delete the old services before parity and rollback tests. Keep an optional Mac adapter for macOS-specific tasks.

## Milestones and acceptance gates

1. **Contract and source alignment:** merge/reconcile OMCP PR #16, ensure repository profile appears in the exposed MCP submit schema; regression test for wrong-repository rejection and idempotency. No silent fallback to default for an explicitly scoped request.
2. **Portable core skeleton:** extract coordinator from Mac-specific execution; build and test ARM64/AMD64 images; no Mac-only dependencies; configuration validated at startup.
3. **PixelBox readiness (read-only first):** measure CPU/RAM/disk, existing Nextcloud and dashboard load, container runtime, backup capacity, tunnel and network availability. Do not disturb photo services.
4. **End-to-end worker path:** submit -> exact scoped approval -> cloud worker -> evidence -> independent verification -> final status; test offline agent and cancelled task.
5. **Recovery:** restart coordinator during a task; preserve history, prevent duplicate consequential execution, reconcile uncertain outcomes, and resume only with valid authority.
6. **Portability rehearsal:** backup PixelBox instance, restore to a different Linux host/architecture, verify permissions, task history and worker registration, then complete a new task using the same client-facing address.
7. **Legacy retirement:** compare Bridge/Desktop Commander functions to replacements, document rollback, disable one at a time after tests, and only then uninstall.

## Explicit non-goals for v1

No automatic production deploy, payment operations, unsupervised approval, multi-active coordinator cluster, full high availability, local AI model hosting, or wholesale rewrite of GitHub/CI functionality. No claims that existing OMCP already supports cloud workers or cross-platform remote execution.

## Open decisions before implementation

- Persistent database choice after actual PixelBox capacity and concurrency measurement.
- Stable endpoint and authenticated tunnel/operator identity provider.
- Worker transport and lease/heartbeat protocol.
- Remote approval UX and policy tiers.
- Backup destination, encryption-key recovery and restore rehearsal frequency.
- Which current Bridge/Desktop Commander capabilities genuinely need parity.

## References

- [Vision](../../VISION.md)
- [Domain model](foundation/DOMAIN-MODEL.md)
- [Authoritative state model](foundation/AUTHORITATIVE-STATE-MODEL.md)
- [Failure/recovery model](foundation/FAILURE-RECOVERY-MODEL.md)
- [Security/authority model](foundation/SECURITY-AUTHORITY-MODEL.md)
- [Testing/verification model](foundation/TESTING-VERIFICATION-MODEL.md)
- [OMCP PR #16](https://github.com/Mr-M13/Orchestra/pull/16)
- [Repository profiles issue #15](https://github.com/Mr-M13/Orchestra/issues/15)
