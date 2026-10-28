# Orchestra and PixelBox backup and recovery

This document is the recovery contract for the PixelBox deployment. It does not
authorize a production backup destination: the owner must choose an
off-device repository and key custody before the timer is enabled.

## What must be recovered

The current host contains the following recoverable state:

| Data | Location | Current size | Notes |
| --- | --- | ---: | --- |
| Orchestra owner configuration and secrets | `/etc/orchestra` | ~50 KiB | `omcp.json`, environment files, DeepSeek/GitHub tunnel credentials, dispatch token. All files are owner/group-only. |
| Orchestra task database/history | `/home/hamed/orchestra-migration/state/state.json`, `audit.jsonl`, `tasks/`, `security-probes/` | ~4 MiB | Durable task records and retained diagnostics. Worktrees are disposable and excluded. |
| Orchestra release metadata | `/opt/orchestra/releases`, `current`/`previous` links, `.orchestra-release-sha`, digest | ~89 MiB | Exact deployable releases and rollback targets. Source is also in Git. |
| Deployment/unit configuration | `/etc/systemd/system/orchestra-*.service`, `deploy/pixelbox/` in the release | ~100 KiB | Includes service ordering, sandbox policy and installer/verifier. |
| Nextcloud configuration | `/home/hamed/MboxNextcloud/compose.yaml`, `.env`, `html/` | ~820 MiB | `.env` is secret; `html/` includes Nextcloud config and installed apps. |
| Nextcloud database | PostgreSQL in `mboxnextcloud-db-1` | ~24 MiB / ~1.8 MiB custom dump | Dumped with `pg_dump --format=custom` after maintenance mode is enabled. |
| Nextcloud user data | `/home/hamed/NextcloudData-SSD` | ~6.0 GiB | Must be quiesced with Nextcloud maintenance mode during a snapshot. |
| Optional FamilyFiles mount | `/mnt/mbox/FamilyFiles` | ~4 KiB | Included if present; the attached Mbox is not an off-device backup destination. |

PostgreSQL's raw `postgres/` data directory is deliberately not the recovery
source: the custom database dump is the consistent restore artifact. The
Nextcloud database and files are captured in the same maintenance window.

## What exists today

Existing material is useful for incident work but is **not** a complete backup
system:

- `backup/source-before-pixelbox-migration.tar.gz` contains source code, not
  runtime state or Nextcloud data.
- `backup/orchestra-recovery-20261025T072900Z/` contains selected config,
  unit and release metadata, but no task database beyond that copy and no
  Nextcloud data/database.
- `pre-deploy-*`, `pre-credit-fix-*`, `pre-ina-contract-*` and
  `pre-concurrency-*` contain point-in-time config backups. They are not
  scheduled and have no retention or restore proof.
- No backup timer, integrity check or restore test existed. Health reported
  backup status as `unchecked`. The Mbox drive is attached to PixelBox and
  therefore does not protect against PixelBox loss.
- A local-only validation snapshot is evidence that the tool works, not
  host-loss protection.

## Destination, encryption and custody options

The backup format is restic: encrypted, deduplicated, incremental, with
retention and `restic check`/`restore`. The example repository uses the
reviewed `/mnt/mbox/OrchestraBackup/restic` path. The service can write only
that dedicated Mbox subtree, while the rest of `/mnt/mbox` remains read-only.
Because Mbox is attached to PixelBox, this remains a local recovery copy and
must keep `off_device: false`.

Choose one before activation:

1. **Owner MacBook/external drive over Tailscale (recommended if an external
   drive is available).** Use an SFTP restic repository on a dedicated
   encrypted volume. The MacBook's internal disk currently has about 9.4 GiB
   free, which is not enough for retention of a ~7 GiB first snapshot plus
   historical versions; use an external volume. PixelBox needs an SSH key
   authorized for the backup account. Keep the restic password in the macOS
   Keychain/password manager and an offline recovery copy; only a root-only
   password file is present on PixelBox for automation.
2. **Cloud object storage (Backblaze B2 or S3-compatible).** Use a dedicated
   bucket with application credentials restricted to that bucket. Keep the
   restic password separately in the owner's password manager/offline copy;
   put provider credentials in a root-only environment file. This is
   off-device and likely the simplest automated option, but it has a recurring
   storage cost and a third-party dependency.
3. **Another trusted NAS/remote Linux host.** Provide host, account, repository
   path and SSH key. The same password custody rule applies.
4. **Manual rotated external media.** Possible, but no automated recency or
   failure alerting can be claimed between rotations; it is not the preferred
   primary backup.

The restic password must not be stored in Git, task state, dashboard output or
chat. The backup status file contains only snapshot metadata, no credentials,
prompts or task text. Provider credentials belong in the root-only
`environment_file` named by `backup.json`; the password belongs in the
root-only `password_file`. `backup.json` itself contains no credentials and is
owned by `hamed` mode `0600` so the unprivileged health service can read it;
the password and provider environment files remain root-only `0600`.

## Retention and storage expectation

The example policy is 7 daily, 4 weekly and 6 monthly snapshots. The first full
snapshot is about 7.5 GiB before restic compression/deduplication. Subsequent
daily snapshots usually add only changed blocks; plan for roughly 15-35 GiB of
repository use at the initial retention depending on Nextcloud churn. Monitor
free space on both the source and the off-device repository and alert below a
bounded threshold. Do not prune below the configured retention until a restore
test has passed.

## Schedule and checks

- Backup: daily at 03:30 local time with up to 30 minutes of jitter, persistent
  across missed boots. The service uses Nextcloud maintenance mode around the
  PostgreSQL dump and files snapshot.
- Integrity: every backup runs `restic check --read-data-subset=10%` and records
  the result. Health treats an integrity check older than 192 hours as failed.
- Recency: health treats a last successful snapshot older than 30 hours as
  failed. A timer that exists is never evidence of a healthy backup.
- Restore test: run `python3 -m omcp.backup --config /etc/orchestra/backup.json
  restore --snapshot latest --target /var/lib/orchestra-backup/restore-test-<date>`
  into an isolated directory, verify the manifest and representative files, and
  remove the isolated copy after recording evidence. Never restore over
  production as a test.

## Recovery outline

1. Stop intake: cancel/stop the coordinator service and prevent new task
   submissions. Preserve `state.json`, `audit.jsonl`, task directories and
   service logs.
2. Identify the active release and config: read `/opt/orchestra/current`,
   `/opt/orchestra/previous`, `.orchestra-release-sha`, `omcp.json` and the last
   known-good backup status. Do not delete the failed current release or task
   records.
3. Inspect a stuck task with the read-only task snapshot/status tools; do not
   mark it successful or forge a completion contract.
4. Restore Orchestra into an isolated location first. Verify the manifest,
   owner/mode of config and secrets, task JSON and release marker. Only then
   restore the selected config and release.
5. Restore Nextcloud database and files together. Stop app/cron, restore the
   database to a fresh/existing database, restore `html/` and
   `NextcloudData-SSD`, preserve web-data ownership/permissions, start app/db,
   run `occ maintenance:mode --off`, and verify a read-only Nextcloud status.
6. For a software rollback, use `/opt/orchestra/previous` or the exact prior SHA
   with the installer's `--rollback` path. Never delete credentials, task
   records or the previous release.
7. Verify coordinator state, authenticated MCP socket, dashboard, tunnel,
   readiness and health; then re-enable intake.

## Off-device activation gate

Before enabling `orchestra-backup.timer`, the owner must supply the chosen
repository, password custody and any provider SSH/API credentials. Until then
the example config is local-only (`off_device: false`) and health reports
backup `unchecked`, not `healthy`.
