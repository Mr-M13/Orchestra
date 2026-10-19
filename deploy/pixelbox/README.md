# PixelBox deployment assets

These files stage the Linux ARM64 coordinator. They do not install, start or
change anything unless `install.sh --apply` is run with explicit cutover
approval.

- `omcp.example.json` — owner-only coordinator configuration template.
- `omcp.env.example` — fixed-principal tunnel assertions and service `PATH`.
- `tunnel.env.example` — tunnel runtime key placeholder.
- `systemd/` — coordinator, tunnel, health service and health timer units.
- `install.sh` — dry-run by default; `--apply` requires
  `ORCHESTRA_CUTOVER_APPROVED=1`.
- `verify.sh` — read-only tests and deployment checks.

Read [docs/PIXELBOX-MIGRATION.md](../../docs/PIXELBOX-MIGRATION.md) before use.
Never commit a filled environment file, GitHub token or provider key.
