# DeepSeek coding lanes

## Runtime decision

The existing OpenCode adapter is not the selected runtime for PixelBox:

- Its `opencode_commands` path constructs a **Seatbelt** policy and requires exact
  loopback port filtering.
- PixelBox is Linux and its authoritative worker sandbox is Bubblewrap; the
  existing backend deliberately fails closed for exact loopback ports:
  `exact loopback-port filtering is not available with bubblewrap`.
- OpenCode is not installed at `/opt/orchestra/bin/opencode-cli` on PixelBox.
- The previous Mac OpenCode failure was an `OpenCodeExecutionFailed` with an
  opaque HTTP 500 while configuration discovery walked the worktree ancestors.
  That fix cannot be reused as a Linux coding runtime without a new sandbox
  design.

The smallest maintainable replacement is the already-installed, already-proven
**Codex CLI 0.163.0 coding harness** configured with DeepSeek's native
Responses API. This is a new coding runtime configuration, not a rename of the
response-only `deepseek` adapter.

## Data path

```
Codex CLI (sandboxed coding tools)
  -> loopback one-task gateway (random path/token)
  -> DeepSeek Responses API https://api.deepseek.com/responses
```

The real DeepSeek API key is read only by the loopback gateway in the runner
process. It is never placed in the worktree, worker log, task result, dashboard,
or Git. Codex receives a synthetic local bearer key. The Codex tool sandbox has
network disabled; only the runner gateway has provider egress.

The gateway validates method/path/host/authorization/content length/JSON/model
and bounds requests and responses. It records only an integer provider status
for classification. Provider bodies, prompts and credentials are not retained.

## Tool restriction

The DeepSeek Codex configuration:

- disables web search, browser/computer use, apps/plugins, skills, image
  generation, goals, image viewing and multi-agent/subagents;
- keeps only terminal execution, stdin and the Codex completion/tool surface
  needed for coding;
- sets `shell_environment_policy.inherit = "none"` so the synthetic provider key
  and service environment do not reach model-generated commands;
- uses the same routine filesystem/network confinement and trusted negative
  security probe as Ina/Codex. The probe and worker derive from the same policy.

No model text is accepted as proof of an edit. The completion contract, result
file, independent file verification and trusted security assertions remain
authoritative.

## Provider guard

Both DeepSeek coding lanes share one account credential. A latest terminal
DeepSeek `credit_exhausted` or `authentication` failure blocks automatic
scheduling for all DeepSeek workers, even if another lane is idle. The guard
keeps accepted tasks queued and does not retry. After the owner repairs balance
or credentials, set `deepseek_guard_cleared_after` to the repair timestamp and
restart the coordinator.

`deepseek_max_parallel_tasks` defaults to 2. The global
`max_parallel_tasks` remains the hard cap and one active task per worker is
preserved. Rate limits and transient provider failures fail the task without
automatic paid retry; uncertain outcomes are not retried after restart.

## Intended production workers

```json
{
  "deepseek-code-1": {
    "adapter": "codex",
    "command": ["codex"],
    "codex_home": "/home/hamed/.codex-deepseek-1",
    "provider": "deepseek",
    "model": "deepseek-flash",
    "credential_file": "/home/hamed/orchestra-migration/secure/deepseek-api-key"
  },
  "deepseek-code-2": {
    "adapter": "codex",
    "command": ["codex"],
    "codex_home": "/home/hamed/.codex-deepseek-2",
    "provider": "deepseek",
    "model": "deepseek-flash",
    "credential_file": "/home/hamed/orchestra-migration/secure/deepseek-api-key"
  }
}
```

The production default remains the proven Codex worker until both lanes pass
the owner-authorized live acceptance. Ina and GitHub Actions configuration and
history remain intact but are unproven/unavailable.

## Credit-free evidence

- Installed Codex CLI 0.163.0 connected to a local fake Responses provider and
  completed a turn without calling DeepSeek.
- The same isolated runtime executed a tool call that created a file and exited
  0; the fake provider received exactly two requests and no real credential.
- The fake Responses request contained only the restricted tools
  `exec_command`, `write_stdin` and `request_user_input`; multi-agent, web,
  browser and image tools were absent.
- Unit tests cover worker config, tool/provider pinning, gateway payload shape
  and provider failure categories.
- The full Python and dashboard suites pass.

## Rollback

Remove the two worker entries and their standing authorizations, restart the
coordinator, and keep `codex` as the fallback. The previous release and its
config backup remain under `/opt/orchestra/previous` and the pre-activate
backup root. Do not remove Ina/Actions history or task records.
