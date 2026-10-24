# Orchestra authority service — shadow-only deployment example

**Status: example only. Nothing here is installed, enabled or connected to live
Orchestra.** Phase 3A deliberately provides no activation script. Installing,
starting or enabling the service is a separate reviewed change.

## Identity model

Create six real Unix identities. The authority UID must be distinct from every
client UID. Each client UID has exactly one role.

| Identity | Suggested UID | Role | Notes |
| --- | ---: | --- | --- |
| `orchestra-authority` | 2101 | service | owns DB, socket, lock; no provider credentials |
| `orchestra-coordinator` | 2102 | coordinator | proposes/requests/cancels; cannot approve/verify |
| `orchestra-approver` | 2103 | human | independent approval decision |
| `orchestra-verifier` | 2104 | verifier | independent verification |
| `orchestra-worker` | 2105 | worker | untrusted report and dispatch receipt |
| `orchestra-operator` | 2106 | operator | unknown-outcome reconciliation and sweep |

`orchestra-authority` is a member of `orchestra-clients` so it can set the
socket's group. Client UIDs are members of `orchestra-clients` so they can
traverse the runtime directory and connect. Group membership alone is not
authorisation: the configured UID allowlist and single-role map are enforced
through `SO_PEERCRED`.

## Filesystem ownership

```text
/opt/orchestra                          root:root       0755
/etc/orchestra                          root:root       0755
/etc/orchestra/authority.json           root:authority 0640
/var/lib/orchestra-authority            authority:authority 0700
/var/lib/orchestra-authority/*.sqlite3  authority:authority 0600
/run/orchestra-authority                authority:clients 0750
/run/orchestra-authority/authority.sock authority:clients 0660
/run/orchestra-authority/authority.lock authority:authority 0600
```

The state directory must be `0700`: coordinator and worker identities must not
be able to read the SQLite database or audit records. The runtime directory is
`0750` so clients may traverse but not create, rename or unlink socket entries.

`tmpfiles.d` example:

```text
d /run/orchestra-authority 0750 orchestra-authority orchestra-clients -
```

The systemd unit is an example under `orchestra-authority.service.example`. It
uses a dedicated UID, `PrivateNetwork`, `RestrictAddressFamilies=AF_UNIX`,
`ProtectSystem=strict`, an empty capability set and no `EnvironmentFile`. It is
not installed by this repository change.

## Configuration

Copy `authority.example.json` to `/etc/orchestra/authority.json`, replace the
example audience and UIDs, set ownership `root:orchestra-authority` and mode
`0640`, then validate:

```bash
sudo -u orchestra-authority \
  PYTHONPATH=/opt/orchestra python3 -m orchestra.authority_service \
  --config /etc/orchestra/authority.json --check
```

The service refuses to start if the config is group/other-writable, is a
symlink, or is owned by an identity other than root or the service UID.

## Ownership verification

Before activation, review:

```bash
stat -c '%U:%G %a %n' \
  /etc/orchestra/authority.json \
  /var/lib/orchestra-authority \
  /var/lib/orchestra-authority/authority.sqlite3 \
  /run/orchestra-authority \
  /run/orchestra-authority/authority.sock
```

Expected:

```text
root:orchestra-authority 640 /etc/orchestra/authority.json
orchestra-authority:orchestra-authority 700 /var/lib/orchestra-authority
orchestra-authority:orchestra-authority 600 /var/lib/orchestra-authority/authority.sqlite3
orchestra-authority:orchestra-clients 750 /run/orchestra-authority
orchestra-authority:orchestra-clients 660 /run/orchestra-authority/authority.sock
```

## Safety properties provided by the service

- kernel-derived peer UID on Linux (`SO_PEERCRED`) or macOS
  (`LOCAL_PEERCRED`); unsupported platforms fail closed;
- one configured role per UID; unknown UIDs rejected before request parsing;
- no caller-supplied principal, role, uid, policy or standing-authorisation
  fields;
- durable request idempotency and response cache scoped to `(peer_uid,
  request_id)`;
- interrupted requests recover as `unknown`, never silently re-executed;
- owner-only DB/audit directory and restrictive config ownership;
- stale-socket detection that refuses symlinks and non-sockets;
- clean SIGTERM shutdown; SIGKILL leaves only a recoverable stale socket.

## What this does not provide

This example does not activate the service, integrate OMCP, provide grant
signing, broker credentials, encrypt SQLite at rest, protect against host root,
or implement multi-host/HA operation. See
[ADR-008](../../docs/decisions/ADR-008-authority-security-boundary.md).
