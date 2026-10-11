# ADR-002: Local MCP task-control adapter

**Status:** Proposed

**Date:** 11 October 2026

## Context

The secure Orchestra MCP tunnel exposes health status but cannot directly submit or control worker tasks. GitHub Issues and Desktop Commander remain useful fallbacks, but routing every task through an issue adds latency and prevents a connected ChatGPT coordinator from obtaining a direct durable acknowledgement.

The execution adapter must not become a second orchestration engine. It must preserve identity/authority separation, durable intent, isolated workspaces and explicit verification scope.

## Decision

Add `omcp`, a standard-library Python MCP server using local stdio behind the authenticated tunnel.

- The tunnel authenticates the remote session; an owner-only local configuration maps the process to a fixed principal and permission set.
- Submission requires a caller-generated idempotency key and is persisted before execution.
- A single execution slot launches direct Codex CLI commands in fresh Git worktrees with `workspace-write` and no interactive escalation.
- A detached runner writes a completion record so server restarts can reconcile outcomes.
- OMCP provides status, bounded results, availability, cancellation and explicit task approval gates.
- Security-relevant actions produce append-only audit events without task bodies or worker output.
- ChatGPT remains responsible for decomposition, scheduling choices, approval decisions and interpretation of results.
- The Mac Bridge and Desktop Commander remain in place until target-Mac tunnel testing passes.

## Consequences

Direct tasks no longer require GitHub Issue dispatch, while the existing Bridge remains independently usable. Local state is authoritative for OMCP attempts, so its private directory must be backed up and protected. A successful worker exit is not promoted to independent verification. V1 does not coordinate multiple OMCP hosts, automatically retry unknown outcomes, merge branches or make deployment decisions.
