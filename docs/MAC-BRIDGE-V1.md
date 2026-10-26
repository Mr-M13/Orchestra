# Orchestra Mac Bridge v1

The Mac Bridge is a small local worker dispatcher. It initiates HTTPS requests to GitHub and does not open a listening socket. GitHub Issues provide durable task intake and lifecycle records; local JSON records heartbeat and process state; worker stdout/stderr stay in private local files.

## Trust and execution boundaries

- The bridge watches only the configured repository and `bridge-approved` issues.
- Only configured GitHub authors can cause work to launch. Use GitHub's issue-label permission controls so only trusted operators can apply `bridge-approved` and `bridge-worker:*` labels.
- Exactly one of `bridge-worker:codex`, `bridge-worker:dzn-ina`, or `bridge-worker:dzn-deepseek` must be present.
- One worker runs at a time. For each issue the bridge fetches the configured base ref and creates an isolated Git worktree and `bridge/issue-<number>` branch under the private state directory. Applying `bridge-approved` is the human approval for that one task attempt. The worker receives Codex `exec` with `workspace-write` and no interactive approval channel; it cannot request escalation, and command failures return to the worker.
- Issue bodies are user task input. Bridge settings are not derived from issue text. Bodies above 64 KiB stay queued.
- A worker that exits unexpectedly is recorded as interrupted and is never retried automatically. Reapply `bridge-approved` only after checking the workspace and local log.
- No secrets are stored in repository files. GitHub credentials come from the host's configured Git credential helper. State, prompts and logs are written with owner-only permissions under the configured state directory.
- GitHub receives lifecycle labels and a bounded JSON execution-result envelope, never the worker's raw output. The envelope contains the issue/task id, worker, process exit status, branch and worktree, changed-file state, HEAD commit, push/PR status, reported test/check summary lines, exact blocker, and private log/result paths. Logs and result JSON stay owner-only under the state directory.

The bridge is intentionally not a general remote shell. It does not accept command strings, arbitrary paths, or Git commands from issues. The worker itself is a coding agent and can write within the configured project root after an operator applies `bridge-approved`; it cannot escape the `workspace-write` sandbox through an approval request. The queue is therefore a metered execution boundary: apply that label only when authorising the selected worker to act.

## Configure

1. Install Python 3.11 or newer and Codex CLI. Confirm the host's Git credential helper can authenticate to GitHub without prompting (`git credential fill` for `github.com`). Do not put a token in this file.
2. Select a local Git repository whose HTTPS `origin` matches the configured GitHub issue queue. `project_root` is used as the trusted source for fetching; the bridge creates per-issue worktrees under the private state directory so worker changes stay isolated from that checkout. Do not point it at a production checkout.
3. Create a configuration file outside this repository, for example `~/Library/Application Support/Orchestra/bridge.json`, with mode `0600`:

```json
{
  "repository": "OWNER/ORCHESTRA",
  "project_root": "/Users/ACCOUNT/Projects/Orchestra",
  "state_dir": "/Users/ACCOUNT/Library/Application Support/Orchestra/state",
  "base_ref": "origin/main",
  "poll_seconds": 30,
  "approved_authors": ["YOUR-GITHUB-LOGIN"],
  "workers": {
    "codex": {"command": ["codex"], "codex_home": "/Users/ACCOUNT/.codex"},
    "dzn-ina": {"command": ["codex"], "codex_home": "/Users/ACCOUNT/.codex-ina"},
    "dzn-deepseek": {
      "command": ["codex", "-m", "deepseek-flash", "-c", "model_provider=\"deepseek\"", "-c", "model_reasoning_effort=\"high\""],
      "codex_home": "/Users/ACCOUNT/.codex"
    }
  }
}
```

Set each path and worker profile to the intended local setup. Applying `bridge-approved` is the explicit per-attempt authorization; the daemon runs without an interactive approval channel and keeps the OS sandbox at `workspace-write`. The existing `dzn-deepseek` shell launcher includes `--approve-for-me` and targets the Delnavazan checkout; this bridge rejects wrappers and approval-bypass options. Configure its direct CLI equivalent as above only after confirming the selected `CODEX_HOME` and provider settings.

4. Create the `bridge-approved`, `bridge-claimed`, `bridge-done`, `bridge-failed`, and worker labels in the configured repository. Restrict who can apply approval/worker labels.
5. Start one foreground diagnostic cycle:

```sh
cd /path/to/Orchestra
python3 -m mac_bridge.bridge --config "$HOME/Library/Application Support/Orchestra/bridge.json" --once
```

6. For continuous use, install a per-user `launchd` LaunchAgent that runs the same module without `--once`, with the checkout as its working directory and the config path as an argument. Keep `RunAtLoad` enabled and `KeepAlive` true; logs should point inside the private state directory. Review `launchctl print gui/$(id -u)` and the state heartbeat after loading it. Stop the service with `launchctl bootout gui/$(id -u) <agent-label>`.

## Queue and status

Create an issue in the configured repository. An authorized operator adds `bridge-approved` and exactly one `bridge-worker:<name>` label. The daemon claims it, fetches the configured base branch, creates a fresh local branch/worktree, records the worker and PID locally, posts a start comment, and launches it there. The worker is instructed to run relevant checks, commit intended changes on its assigned branch, and report checks in its final log summary; it must not push, merge, or create a PR. When the process exits, Bridge records an execution result and inspects the worktree. It pushes a clean committed result to that exact branch with a non-force push. Uncommitted changes, no changes, worker failure, or push failure produce an explicit blocker/result instead. Completion adds `bridge-done` for exit status 0 or `bridge-failed` otherwise; these labels describe process execution only, not task validity or merge readiness. Stdout/stderr are in `state/issue-<number>.log`; the bounded envelope is in `state/issue-<number>.result.json`. The local `state/state.json` records the attempt lifecycle and result fields. Bridge does not create PRs, verify task correctness, or merge; the coordinator owns those decisions.

If a bridge process restarts while a worker still exists, the configured workspace remains busy until that process exits. If it is confirmed gone, the issue is marked interrupted and must be explicitly re-approved. A re-approved attempt reuses its preserved issue worktree; unrelated tasks get separate worktrees. This avoids duplicate workers after an ambiguous crash or branch collisions between issues.

## Current limits

v1 does not implement coordinator leases across multiple Macs, GitHub App installation, stale-process termination, automatic retry, worker cancellation from GitHub, or independent artifact verification. Run only one bridge against a given state directory and project root. GitHub issue status is durable coordination, while local logs/workspace are the execution evidence. Do not interpret a successful process exit as verification or integration.
