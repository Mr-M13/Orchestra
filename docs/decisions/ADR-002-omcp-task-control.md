# ADR-002: Local MCP task-control adapter

**Status:** Accepted (amended 19 October 2026)

**Date:** 11 October 2026

## Context

The secure Orchestra MCP tunnel exposes health status but cannot directly submit or control worker tasks. GitHub Issues and Desktop Commander remain useful fallbacks, but routing every task through an issue adds latency and prevents a connected ChatGPT coordinator from obtaining a direct durable acknowledgement.

The execution adapter must not become a second orchestration engine. It must preserve identity/authority separation, durable intent, isolated workspaces and explicit verification scope.

## Decision

Add `omcp`, a standard-library Python MCP server using local stdio behind the authenticated tunnel.

- The authenticated tunnel binds each authenticated caller to a dedicated server process and passes a verified identity at startup; OMCP refuses to start without that identity matching its owner-only configured principal. A shared or unauthenticated tunnel is unsupported.
- Submission requires a caller-generated idempotency key and is persisted before execution.
- An owner-configured local scheduler launches workers in fresh Git worktrees with no interactive escalation. Capacity supports 1-64 tasks; an initial limit of two is operating policy rather than a code ceiling. One task may use a worker at a time, and server-derived read/write reservations prevent same-repository writer conflicts.
- Repository queues are FIFO and selected round-robin. Claims, lanes and resources are persisted before side effects. Detached worker and host-operation runners write a private startup lease and completion record so server restarts can adopt matching processes, reconcile outcomes and fail closed on expired or mismatched leases.
- Approval policy is server-owned. The default `required` mode makes every non-standing-authorized worker task wait for a separate local decision. An owner may disable that general task gate, but direct DeepSeek tasks and host operations still require explicit local approval. The remote MCP caller cannot approve any task.
- Host-side acceptance is a separate, explicitly permissioned task type. The caller may select only an owner-enabled built-in operation and an exact local commit; it cannot supply commands, arguments, paths, images, environment, credentials or network policy. Host operations never receive standing authorization.
- The initial Delnavazan MariaDB operation runs a separately pinned harness commit, tests an exact target commit, gates Docker to cached images and exact disposable commands, forces internal or no-network container paths, forwards no service credentials, and always attempts bounded teardown. It does not expose Docker or GitHub to workers.
- A remote MCP request cannot change approval mode or grant standing authority.
- Security-relevant actions produce append-only audit events without task bodies or worker output.
- ChatGPT remains responsible for decomposition, scheduling choices, approval decisions and interpretation of results.
- The Mac Bridge and Desktop Commander remain in place until target-Mac tunnel testing passes.

## Consequences

Direct tasks no longer require GitHub Issue dispatch, while the existing Bridge remains independently usable. Local state is authoritative for OMCP attempts, so its private directory must be backed up and protected. A successful worker or host-operation exit is bounded evidence, not independent verification. V1 does not coordinate multiple OMCP hosts, automatically retry unknown outcomes, merge branches or make deployment decisions. Adding a generic host shell is explicitly outside this decision.
