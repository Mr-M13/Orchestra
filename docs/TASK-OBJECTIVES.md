# Task responses and objectives

`submit_task` accepts an optional `task_intent`: `analysis`, `code_change`, or
`deployment`. New dashboard submissions declare it explicitly. This is a
deliverable classification, separate from the consequence/authority class.

| Adapter | Analysis/review | Code change | Deployment |
| --- | --- | --- | --- |
| Direct DeepSeek | Response | Rejected | Rejected |
| Codex | Response | Contract + observed worktree artifacts | Rejected |
| OpenCode | Response | Contract + observed worktree artifacts | Rejected |
| GitHub Actions | Response | Rejected until its completion contract is integrated | Rejected |

Incompatible submissions fail before workspace preparation or provider calls.
There is also a launch check for queued records from older coordinators.
Deployment cannot be classified as routine development. No deployment adapter
is certified by this change, and no new human approval gate is introduced.
Sandbox and existing authorization policy remain authoritative.

Legacy clients may omit intent. A bounded text guard recognizes common coding
and deployment requests; other legacy requests are classified as analysis.
This is compatibility handling, not general natural-language verification.
Clients should always send the explicit deliverable. Explicit intent participates
in idempotency; omitted-intent request digests retain their original format.

The existing `status` continues to describe execution completion. Additive API
fields separate `response_status` (`pending`, `delivered`, `missing`) and
`objective_status` (`pending`, `response_delivered`, `artifact_verified`,
`unverified`). Analysis success means a response was delivered. Coding success
requires the structured completion contract, existing security checks where
applicable, and coordinator-observed changes against a prelaunch file snapshot.
Observations are bounded to 10,000 files/directories and 64 MiB, fail closed,
ignore Git metadata, temporary and contract files, and do not follow symlinks.
No worker commit is required. Evidence lists changed paths; artifacts alone do
not prove correctness, test coverage, integration, or deployment.

Worker `coding_ready` requires a successful artifact-verified coding attempt.
Historical exit-zero responses cannot establish this proof. New Task labels
unproven/response-only workers and filters them out of coding choices.

Historical events and persisted task history are not rewritten. A source-controlled
read projection annotates known false success
`d88d24ad-ddff-4f43-8ed3-ae3c0e9fd983` as objective unverified/not executed,
including an annotation source identifier. Other historical objectives without
new evidence are displayed as unverified. This annotation can be reviewed and
reverted as ordinary source, without modifying the historical event log.
