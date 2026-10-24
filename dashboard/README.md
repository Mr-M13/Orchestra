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
dashboard therefore derives the display-only current title from `list_tasks`
and tells the user that Orchestra makes the authority decision after submission.
The smallest future addition would be a read-only `task_submission_preview`
tool accepting the same bounded submit fields and returning only the server's
policy decision and structured execution capabilities.

OMCP exposes no network approval RPC. Dashboard controls, when explicitly
configured as above, use the existing local human approval channel.
