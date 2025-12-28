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

## Setup Instructions

### 1. Initialize the Environment

```bash
git clone <your-repository-url> charon
cd charon
cp .env.example .env
```

### 2. Configure rclone

Generate a token locally (`rclone config`) and map the values to your `.env`:
```env
RCLONE_CONFIG_GDRIVE_TYPE=drive
RCLONE_CONFIG_GDRIVE_SCOPE=drive
RCLONE_CONFIG_GDRIVE_TOKEN={"access_token":"...","refresh_token":"..."}
```

### 3. Configure Git

Ensure SSH keys are available in the path defined by `HOST_SSH_PATH`.
Required `.env` variables:
- `GIT_REPO_URL`: Destination repository.
- `GIT_USER_EMAIL` & `GIT_USER_NAME`: Git identity.

## Execution

```bash
docker-compose up -d
```

## Services

- **rclone**: Bi-directional sync with Google Drive.
- **git-backup**: Hourly history commits.
- **S3-Backup**: (Future) Encrypted archival to AWS S3.