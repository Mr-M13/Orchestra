# ADR-004: PixelBox coordinator with GitHub Actions workers

**Status:** Accepted for implementation; deployment and cutover pending explicit owner approval.

**Date:** 20 October 2026

## Context

The deployed OMCP coordinator is a macOS process. Its worker sandbox, OpenCode
executable path, approval notifications and service lifecycle assume macOS.
PixelBox is a Raspberry Pi 4 running Linux ARM64. Keeping the coordinator on the
Mac would preserve an always-on desktop dependency, while moving every worker to
PixelBox would overcommit a small ARM host and couple execution to one machine.

The owner requires GitHub Actions to provide disposable coding workers while the
coordinator remains a persistent, auditable control point. The existing
Nextcloud Docker stack, Tailscale Serve configuration and Mac service must remain
untouched until verification and explicit approval.

## Decision

1. **Coordinator on PixelBox.** Keep authoritative task, approval, audit,
   idempotency and scheduler state in the existing OMCP process. Add systemd
   units, health reporting and recovery procedures for Linux ARM64.
2. **Platform-neutral sandbox construction.** Introduce `omcp.platform`:
   Seatbelt on macOS and bubblewrap on Linux. File policies are deny-by-default.
   A policy that needs provider egress shares the Linux network namespace because
   bubblewrap cannot filter by destination host; host firewall policy may add
   that control. Routine Codex work denies network entirely.
3. **GitHub Actions is an execution adapter, not a scheduler.** An
   owner-configured `github_actions` worker binds repository, workflow, ref,
   agent and model. A task cannot choose any of them. The detached runner
   dispatches or adopts one deterministic run, persists its ID before polling,
   and records a completion file.
4. **Artifacts, not pushes.** The workflow has read-only repository permission
   and returns `result.txt`, `patch.diff`, `worker.log` and `summary.json`. The
   coordinator downloads the named artifact with size and path validation and
   applies the patch only in its isolated task worktree. No merge, push or
   deployment authority is introduced.
5. **Credentials stay off task paths.** The coordinator's fine-grained GitHub
   token is owner-only and sent only in an HTTPS header. Provider keys live in a
   protected Actions environment. Tunnel keys, approval capabilities and the
   OMCP principal marker are never forwarded to a GitHub worker.
6. **Cancellation is durable.** The coordinator writes a cancellation request,
   signals the local runner, and the runner asks GitHub to cancel the exact run.
   Restart reconciliation cancels an orphaned run when its identity is known and
   marks the task terminal without automatic retry.
7. **Health is sanitized.** A heartbeat file, `omcp.health` CLI and systemd timer
   report liveness, sandbox availability, approvals, scheduler capacity and disk
   headroom without task text or credentials.
8. **No production cutover in this change.** Units, configuration templates,
   installer and verifier are staged in the repository. Applying them requires
   explicit approval. The Mac service remains the active coordinator until the
   documented end-to-end gate passes.

## Consequences

- PixelBox can run the coordinator without macOS-only binaries.
- Codex and DeepSeek can execute on ephemeral GitHub runners without giving the
  coordinator's persistent credentials to those runners.
- GitHub outage, queue delay or artifact expiry is an execution failure, not a
  reason to silently retry or claim success.
- Artifact-based integration means a remote run cannot mutate the repository;
  an operator or a separately reviewed integration policy still decides whether
  to merge.
- Linux direct provider adapters have weaker destination filtering than Seatbelt
  unless a host firewall rule is added. The migration does not pretend otherwise.
- Remote approval, automatic delivery and multi-coordinator election remain out
  of scope.
