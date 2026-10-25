# Worker capability audit

`available: true`, a configured credential file, and an executable on `PATH` do
not prove that a worker has funding, can edit a repository, or has completed a
task successfully. Use the credit-free audit before scheduling any paid
acceptance test:

```sh
cd /opt/orchestra/current
python3 scripts/audit_workers.py \
  --config /etc/orchestra/omcp.json \
  --dispatch-config /etc/orchestra/github-dispatch/config.json \
  --json
```

The command opens no provider connection, submits no task, and reads no
credential contents. It reports:

- adapter and whether the configured lane is repository-writing or
  response-only;
- local executable presence and a bounded `--version` probe;
- Codex home and credential-file metadata (never the value);
- GitHub dispatch-broker socket presence, owner match and world-access bit;
- `workflow_sha` versus the local `origin/<workflow_ref>` commit;
- observed host memory and a planning allowance after a 1 GiB reserve;
- `funding: unverified` and `credits: unverified` for every worker.

## Current lane assessment

The reviewed PixelBox configuration has two local Codex workers
(`ina-codex` and `hamed-codex`). Both are coding lanes because Codex is a
repository-editing adapter. They are the intended two-writer pair for the
initial `repository_parallelism: isolated_worktrees` rollout.

The direct DeepSeek adapter is response-only. It must not be counted as a
coding writer or used for repository edits.

The GitHub Actions lane is configured but not healthy. Failed dispatch runs
show the verification job succeeding and the `Execute approved task` job
receiving no runner: the job requests `ubuntu-24.04-arm`, remains unassigned,
and completes as a failure with no steps. Examples include run `40156702380`
and the earlier failed runs from 2026-10-25. Do not call the Actions lane
healthy or enable it for production work until an eligible ARM64 runner is
confirmed, the broker `workflow_sha` matches the reviewed protected source
ref, and a credit-free dispatch/adoption test passes. Changing the runner label
or broker SHA requires a separately reviewed change and owner authorization.

The existing GitHub `orchestra-workers` environment has only the protected
branch policy and no required reviewers. Do not reintroduce a human review
gate.

## Funding and paid acceptance

Funding cannot be inferred from configuration or a successful local version
probe. A paid Codex, DeepSeek, or GitHub Actions acceptance task requires the
owner's explicit authorization for that specific test. Until then, use:

- unit and scheduler tests;
- the `/usr/bin/true` startup reproduction in
  `deploy/pixelbox/reproduce-worker-startup.sh`;
- local workspace-preparation and Git-identity tests;
- health and worker-audit checks.

## Memory and CPU limits

The initial PixelBox target is two simultaneous coding workers on a 4 GB host
that also runs Nextcloud and other services. Do not set `MemoryMax` or
`CPUQuota` from a static estimate alone. Measure:

1. Baseline Nextcloud, Docker, Tailscale and coordinator memory/CPU during
   normal use.
2. Two harmless coding workers with the intended models and worktrees,
   recording peak RSS, swap, CPU time and OOM events.
3. Headroom for the kernel page cache and Nextcloud request bursts.

Only after that measurement should a reviewed change add per-worker
`MemoryMax` and `CPUQuota` to the transient user service. A limit that can
trigger an unreported OOM kill is worse than no limit. Until then,
`max_parallel_tasks: 2` and one task per worker are the operating brake.
