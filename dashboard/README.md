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

The repository includes `deploy/pixelbox/systemd/orchestra-dashboard.service`,
which binds the dashboard to `127.0.0.1:8082` with read-only host views and no
approval authority. It also includes
`deploy/pixelbox/systemd/orchestra-approval-portal.service`, which binds the
separate owner approval portal to `127.0.0.1:8767`. `install.sh` copies unit
files during an approved release activation, but does not enable or restart
either unit. Enabling them is a separate owner-approved operation. The legacy
PixelBox dashboard on `127.0.0.1:8080` remains in place and is not retired by
this checkout.

Cloudflare Access is only the outer access boundary for the public dashboard.
The dashboard deliberately does not accept approval decisions or trust
forwarded identity headers, so it never turns that boundary into approval
authority.

## High-risk approval decisions

Routine-development work follows OMCP's configured automatic policy. Protected
tasks remain pending. The public dashboard is read-only for approvals: it lists
pending tasks and explains where to review them, but renders no Approve or
Reject form and exposes no `/tasks/<id>/approval` action.

Approval decisions use the existing, separate owner-authenticated local portal
in `omcp/approval_portal.py`. Start it only on loopback with a pre-created
owner-only token and the owner-only OMCP state directory; the repository unit
does this for `127.0.0.1:8767`. The portal:

- binds loopback only and calls the existing owner-only Unix approval socket;
- requires a bootstrap session established from an owner-only token, with a
  short-lived signed session cookie;
- displays complete instructions, risk, requested authority/capabilities,
  repository, worker, consequence, expiry and a required reason;
- enforces CSRF, expiry, self-approval rejection and audit in the existing
  coordinator; and
- never exposes the socket, token, or a web approval RPC to the tunnel.

Access it from an owner-controlled client through an authenticated SSH tunnel:

```sh
ssh -N -L 8767:127.0.0.1:8767 hamed@<pixelbox-tailscale-host>
# then open the bootstrap URL with the token read from the owner-only state file
```

Do not put the bootstrap token in chat, logs, a browser bookmark or the public
dashboard. Do not expose port 8767 through Cloudflare, Tailscale Serve, or a
reverse proxy. The token file must already exist with mode `0600`; the systemd
unit mounts the state directory read-only and fails closed if it is missing or
unsafe.

## V1 integration gaps

OMCP's published responses do not include the current task title in
`worker_availability`, nor a pre-submission policy-preview endpoint. The
dashboard therefore derives the display-only current title from
`task_snapshot` and tells the user that Orchestra makes the authority decision
after submission. `worker_availability` now reports both idle process capacity
(`available`) and scheduling readiness for the next queued task (`runnable`),
with a `reason` such as `worker_busy`, `execution_limit_reached`,
`repository_serialized`, or `no_queued_task`. The dashboard uses `runnable` and
`reason` for the worker status badge so an idle worker blocked by a repository
rule is not advertised as usable execution capacity. The new-task form uses
idle process capacity because a submission may legitimately queue.
The smallest future addition would be a read-only `task_submission_preview`
tool accepting the same bounded submit fields and returning only the server's
policy decision and structured execution capabilities.

OMCP exposes no network approval RPC. The dashboard deliberately exposes no
approval controls; the separate loopback approval portal uses the existing local
human approval channel.

## Unified PixelBox home

The home page includes a read-only overview of PixelBox, the photo pipeline,
Mbox storage, Nextcloud, existing PixelBox and Orchestra services, Tailscale and
public MCP health, alongside the unchanged Orchestra workers, task groups and
task controls. The overview also loads when OMCP is unavailable.

`GET /api/system` is independent of OMCP and uses `no-store`, CSP,
MIME-sniffing protection, frame denial and no-referrer headers. Other methods
receive 405. It inherits the dashboard's private access boundary; it does not
add authentication or permit public binding.

PixelBox data comes only from the exact legacy loopback endpoint
`GET http://127.0.0.1:8080/api/status`, with:

- a fixed host, port and path, no query parameters and no redirect following;
- a 2.5-second total deadline and a 128 KiB response cap enforced while reading;
- JSON/object validation and an explicit allowlisted field projection;
- no Docker, sudo, systemctl, shell or legacy POST control invocation; and
- omission of internal URLs, service names outside the allowlist, unknown timer
  names and original photo filenames.

Public MCP health is a separate fixed `GET https://mcp.delnavazan.com/healthz`
request with certificate verification, no redirect following, a 2.5-second
deadline and a 4 KiB cap. Only the status code is retained. 2xx is healthy,
3xx/4xx reachable (including authentication gates), and 5xx unhealthy; this does
not prove origin, Cloudflare configuration or tunnel health.

The browser refreshes the overview every 30 seconds while visible, with a manual
refresh button and explicit unavailable/stale states. The server caches the
validated sample for 30 seconds. Browser rendering uses `textContent`; server
rendering continues to HTML-escape task data. There are no host, service,
container, storage or configuration control endpoints.

Validation (repository root):

```sh
PYTHONPATH=.:tests TMPDIR=/tmp python3 -m unittest test_dashboard.DashboardTests test_dashboard_system
node --test tests/test_dashboard_polling.js tests/test_dashboard_system.js
```

The broader `test_dashboard.OMCPClientTests` includes real Unix-socket
integration tests and requires an environment that permits local socket
binding.
