# ADR-001: Local outbound worker bridge

**Status:** Accepted for v1  
**Date:** 4 October 2026

## Context

Orchestra needs a local dispatcher for existing worker profiles without exposing a Mac listener or making a desktop-control service a required dependency. The existing repository defines worker identity, external state, authority, stale coordinator recovery and artifact-preserving failure semantics, but has no runtime queue or daemon implementation.

The local `dzn-deepseek` wrapper currently adds `--approve-for-me` and changes into a Delnavazan checkout. A bridge must not inherit that approval bypass or silently choose a project root.

## Decision

- Use an outbound HTTPS polling process and GitHub Issues as the v1 durable queue. GitHub remains authoritative for issue and label state; a private local state directory records heartbeat, attempts, PID, exit code and output path.
- Require an allowlisted issue author, explicit approval label, and exactly one named worker label before launch. Remove the approval label as part of claiming to prevent redelivery after a crash.
- Configure one trusted local Git source root and one worker at a time. Fetch its configured base ref and create a separate Git worktree/branch per issue before invoking Codex directly with `exec` and `workspace-write`. Applying the approval label is the human gate for that attempt because the daemon has no interactive approval channel. Do not allow sandbox escalation or turn issue content into shell commands or paths.
- Capture output locally with owner-only permissions. Publish lifecycle status only, not logs or local paths, to GitHub.
- On uncertain worker recovery, preserve the issue and workspace, mark the task interrupted after confirming the process is gone, and require explicit re-approval. Never retry automatically.
- Reuse the host's Git credential helper. Keep credentials and configuration outside the repository.

## Consequences

The bridge has no inbound listener and needs no third-party package. GitHub outage pauses intake while local logs/state remain. Separate issue worktrees prevent workers from mixing edits in the trusted source checkout. Worktrees remain local for review; v1 does not push branches or open PRs. A task is execution input, not proof of success. Human issue-label authority remains essential. v1 does not provide multi-Mac leases, automatic retries, remote cancellation, independent verification or production deployment authority.

## Rejected for v1

- A public or LAN HTTP endpoint: adds network exposure and inbound authentication concerns.
- GitHub as Orchestra's entire state model: it is authoritative for issue facts but not for the broader goal or verification model.
- Reusing the existing DeepSeek wrapper unchanged: it bypasses command approval and selects a project path implicitly.
