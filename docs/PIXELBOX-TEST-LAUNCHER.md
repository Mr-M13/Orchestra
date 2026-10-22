# PixelBox fixed-profile test launcher (Phase 1 prototype)

This is an inactive, fixed-profile mechanism for testing a **previously
staged** Orchestra release on PixelBox. It is neither an OMCP worker nor an
authority service. It accepts one full SHA, runs only Python's repository unit
tests, and emits bounded test evidence. That evidence must never approve a
merge, deployment, provider request, or production action.

## Boundary

`orchestra-test@<sha>.service` reads only
`/opt/orchestra/releases/<sha>`, never the mutable `current` symlink. The
root-owned launcher checks the full lower-case SHA, a matching release marker,
a valid release digest, and a direct `tests` directory before starting. The
unit's command line has no task-supplied executable, arguments, environment or
working directory.

The unit runs as `orchestra-test`, with no supplementary groups or credentials.
It has a private network namespace, `IPAddressDeny=any`, `SocketBindDeny=any`,
and only `AF_UNIX`; those controls together prevent host-network egress rather
than relying on `RestrictAddressFamilies` alone. Explicit inaccessible paths
cover Orchestra production configuration, Hamed's home and SSH material,
Docker, Tailscale, and common Nextcloud locations. It also uses a private user
namespace, strict read-only system view, private `/tmp`, no capabilities, and
explicit CPU/memory/process limits.

Test code is untrusted relative to this boundary. It can make false assertions
or alter its own bounded output; therefore JSON output is only SHA-bound test
evidence. A human/reviewer or a separate authority must decide what it means.

## Minimal future canary procedure (not performed by this change)

1. On PixelBox, stage but do not activate a reviewed release with the existing
   `deploy/pixelbox/install.sh --stage --sha <full-sha>` procedure.
2. Independently inspect the exact committed unit and launcher. Create the
   dedicated `orchestra-test` system account with no login, home, groups or
   credentials. Install the launcher as root-owned `0755` and its Python module
   as root-owned, non-writable files under `/usr/local/lib/orchestra-test-launcher`,
   outside every release; install the unit as root-owned `0644`; create only
   the root-owned parent for the systemd-managed result directory (the test UID
   may write its non-authoritative evidence there). Do not edit OMCP, tunnel,
   Nextcloud, Docker or Tailscale.
3. Run `systemd-analyze security orchestra-test@<sha>.service` and manually
   verify all paths and network controls on the target systemd version.
4. Start a single known-safe canary with
   `systemctl start orchestra-test@<sha>.service`, then inspect its unit state,
   journal and bounded JSON. Confirm no result is consumed by OMCP or a deploy
   path.
5. Roll back by stopping the one instance and removing the *new* unit,
   launcher, account and empty test-result directory. No active Orchestra
   symlink or production service changes are involved.

This branch intentionally does not automate any of those privileged steps.

## Limits requiring live validation

The portable tests validate command and policy construction only. PixelBox's
systemd version must be checked for support for `PrivateUsers`,
`SocketBindDeny`, `IPAddressDeny`, and `ProtectProc`. ARM64 execution, actual
namespace enforcement, and cgroup resource limits still need the harmless
canary. The prototype contains no trusted result-attestation mechanism and no
networked profile; adding either needs a separate security review.
