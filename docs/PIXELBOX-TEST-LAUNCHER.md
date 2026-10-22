# PixelBox socket-free test launcher (inactive prototype)

This is a fixed, one-test PixelBox canary, not an Orchestra worker or another
orchestration service. It never contacts OMCP and can never authorise a merge,
provider request, or deployment.

## Small trust boundary

The prototype has three explicit steps, all manually invoked in a future
approved canary:

1. A root-only `prepare` command validates `/opt/orchestra/releases/<sha>`:
   the full SHA marker, deployment tree digest, root ownership/non-writability,
   fixed manifest, and the exact hash of its sole executable test. It seals a
   root-owned `0640` job readable only by the dedicated worker group.
2. `orchestra-test@<sha>.service` starts **directly** as `orchestra-test` and
   reads that sealed job. It executes only
   `tests.test_socket_free_canary`; there are no task-supplied arguments,
   network destinations, credentials, or writable state paths.
3. A root-only `receipt` command revalidates the release, manifest and test
   tree; obtains the terminal unit result and invocation ID directly from
   systemd; hashes only that invocation's bounded journal output; and writes a
   root-owned receipt. It refuses an active, stale, failed/non-zero, pre-seal,
   or wrong-boot invocation, or any release identity change.

Test output is untrusted evidence, not a success claim. A receipt binds the
SHA, release digest, manifest digest, executable-test digest, systemd invocation
ID and timing/status, and output artifact hash/size. It is never consumed by
OMCP or deployment code.

The unit has `RestrictAddressFamilies=none`, so even `AF_UNIX` socket creation
is denied. `PrivateNetwork`, `IPAddressDeny`, `SocketBindDeny`, hidden `/run`,
and explicit inaccessible production paths are independent defense in depth.
The fixed canary has no socket requirement; isolation is not weakened to run
the general test suite.

## Future canary installation (not performed here)

1. Stage, but do not activate, a reviewed exact SHA with the existing release
   staging procedure.
2. Independently install the committed launcher module and two wrappers
   (`orchestra-test-launcher`, `orchestra-test-runner`) root-owned outside every
   release. Create root-owned directories: `jobs` (0710, group
   `orchestra-test`) and `receipts` (0700) under `/var/lib/orchestra-test`.
   The worker may read exactly its group-readable sealed job but cannot list,
   alter, or access receipts. Output is captured in the journal by invocation,
   not in a reusable worker-visible file.
3. Install the unit as root-owned 0644. Create `orchestra-test` as a locked,
   no-home, no-group system account. Do not edit OMCP, tunnel, Docker,
   Nextcloud, or Tailscale.
4. Run `systemd-analyze verify` and `systemd-analyze security`. Prepare the
   SHA, run one unit, then generate a receipt only after systemd reports success.
   Independently probe AF_UNIX socket creation under the same transient policy;
   it must fail with `EPERM` or `EACCES`.
5. Roll back by stopping the instance and removing only the new unit, wrappers,
   account, and empty launcher directories. No production symlink changes.

## Unverified here

This environment has no running systemd and is not ARM64. PixelBox must verify
`RestrictAddressFamilies=none`, invocation-scoped journal retrieval, direct
unprivileged start with `PrivateUsers`, timeout/cgroup cleanup, and the actual
AF_UNIX denial. The canary must remain disconnected from production services.
PixelBox currently has its memory cgroup disabled: `MemoryMax=1G` remains in
the unit as a required limit, but a canary must not run until the owner verifies
that the target cgroup hierarchy enforces it.
