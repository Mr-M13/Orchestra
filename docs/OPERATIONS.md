# Operations: current readiness and alerts

## Current readiness

`GET /readyz` and the dashboard now use the coordinator's `readiness`
projection. Readiness is about what Orchestra can do now, not what failed in
the past.

- `ready`: a verified coding worker is operational and has idle capacity.
- `degraded`: Orchestra can accept work, but all verified coding workers are
  busy, capacity is saturated, or the accepted queue is not advancing.
- `unavailable`: the coordinator is unhealthy or no verified coding worker
  exists.

`/readyz` is authenticated and returns HTTP 200 only for `ready`; degraded or
unavailable returns 503 with a bounded reason. `/healthz` remains a public
process-liveness probe and does not claim task readiness.

Worker roles are explicit:

- `ina` and `codex` are coding workers when their last terminal attempt is a
  real success. Historical failures do not advertise them as operational.
- direct `deepseek` is `response_only`; it never satisfies coding readiness.
- `actions-codex` and `actions-deepseek` are `remote_unproven` until a real
  end-to-end Actions task succeeds.
- Idle or configured does not mean operational.

Historical task failures remain in the task database and dashboard Recent
Tasks. They no longer create a permanent `degraded` health state. Current queue
stalls, coordinator/dashboard/MCP/tunnel failures, backup failures and disk
pressure remain current warnings.

## Alert channel

The alert engine supports two independent external mechanisms:

1. **Healthchecks dead-man ping**: `healthcheck_ping_url` is pinged every
   evaluation. An active alert pings `.../fail`; recovery pings the normal URL.
   If PixelBox is down and pings stop, the external Healthchecks service sends
   the owner's configured email/push notification.
2. **Webhook**: `webhook_url` receives bounded JSON transition events for
   firing and recovery. It carries no prompts, credentials or task text.

Configure one or both in `/etc/orchestra/alerts.json` (owner-only, `0600`).
The owner must create the external account/check and choose the notification
channel. Until then `orchestra-alerts.timer` is installed but disabled and
health reports `unchecked`, not healthy.

Recommended channel: a Healthchecks.io check with a 5-minute grace period and
email/push notification enabled. This is the only part that continues to work
when PixelBox itself is down. A webhook alone running on PixelBox cannot alert
on PixelBox loss.

## Deduplication and baseline

- The first successful alert run baselines existing task failures; it does not
  page for the whole historical database.
- New task failure/interruption IDs fire once and remain active until they age
  out of the 24-hour current window or recover.
- Persistent component conditions fire on transition, not every two minutes.
- Recovery events are sent when a current condition clears.
- Alert payloads contain the component, reason, first/last observed times and
  an operator action. No user instructions, provider output or credentials are
  read into an alert.

## Thresholds

Defaults in `alerts.example.json`:

- queue/task threshold: 600 seconds;
- disk free warning: 512 MiB (health uses its own 512 MiB floor);
- alert run recency: 15 minutes; an overdue run is `failed`.

The owner can adjust thresholds, but should not suppress critical component
failures.

## Safe test

Use a local capture endpoint and synthetic health reports in a disposable
environment; never send a provider task. The tests cover:

- first-run baseline with old failures;
- a new failure firing once;
- repeated evaluation without duplicate firing;
- recovery notification;
- no channel => `unchecked`;
- health recency and external-channel configuration.

For the external channel, the owner must supply a Healthchecks check URL (or a
webhook). A firing/recovery test with that URL is a separate authorization
because it sends a real owner notification.

## Operator action

For a stuck task: use read-only task snapshot/status, inspect the worker log and
contract diagnostics, cancel only with a reason, and preserve task records.
For a failed service: check `systemctl` and the component's bounded health
entry. For backup failure: inspect `omcp.backup status`, run a backup, then
verify restore into an isolated target. Do not delete task history or the
previous release.
