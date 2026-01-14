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

### 3. Launch
We provide standardized scripts for both production and development environments.

**Production (Manager Node):**
Deploy as a Docker Swarm stack (managed by `ops-scripts`):
```bash
./scripts/deploy.sh "charon" docker-compose.yml
```

**Development:**
Run locally with environment loading:
```bash
./start_dev.sh
```

### Manual Execution (Advanced)
You can invoke the deployment script directly:
```bash
./scripts/deploy.sh "charon" docker-compose.yml
```

## Services

- **rclone**: Bi-directional sync with Google Drive.
- **git-backup**: Hourly history commits.
- **S3-Backup**: (Future) Encrypted archival to AWS S3.