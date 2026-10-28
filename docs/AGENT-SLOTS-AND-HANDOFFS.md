# Agent slots and task handoffs

The dashboard has four top-level views: Orchestra, PixelBox, Mbox, and Settings.
The Orchestra view shows up to three enabled agents, their configured provider
and model, verified health, and current task state. Refresh is in the header.

## Enable the three current lanes

The owner configuration can set:

```json
"enabled_workers": ["deepseek-code-1", "deepseek-code-2", "codex"]
```

All names must exist in `workers`; duplicates and an empty set are rejected.
Omitting the field preserves the old all-workers behavior. Disabled workers
remain in historical task records and availability diagnostics, but new task
submissions to them are rejected. Existing accepted tasks for disabled workers
remain queued and visible; the scheduler skips them. An operator must inspect
and cancel those tasks with a reason or re-enable the worker. Do this only
after checking there are no active or accepted tasks for a worker being
disabled.

The three agent cards are assigned in the order above. `coding_ready` means
the worker's latest terminal task produced an independently observed coding
artifact. Idle alone is not proof of coding ability. The direct DeepSeek
adapter remains response-only and is outside these three slots.

## Follow-up and review

A task detail page accepts owner text for a linked follow-up or a review by
another enabled agent. The new task records `follow_up_of` and stays in the
same repository profile. The prior result is bounded and labelled untrusted
context. A review uses `analysis` intent and a repository read-only policy
with no writable paths or files; it still must produce its completion contract.
A coding follow-up requires a currently proven coding worker. These actions
create new tasks; they never change a historical result.

There is no deployment adapter in the current Orchestra release. Task prose,
a changed worktree, and a review response are not deployable artifacts. A
Continuous Deployment handoff must wait for a separate integration that binds
an immutable commit/PR, verifies protected-main CI and review policy, runs the
repository's protected deployment workflow idempotently, and records terminal
production verification. The dashboard deliberately has no send-to-CD action
until that path exists.
