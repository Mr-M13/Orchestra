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
- `systemd/` — coordinator, tunnel, health service and health timer units.
- `install.sh` — exports an exact 40-character Git SHA into
  `/opt/orchestra/releases/<sha>`, validates it, and atomically updates the
  `/opt/orchestra/current` symlink only during a separately approved
  activation. A stored full-tree digest detects later release drift;
  `--rollback` selects any validated release still on disk.
- `verify.sh` — read-only staging checks by default; `--live` makes missing or
  inactive installed services, tunnel health, platform, and exact active SHA a
  failure.

Staging and cutover are separate operations. `--stage --sha <sha>` never changes
systemd or the active symlink. Complete the owner-only configuration and
credential files, then use `--activate --sha <sha>` after cutover approval.
`--apply` retains the original two-pass convenience behavior and exits with
status `3` rather than activating newly created placeholder configuration.

Portable source and staging tests on another platform do not prove PixelBox or
Linux ARM64 readiness. Only `verify.sh --live --sha <sha>` on PixelBox can
produce that evidence.

`systemd/orchestra-test@.service.example` is an inactive Phase 1 prototype for
a fixed-profile, SHA-pinned test canary. It is not installed by `install.sh`,
does not contact OMCP, and cannot authorise a merge or deployment. See
[the launcher boundary and canary procedure](../../docs/PIXELBOX-TEST-LAUNCHER.md).

Read [docs/PIXELBOX-MIGRATION.md](../../docs/PIXELBOX-MIGRATION.md) before use.
Never commit a filled environment file, GitHub token or provider key.
Replace invalid UID/SHA placeholders in both templates during local owner
review. The installer deliberately does not create the service account, group,
broker configuration or credential; those migration steps require separate
future approval.
