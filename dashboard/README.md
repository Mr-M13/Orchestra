# Orchestra Dashboard V1

This is a small server-rendered dashboard which communicates with OMCP only over
an owner-configured Unix socket. It has no database, no browser credentials and
no direct access to Orchestra state files.

## Development

Run from the repository root:

```sh
PYTHONPATH=. ORCHESTRA_OMCP_SOCKET=/path/to/omcp.sock python3 -m dashboard.app
```

It intentionally binds loopback only. For PixelBox, run it as a dedicated
unprivileged service bound to `127.0.0.1`, then expose it only through the
existing authenticated private Tailscale/reverse-proxy path after owner review.
Do not expose its port publicly, put OMCP credentials in the browser, or alter
OMCP, tunnel, Docker, Nextcloud, Tailscale, firewall, or systemd configuration
as part of this dashboard checkout.

## Live task monitoring

The dashboard groups authoritative coordinator state into three independently
collapsible sections: every Running task, every Queued task (including a distinct
awaiting-approval state), and the latest 20 terminal tasks. The browser refreshes
`GET /api/tasks` every 10 seconds without reloading the page. A manual Refresh
button and a last-updated/stale warning are always available; polling pauses
while the tab is hidden and resumes immediately when it becomes visible.

`/api/tasks` is a server-side call to the same authenticated OMCP socket using
the read-only `task_snapshot` tool. That tool requires the existing `tasks:read`
permission, returns all running and queued tasks without pagination, and bounds
only terminal history. The dashboard never reads OMCP state files directly.

## Deployment status

The current `deploy/pixelbox` assets stage the coordinator, tunnel, health and
approval services only. They do not install this dashboard or create an OMCP
Unix socket for it. Until an owner approves a dedicated loopback dashboard
service and socket, the dashboard remains a manually operated local service and
must not be exposed through a public port.

## High-risk approval decisions

Routine-development work follows OMCP's configured automatic policy. Protected
tasks remain pending. To display working Approve and Reject controls, start the
dashboard only as the trusted local approver with both values below configured
by its owner-controlled service launcher:

```sh
ORCHESTRA_DASHBOARD_APPROVAL_STATE_DIR=/owner-only/omcp-state
ORCHESTRA_DASHBOARD_APPROVER_IDENTITY=local:dashboard:owner
```

The dashboard then calls the existing owner-only local approval socket and
capability token server-side. It never exposes that token to a browser and does
not add an OMCP approval RPC. Do not enable these values unless the dashboard is
behind an independently authenticated private access boundary; it deliberately
does not trust forwarded identity headers. Without both values, approvals are
read-only and remain available through OMCP's existing local approval interface.

## V1 integration gaps

OMCP's published responses do not include the current task title in
`worker_availability`, nor a pre-submission policy-preview endpoint. The
dashboard therefore derives the display-only current title from
`task_snapshot` and tells the user that Orchestra makes the authority decision
after submission.
The smallest future addition would be a read-only `task_submission_preview`
tool accepting the same bounded submit fields and returning only the server's
policy decision and structured execution capabilities.

OMCP exposes no network approval RPC. Dashboard controls, when explicitly
configured as above, use the existing local human approval channel.

## Unified PixelBox home

The existing home page now includes a read-only system overview alongside the
unchanged Orchestra workers, task groups and task controls. The overview also
loads when OMCP is unavailable. `GET /api/system` is independent of OMCP and uses
`no-store`, CSP, MIME-sniffing protection, frame denial and no-referrer headers.
Other methods receive 405. It inherits the dashboard's existing private access
boundary; it does not add authentication or permit public binding.

The browser refreshes the overview every 30 seconds while visible, with a manual
refresh button and explicit unavailable/stale states. The server caches samples
for 30 seconds. Metrics describe the dashboard host: CPU count and 1/5/15-minute
load averages (not instantaneous CPU utilisation), memory, root filesystem disk
usage, uptime and a recognised CPU thermal zone when accessible. Missing OS
interfaces and sensors display unavailable.

`dashboard/system.py` is the only collector. It reads bounded fixed `/proc` and
CPU thermal paths and filesystem usage, and permits exactly four executable/argv
tuples with absolute `/usr/bin` paths:

- `systemctl show` for the eight named services, selecting only unit ID, load
  state and active state. Missing/inaccessible units are unavailable.
- `sudo -n /usr/bin/docker container ls --all --format` with a fixed
  state/image projection. The API publishes
  aggregate counts and counts for images whose basename is `nextcloud`, never
  names, image strings, labels, mounts, logs, inspect data or environment values.
  Nextcloud container state is not an application health check.
- `tailscale status --json`, projecting only recognised backend state and up to
  eight validated local IP addresses. Peer identities and login URLs are dropped.
- A credential-free HTTPS GET request to `https://mcp.delnavazan.com/healthz` with curl
  config disabled, no proxy, no redirects, certificate verification and short
  connection/total timeouts. Only the HTTP status is retained. 2xx is healthy,
  3xx/4xx reachable (including authentication gates), and 5xx unhealthy. This does
  not prove origin, Cloudflare configuration or tunnel health.

Every child runs without a shell, with closed input, discarded stderr, a minimal
fixed environment and no inherited credentials or CLI configuration. Each probe
has a 2.5-second deadline and a 64 KiB output cap enforced while reading; failure,
excess output or denied access becomes unavailable. Executable paths and arguments
cannot be supplied through HTTP. Samples contain selected numbers, enums and
validated addresses rather than raw probe output. Browser rendering uses
`textContent`; existing server rendering continues to HTML-escape task data.

Run with existing host permissions only. The Docker probe uses one exact
non-interactive sudo argv tuple; no other sudo command is permitted. Do not change
sudoers, groups, Docker socket access, service permissions or credentials for this
dashboard; denied access is an expected unavailable result. There are no host/service/container
control endpoints or configuration writes. Deployment and infrastructure assets
are unchanged.

Validation (repository root):

```sh
PYTHONPATH=.:tests TMPDIR=/tmp python3 -m unittest test_dashboard.DashboardTests test_dashboard_system
node --test tests/test_dashboard_polling.js tests/test_dashboard_system.js
```

The broader `test_dashboard.OMCPClientTests` includes real Unix-socket integration
tests and requires an environment that permits local socket binding.
