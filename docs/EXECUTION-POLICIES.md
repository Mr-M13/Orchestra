# Orchestra execution policies

This document separates two policies that must not be treated as one grant of
authority:

| Policy | Current status | Effect |
| --- | --- | --- |
| OMCP worker task approval | **Active in the deployed service.** [PR #25](https://github.com/Mr-M13/Orchestra/pull/25) added the owner-controlled switch, and the deployed configuration sets `task_approval_mode` to `disabled`. | An otherwise-valid ordinary OMCP worker task is automatically authorised and queued without a per-task approval prompt. |
| Automatic merge and deployment | **Proposed; not active.** Implementation is being developed in OMCP task `ecae5369-8d70-4ff7-b97c-513bc7c7e763`. It has not been independently verified, merged, deployed or enabled. | No task may currently infer authority to merge or deploy. The eligibility and operating rules below describe the intended policy, not current capability. |

Worker completion, merge, deployment and verified production health are
different claims. Evidence for one does not establish any of the others.

## Deployed worker task policy

`task_approval_mode` is an owner-only service setting. A task request cannot
change it. In the deployed service it is set to:

```json
{
  "task_approval_mode": "disabled"
}
```

In this mode, ordinary OMCP worker tasks do not create an approval request or
wait for a user prompt. Each valid task is recorded with automatic
authorisation and enters the existing single-worker queue. Submission
permissions, fixed-principal authentication, repository and worker allowlists,
fresh worktrees, sandbox restrictions, cancellation, result bounds and audit
logging still apply. Disabled task approvals do not make a task valid when one
of those controls rejects it.

This setting also does not grant Git write, push, merge, deployment, credential
or machine-wide authority to a worker. A successful worker result means only
that the isolated worker process completed successfully.

### Separate host-operation gates

Any isolated host-operation approval gate is a separate control plane. In
particular, the allowlisted host-operation work tracked in
[PR #24](https://github.com/Mr-M13/Orchestra/pull/24) is not the ordinary OMCP
worker queue and is not part of PR #25's deployed task-approval policy.
Disabling worker task approvals neither approves a host operation nor removes
any operation-specific local approval, capability, allowlist or audit check.
PR #24 must not be treated as active unless its own review, merge, deployment
and verification evidence establishes that status.

### Operator runbook

1. Keep the configuration owner-only and set `task_approval_mode` explicitly;
   do not rely on the default, which remains `required`.
2. Restart the service through its operator-controlled launcher after a
   configuration change.
3. Call `bridge_status` and confirm it reports
   `task_approval_mode=disabled` before relying on prompt-free scheduling.
4. Submit a harmless task with a unique request ID. Confirm that it is queued
   without appearing in `list_approvals`, and that its record contains
   `authorization.mode=automatic`, policy `task_approvals_disabled` and
   `approval.status=not_required`.
5. Confirm the audit record identifies the principal, repository profile,
   worker, consequence class and automatic-authorisation outcome without
   storing task instructions or worker output.
6. If any check is absent, contradictory or stale, treat the effective mode as
   unknown, stop consequential work and report the discrepancy. Do not bypass
   the remaining controls.

To restore per-task worker approvals, set the owner-only value to `required`,
restart the service and repeat the status and harmless-task checks. Restoring
this setting affects worker admission only; it is not a rollback mechanism for
code or deployments.

## Proposed automatic merge and deployment policy

> **NOT ACTIVE:** This section is a target contract for task
> `ecae5369-8d70-4ff7-b97c-513bc7c7e763`. It does not authorise a merge or
> deployment. Until a reviewed implementation is cross-linked here, merged,
> deployed, independently verified and explicitly enabled by the owner, the
> system must stop after producing reviewable worker changes.

The intended policy is owner-configured and fail-closed. A caller or task body
must not be able to select, widen or enable it. A candidate is eligible for
automatic delivery only when one exact policy matches and all of the following
are established with fresh evidence bound to the candidate commit:

- the completed change complies with the task instructions, with no unresolved
  ambiguity or unmet requirement;
- all policy-required tests pass against the exact candidate commit;
- all required CI checks pass and all required reviews are satisfied;
- the destination branch is protected, its normal merge rules permit the
  merge, and no protection or review bypass is used;
- the repository, worker, consequence class and changed paths are inside the
  owner-configured eligibility boundary;
- a known-good current release and an available rollback target are recorded;
- the merge result is resolved to the exact resulting `main` commit;
- deployment targets that exact `main` commit, not a stale branch head,
  pre-merge candidate, mutable label or locally modified tree; and
- post-deployment health verification proves that the deployed revision is the
  expected exact `main` commit and that the policy-required checks are healthy.

Required reviews are part of the predeclared repository gate. Once every
eligibility condition above passes, the intended policy performs merge and
deployment without asking the user for an additional final approval. A missing,
pending, stale, contradictory or unverifiable condition makes the candidate
ineligible; it must fail closed and report the specific error.

The policy must never convert worker success, a green subset of tests, an open
pull request, mergeability, a deployment request or a generic health response
into broader end-to-end success.

### Proposed configuration and activation runbook

The exact configuration schema and commands are deliberately not documented
yet because no independently verified implementation is available on `main`.
When implementation becomes reviewable, update this section with a link to its
PR, commit, configuration reference and tests before enabling it. The final
owner-only configuration must, at minimum, bind a stable policy ID to:

- explicit repository profiles, workers and permitted consequence classes;
- permitted and forbidden path scopes;
- the protected destination branch and allowed merge method;
- named required tests, CI checks and review rules;
- independently reviewed verification, deployment, current-release, health and
  rollback adapters; and
- an explicit enable flag that defaults to disabled.

Activation must follow this sequence:

1. Cross-link the implementation PR and exact merged commit in this document.
2. Obtain independent review of authority boundaries, fail-closed behavior,
   durable state, secret handling, command construction and restart recovery.
3. Pass unit, integration and disposable end-to-end tests, including negative
   cases for missing evidence, changed SHAs, failed checks, absent reviews,
   unprotected branches, bypass attempts, stale `main`, unhealthy deployment,
   restart during side effects and rollback failure.
4. Deploy the exact reviewed `main` commit with the delivery policy still
   disabled and verify the running revision.
5. Install owner-only policy configuration, credentials and adapters; verify
   their permissions and preserve the previous known-good release.
6. Explicitly enable one narrow policy and exercise a harmless canary through
   candidate verification, protected merge, exact-`main` deployment, health
   verification and audit inspection.
7. Expand eligibility only through another reviewed configuration change.

Until every step succeeds, automatic delivery remains disabled. Operators must
not substitute an OMCP task ID, a running process, a worker result or an
uncommitted implementation worktree for merged and deployed evidence.

### Audit, failure and rollback

Before each external side effect, persist intent and an idempotency key. Audit
records must allow an operator to reconstruct the policy decision and each
transition without exposing credentials or unrestricted command output. Record
at least:

- task, policy, principal, repository, worker and consequence class;
- candidate SHA, pull request, changed-path decision and instruction-compliance
  result;
- required test, CI, branch-protection and review results with their evidence
  freshness;
- merge intent and outcome, resulting exact `main` SHA and any provider
  operation identifier;
- previous release, deploy intent, target and observed deployed SHA;
- health result, rollback intent and rollback result; and
- stable, reportable reason codes for every refusal or failure.

Unknown merge or deployment outcomes must be reconciled with the authoritative
provider before retrying. They must never be blindly repeated. Any pre-merge or
pre-deployment failure stops delivery and reports the error without performing
later stages.

If post-deployment health verification fails, the intended policy must attempt
rollback to the recorded previous known-good release, verify the rollback and
report both the health failure and rollback result. A failed or unverifiable
rollback is a critical terminal condition: stop automation, preserve evidence
and require operator intervention. Do not report the task as deployed or
healthy unless the exact-revision health gate passes.

## Implementation cross-links

- Worker approval switch: [PR #25](https://github.com/Mr-M13/Orchestra/pull/25)
- Separate allowlisted host-operation work: [PR #24](https://github.com/Mr-M13/Orchestra/pull/24)
- Automatic merge/deploy: OMCP task
  `ecae5369-8d70-4ff7-b97c-513bc7c7e763` — **proposed/not active; replace this
  task-only reference with the reviewed implementation PR and exact merged
  commit once available**
