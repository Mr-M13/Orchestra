# PixelBox deployment assets

These files stage the Linux ARM64 coordinator. The installer is read-only by
default and every mutating mode requires an explicit approval marker. No mode
starts the tunnel or touches unrelated host services.

- `omcp.example.json` — owner-only coordinator configuration template,
  including the `systemd-user` local worker launcher for Codex on PixelBox.
- `github-dispatch.example.json` — dedicated broker configuration template;
  it and the token must be owned by the separate `orchestra-github` UID.
- `omcp.env.example` — fixed-principal tunnel assertions and service `PATH`.
- `tunnel.env.example` — tunnel runtime key placeholder.
- `mcp-http.env.example` — owner-managed bearer-token template for the optional
  loopback HTTPS MCP gateway.
- `systemd/` — coordinator, tunnel, health, optional loopback HTTP gateway and
  health-timer units.
- `install.sh` — exports an exact 40-character Git SHA into
  `/opt/orchestra/releases/<sha>`, validates it, and atomically updates the
  `/opt/orchestra/current` symlink only during a separately approved
  activation. A stored full-tree digest detects later release drift;
  `--rollback` selects any validated release still on disk.
- `config-access.sh` — sourced by `install.sh` to restore the dedicated
  broker and HTTP-gateway traverse-only ACLs after every `install -d` on
  `/etc/orchestra`. It never grants read/list access or changes the owner, and
  it verifies that `other` remains `---`.
- `verify.sh` — read-only staging checks by default; `--live` makes missing or
  inactive installed services, tunnel health, platform, and exact active SHA a
  failure.
- `verify_mcp_http.sh` — read-only live check for the loopback HTTP gateway. It
  reads the token as the gateway identity without printing it, verifies the
  loopback-only listener, and runs authenticated initialize/tools/delete cycles.

Staging and cutover are separate operations. `--stage --sha <sha>` never changes
systemd or the active symlink. Complete the owner-only configuration and
credential files, then use `--activate --sha <sha>` after cutover approval.
`--apply` retains the original two-pass convenience behavior and exits with
status `3` rather than activating newly created placeholder configuration.

Staging an HTTPS-capable release creates the `orchestra-mcp` group and
`orchestra-mcp-http` service identity before installing gateway configuration.
The gateway token file is owned by that identity, mode `0600`, and the
configuration root grants only group traverse access. Existing token contents
are preserved; activation fails if the token is still a placeholder or shorter
than 32 non-whitespace characters. Staging an HTTPS-capable release therefore
requires Linux account tools (`getent`, `groupadd`, `useradd`) and POSIX ACL
tools (`setfacl`, `getfacl`). Portable staging on another platform is limited to
pre-HTTPS releases. Rolling back to a pre-HTTPS release disables and removes the
gateway unit; the identity, ACL, and token remain dormant, so a security
rollback should include token rotation and owner-approved cleanup.

Portable source and staging tests on another platform do not prove PixelBox or
Linux ARM64 readiness. Only `verify.sh --live --sha <sha>` on PixelBox can
produce that evidence. Use `verify_mcp_http.sh` after an approved HTTPS gateway
activation.

`systemd/orchestra-test@.service.example` is an inactive Phase 1 prototype for
a fixed-profile, SHA-pinned test canary. It is not installed by `install.sh`,
does not contact OMCP, and cannot authorise a merge or deployment. See
[the launcher boundary and canary procedure](../../docs/PIXELBOX-TEST-LAUNCHER.md).

Read [docs/PIXELBOX-MIGRATION.md](../../docs/PIXELBOX-MIGRATION.md) before use.
Never commit a filled environment file, GitHub token or provider key.
Replace invalid UID/SHA placeholders in both templates during local owner
review. The installer creates the loopback gateway's system group and service
identity, but deliberately does not create the GitHub dispatch broker identity,
broker configuration or credentials; those migration steps require separate
future approval.
