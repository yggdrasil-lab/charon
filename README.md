# Charon

> I am Charon, the Ferryman of the Yggdrasil ecosystem. My domain is Storage, Sync, and Persistence. I guard the River of Memory, ensuring that which is recorded is never lost to the void.

## Mission

I am the silent force that moves data between the realms. My mission is to ensure that every thought, every project, and every journal entry is mirrored across the local world of Gaia and the ethereal clouds.

## Core Philosophy

*   **Safe Passage**: Data must flow without friction. Like the souls across the Styx, information must reach its destination intact and without corruption.
*   **Immortality**: The physical machine is temporary, but the record is eternal. I exist to prevent the Great Void from claiming your history.

---

## Tech Stack

*   **rclone**: The primary engine for Google Drive synchronization.
*   **Git**: Version control for the Obsidian vault and infrastructure code.
*   **AWS S3**: (Planned) Cold storage for encrypted archival.

## Architecture

The system operates using two primary mechanisms:

1.  **Sync Vessel (rclone)**: Utilizes `bisync` to maintain parity between the local filesystem and Google Drive.
2.  **Persistence Vessel (git-backup)**: An hourly ritual that commits and pushes changes to a remote repository, ensuring versioned history.

## Prerequisites

- **Docker & Docker Compose**
- **rclone**: Required locally for initial token exchange.
- **aether-net**: The internal Docker network.
  ```bash
  docker network create aether-net
  ```

## Configuration

### Secret Configuration
This stack uses **Docker Secrets** for sensitive data. You must create these secrets specific to the stack (e.g., using `scripts/ensure_secret.sh`).

**1. Rclone Config:**
Create a `rclone.conf` file with the following structure:
```ini
[gdrive]
type = drive
scope = drive
token = {"access_token":"...","token_type":"Bearer","refresh_token":"...","expiry":"..."}
```

Load it as a secret:
```bash
echo "$(cat rclone.conf)" | ./scripts/ensure_secret.sh charon_rclone_config
```

### 3. Token Refresh & Immutability (Important)
Rclone requires write access to its configuration file to update OAuth tokens (e.g., Google Drive access tokens). However, Docker Secrets are read-only.
**Strategy Used:**
1.  **Secret Injection:** The initial `rclone.conf` (with your first token) is injected as a read-only Docker Secret at `/run/secrets/charon_rclone_config`.
2.  **Ephemerality (Tmpfs):** The container mounts `/tmp` as a `tmpfs` (RAM Disk).
3.  **Runtime Copy:** On startup, `entrypoint.sh` copies the secret to `/tmp/rclone.conf` (Writable RAM).
4.  **Security:** This ensures your unencrypted config **never touches the physical disk** and is instantly destroyed when the container stops.
*Note: If the container restarts, it reverts to the token in the Secret. Since Google Drive tokens are long-lived, this is acceptable.*

**2. SSH Key:**
Load your private SSH key for Git access:
```bash
# Example
echo "$(cat ~/.ssh/id_rsa)" | ./scripts/ensure_secret.sh charon_ssh_key
```

### Environment Variables
Set the following in `.env` (pointing to the secret names generated above):
```env
CHARON_RCLONE_CONFIG_NAME=charon_rclone_config_<hash>
CHARON_SSH_KEY_NAME=charon_ssh_key_<hash>
```

**Git Identity:**
- `GIT_REPO_URL`: Destination repository.
- `GIT_USER_EMAIL` & `GIT_USER_NAME`: Git identity.
- `ENV`: (Optional) Environment identifier to prepend to commit messages (e.g., `prod`, `dev`).

**Backups:**
- `GDRIVE_BACKUP_PATH`: Destination path in Google Drive for infrastructure dumps (e.g., `Second Brain/Backups`).

**Private Registry (Optional):**
- `REGISTRY_PREFIX`: If pushing to a private registry (e.g., `registry.<DOMAIN_NAME>/`), set this variable in `.env` or GitHub Variables. Ensure it includes the trailing slash.

## Deployment

### 1. Initialize Submodules
This repository uses `ops-scripts` for standardized deployment logic.
```bash
git submodule update --init --recursive
```

### 2. Configure Environment
1. Copy the example configuration:
   ```bash
   cp .env.example .env
   ```
2. Populate the `.env` file with your credentials (see **Configuration** below).
3. **Important:** Manually create the cache directories on the host to ensure proper segregation:
   ```bash
   # Example based on default paths
   mkdir -p /opt/charon/cache/rclone/sync
   mkdir -p /opt/charon/cache/rclone/archive
   ```

### 3. Launch
We provide standardized scripts for both production and development environments.

**Production (Manager Node):**
Deploy as a Docker Swarm stack (managed by `ops-scripts`):
```bash
./scripts/deploy.sh "charon" docker-compose.yml
```



### Manual Execution (Advanced)
You can invoke the deployment script directly:
```bash
./scripts/deploy.sh "charon" docker-compose.yml
```

## Services

- **charon-sync**: Bi-directional sync of the Second Brain (Vault) with Google Drive. Runs every 30s on Manager.
- **charon-archive**: Daily backup of Infrastructure dumps (`/mnt/storage/backups`) to Google Drive. Runs on ALL nodes.
- **git-backup**: Hourly history commits of the Vault.
- **S3-Backup**: (Future) Encrypted archival to AWS S3.

## Troubleshooting

### Sync Issues: "Nothing to transfer"
If `rclone` reports "Nothing to transfer" even when files have changed, check your **`GDRIVE_VAULT_PATH`** variable.
- **Incorrect:** `"Second Brain/second-brain"` (Quotes included in value)
- **Correct:** `Second Brain/second-brain` (No quotes)

**Why?** If you include quotes in GitHub Environment Variables or `.env`, `rclone` may interpret them as part of the directory name, causing a path mismatch. Ensure the variable is set correctly at the source.

### Network Issues: "Network not found"
If you see errors like `network charon_default not found` during rapid redeployments:
- This stack uses an explicit **`internal`** overlay network.
- The `ops-scripts/deploy.sh` logic specifically waits for `${STACK_NAME}_internal` to be removed.
- Ensure your `docker-compose.yml` defines the network as `internal` (not `default`) to align with this logic.