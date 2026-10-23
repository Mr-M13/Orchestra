# Future work: DeepSeek isolated coding adapter

**Status: proposal / not implemented. Do not grant standing authorisation to the
current `deepseek` worker.**

The deployed `deepseek` worker is a direct, response-only HTTPS adapter. It has
no coding tools, cannot edit a worktree, and always requires a fresh local
approval. OMCP configuration deliberately rejects standing authorisations that
name a non-Codex worker:

```text
ValueError: standing authorization may only name Codex workers
```

A future `DeepSeek isolated coding adapter` must satisfy the same contract as
the Ina/Codex routine path before standing routine-development authority can be
considered:

- run only inside a fresh, profile-specific isolated worktree;
- use a bounded, provider-specific sandbox with no host or credential access;
- deny `/etc/orchestra`, secrets, host configuration, deployment paths and
  outbound network outside the approved provider endpoint;
- keep Git metadata read-only and produce commit-ready changes only;
- implement the structured completion contract, including explicit failure for
  denied required operations;
- execute and report the required negative security assertions;
- preserve approval gates for production, deployment, merging, destructive
  operations and security-policy changes;
- pass cancellation, cleanup, timeout, result-bound and orphan-process tests;
- receive a separate security and policy review before any standing grant is
  added.

This document is a scoping record, not an authorisation. The current PR does not
change the `deepseek` adapter, its approval requirements or its eligibility.
