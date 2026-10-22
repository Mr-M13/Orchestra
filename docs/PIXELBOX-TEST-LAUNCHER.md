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

The service starts a root-owned, fixed supervisor solely to validate the
release, drop its child to `orchestra-test` (with no supplementary groups),
wait for that child, and write the receipt. The test child has no credentials.
`RestrictAddressFamilies=none` denies **all** socket syscalls, including
`AF_UNIX`; `PrivateNetwork=yes`, `IPAddressDeny=any`, and
`SocketBindDeny=any` are defense in depth rather than a substitute for that
denial. Thus there is no essential Python or systemd need for `AF_UNIX` in this
profile. Explicit inaccessible paths additionally cover Orchestra production
configuration, Hamed's home and SSH material, Docker, Tailscale, and common
Nextcloud locations. The unit also uses a private user namespace, strict
read-only system view, private `/tmp`, no capabilities, and explicit
CPU/memory/process limits.

Test code is untrusted relative to this boundary. The root supervisor captures
at most 32 KiB of its combined output and, only after the child exits, writes a
root-owned receipt outside every child-writable directory. The receipt binds
the exact SHA, staged-release digest, fixed profile, exit status, duration and
the bounded output artifact's byte count and SHA-256. It contains no child
supplied success flag or output text. This is still test evidence, not an
approval: a human/reviewer or separate authority must decide what it means.

## Minimal future canary procedure (not performed by this change)

1. On PixelBox, stage but do not activate a reviewed release with the existing
   `deploy/pixelbox/install.sh --stage --sha <full-sha>` procedure.
2. Independently inspect the exact committed unit and launcher. Create the
   dedicated `orchestra-test` system account with no login, home, groups or
   credentials. Install the launcher as root-owned `0755` and its Python module
   as root-owned, non-writable files under `/usr/local/lib/orchestra-test-launcher`,
   outside every release; install the unit as root-owned `0644`; create only
   the root-owned receipt directory. Do not grant `orchestra-test` access to
   that directory, and do not edit OMCP, tunnel, Nextcloud, Docker or
   Tailscale.
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

The portable tests verify that the no-network policy names no permitted address
family and that receipt writes resist replacement. They cannot demonstrate the
kernel's `RestrictAddressFamilies=none` enforcement because this environment
is not booted with systemd. PixelBox's systemd version must be checked for
support for `PrivateUsers`, `RestrictAddressFamilies=none`, `SocketBindDeny`,
`IPAddressDeny`, and `ProtectProc`; the canary must attempt AF_UNIX connects to
an intentionally supplied probe socket and confirm `EPERM`/`EACCES`. ARM64
execution, actual namespace enforcement, UID mapping for the root supervisor,
and cgroup resource limits remain live-validation items. The prototype has no
networked profile; adding one needs a separate security review.
