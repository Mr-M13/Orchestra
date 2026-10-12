# ADR-003: Local approval notifications and capability-bounded standing authority

Status: accepted for implementation; remote approval and live standing grants remain disabled pending deployment verification.

## Context

OMCP persists approval requests and accepts decisions only through an owner-only Unix socket. ChatGPT can submit and observe tasks through the authenticated tunnel, but the owner previously had to open ChatGPT Work and invoke the local command manually. Draft standing authorization also trusted a caller-selected natural-language consequence class too much.

## Decision

Keep the coordinator authoritative. Add two replaceable edge components:

1. A notification projector reads durable coordinator state and maintains an owner-only delivery ledger. Native notification identifiers are deterministic per approval request, so retries replace rather than duplicate.
2. A local HTTP approval interface obtains complete details and resolves decisions exclusively through the existing owner-only channel. Its bearer bootstrap creates a short-lived signed, HttpOnly, SameSite session; decisions also require CSRF validation and a reason.

No approval method is added to MCP. Notifications contain routing metadata, not instructions or credentials. Expired, cancelled, rejected, approved, and removed requests are closed in the ledger and removed from Notification Center. Coordinator state and audit remain the source of truth.

Standing authorization selects an execution capability, not an assertion about prose. The routine profile denies reads outside the worktree except minimal runtime paths, disables command network, ignores user configuration and repository rules, keeps Git metadata read-only, and grants writes only to owner-configured relative directory roots. Production deployment, merge/push, credential access, security configuration, destructive operations, and unclassified work remain outside this capability and require explicit approval.

The one-worker admission limit remains in the coordinator and is unchanged.

## Remote evolution

The portal refuses remote binding unless explicitly enabled with a non-wildcard address, TLS 1.3, and mandatory client certificates. This prepares the UI authentication boundary for Tailscale-connected devices, but remote approval is not enabled. The Unix socket must never be exposed or forwarded. A future PixelBox deployment needs a separately reviewed authenticated decision protocol, certificate lifecycle and revocation, Tailscale ACL verification, and end-to-end tests before activation.

## Consequences

- Restart recovery does not depend on notification delivery.
- Duplicate helper calls are harmless because the notification identifier is stable.
- The approval UI can be replaced without changing coordinator policy.
- Routine tasks may prepare changes in permitted roots but cannot commit, merge, push, deploy, use command network, or read user credentials.
- Repositories without clean non-sensitive path boundaries cannot receive standing edit authority and must continue using explicit approval.
