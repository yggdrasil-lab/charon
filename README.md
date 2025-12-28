# Charon

> I am Charon, the Ferryman of the Yggdrasil ecosystem. My domain is Storage, Sync, and Persistence. I guard the River of Memory, ensuring that which is recorded is never lost to the void.

## Mission

Establish and maintain the **Unified Storage Layer**. My purpose is to ensure data moves safely between Local (Gaia), Cloud (Google Drive), and Cold Storage (S3).

## Core Philosophy

*   **Safe Passage**: Reliable synchronization between Local and Cloud environments. Data must flow without friction, yet remain consistent across all realms.
*   **Immortality**: Disaster recovery and cold storage. Even if the local world falls, the memory remains preserved in the depths of the vault.

## Tech Stack

*   **rclone**: The engine for Google Drive synchronization, facilitating the passage between local and cloud.
*   **Git**: The scribe of history, providing version control and ensuring every change is accounted for.
*   **AWS S3**: (Planned) The final destination for encrypted, immutable backups.

---

## Architecture

Charon operates through two primary vessels:

1.  **Sync Vessel (rclone)**: Utilizes `bisync` to maintain parity between the local filesystem and Google Drive.
2.  **Persistence Vessel (git-backup)**: An hourly ritual that commits and pushes changes to a remote repository, ensuring versioned history.

## Prerequisites

- **Docker & Docker Compose**: The engines of virtualization.
- **rclone**: Required locally for the initial token exchange.
- **aether-net**: An external Docker network must be present.
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

Edit the `.env` file to provide the necessary secrets and paths for the journey.

### 2. Configure rclone for Safe Passage

The `rclone` service requires authentication with your cloud provider (Google Drive).

**Step 1: Generate an rclone Token**
Run `rclone config` on your local machine.
- Create a new remote named `gdrive`.
- Follow the authentication flow to grant access.

**Step 2: Extract Configuration**
Locate your `rclone.conf` (typically `~/.config/rclone/rclone.conf`) and map the values to your `.env` file:

```env
RCLONE_CONFIG_GDRIVE_TYPE=drive
RCLONE_CONFIG_GDRIVE_SCOPE=drive
RCLONE_CONFIG_GDRIVE_TOKEN={"access_token":"...","refresh_token":"..."}
```

### 3. Configure Git for Persistence

The `git-backup` service requires SSH access to push changes. Ensure your SSH keys are available in the path defined by `HOST_SSH_PATH`.

Required environment variables in `.env`:
- `GIT_REPO_URL`: The destination for the vault's history.
- `GIT_USER_EMAIL` & `GIT_USER_NAME`: The identity of the scribe.

## Execution

To begin the ferryman's work:

```bash
docker-compose up -d
```

## Services

- **rclone**: Periodically synchronizes the local data with Google Drive using `bisync`.
- **git-backup**: Commits and pushes data to the remote Git repository every hour.
- **S3-Backup**: (Future) Encrypted archival to AWS S3.