# Charon

The Ferryman — storage, sync, and persistence for the Yggdrasil ecosystem. Three services ferry data between Gaia's vault and the cloud.

```mermaid
graph TD
    csync[charon-sync] <-->|bisync| vault[Obsidian Vault /data]
    csync <-->|rclone bisync| gdrive[Google Drive]
    gitbackup[git-backup] -->|hourly commit| github[GitHub]
    gitbackup --> vault
    carchive[charon-archive] -->|nightly copy| gdrive
    carchive --> backups["/mnt/storage/backups"]
```

## Services

| Service | Role | Schedule |
|---------|------|----------|
| charon-sync | Bi-directional vault sync with Google Drive via rclone bisync | Every 5min |
| charon-archive | Nightly backup of infrastructure dumps to Google Drive | 3am NZT |
| git-backup | Hourly git commits of vault to private GitHub repo | Hourly |

## Networks

- `aether-net` — external overlay for Google Drive API access
- `internal` — stack-internal overlay

## Failure Notifications & Monitoring

Charon integrates with Discord for instant error alerting:
- **Discord Webhook**: Configured via `DISCORD_WEBHOOK_URL` (GitHub secret or environment).
- **Triggers**:
  - `charon-sync`: Critical bisync errors, cache corruption / forced resync.
  - `charon-archive`: Failures in nightly `rclone copy` (3am) or retention prune `rclone delete` (4am).
  - `git-backup`: Unresolved git rebase divergence or remote push failures.
- **Healthchecks**: Real-time heartbeat validation across all three services to detect stuck sync loops or frozen network operations.
- **Resource Limits**: CPU (0.50) and memory limits (512M) enforced across all containers.

## Quick Deploy

Host directories and permissions are managed centrally by [`yggdrasil-os`](https://github.com/yggdrasil-lab/yggdrasil-os).

```bash
curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/deploy/deploy.sh -o /tmp/deploy.sh && chmod +x /tmp/deploy.sh
/tmp/deploy.sh "charon" docker-compose.yml
```

## Full Documentation

Operational details, environment variables, secrets, backup architecture, pitfalls, and historical context live in the vault at `Areas/90-Infrastructure/Charon/Charon Stack.md`.
