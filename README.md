# Atlas Infra

This repository contains the infrastructure configuration for running a suite of services using Docker, including Obsidian for note-taking, Kratos, and data backup solutions.

The primary data for these services is stored in the `vault_data` directory, which appears to be an [Obsidian](https://obsidian.md/) vault.

## Prerequisites

Before you begin, ensure you have the following installed on your system:
- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose](https://docs.docker.com/compose/install/)
- [rclone](https://rclone.org/install/) (for initial configuration)

## Setup Instructions

### 1. Clone the Repository

```bash
git clone <your-repository-url>
cd atlas-infra
```

### 2. Configure Environment Variables

This project uses a `.env` file to manage configuration and secrets.

```bash
cp .env.example .env
```

Now, edit the `.env` file and provide the appropriate values for your setup.

### 3. Configure rclone for Backups

The `rclone` service is configured to synchronize your `vault_data` with a cloud storage provider. It is configured using environment variables loaded from the `.env` file.

**Step 1: Generate an rclone Token**

To get the necessary configuration values, you first need to run `rclone`'s interactive setup on your local machine:

```bash
rclone config
```

Follow the prompts:
- Choose `n` for a "New remote".
- Give it a name (e.g., `gdrive`).
- Choose your cloud storage provider.
- Follow the authentication steps. `rclone` will likely open a browser window to grant access.

**Step 2: Get Configuration Values**

Once finished, `rclone` will have created a `rclone.conf` file (usually at `~/.config/rclone/rclone.conf`). Open this file. You will see your remote's configuration, which looks something like this:

```ini
[gdrive]
type = drive
scope = drive
token = {"access_token":"...","token_type":"Bearer","refresh_token":"...","expiry":"..."}
```

**Step 3: Add rclone Variables to `.env`**

Convert the values from `rclone.conf` into environment variables in your `.env` file. The variable names must follow the format `RCLONE_CONFIG_{REMOTE_NAME}_{KEY}`.

Using the example above, you would add the following to your `.env` file:

```
RCLONE_CONFIG_GDRIVE_TYPE=drive
RCLONE_CONFIG_GDRIVE_SCOPE=drive
RCLONE_CONFIG_GDRIVE_TOKEN={"access_token":"...","refresh_token":"..."}
```
**Important**: The `docker-compose.yml` is configured to use a remote named `gdrive`. Make sure you name your remote `gdrive` during the `rclone config` step, or update the `command` in the `rclone` service definition.

### 4. Configure SSH for Git Backups

The `git-backup` service requires an SSH private key to push changes to your git repository. For portability, the key is loaded from an environment variable.

1.  **Get your SSH private key:**
    Copy the entire content of your SSH private key file (e.g., `~/.ssh/id_rsa`).

2.  **Add the key to your `.env` file:**
    Open your `.env` file and add the `SSH_PRIVATE_KEY` variable. The value should be the full content of your key file, enclosed in double quotes.

    ```
    SSH_PRIVATE_KEY="-----BEGIN OPENSSH PRIVATE KEY-----\nyour-key-content-goes-here\n-----END OPENSSH PRIVATE KEY-----"
    ```
    It is important to preserve the newlines in the key. When pasting into the `.env` file, you might need to replace actual newlines with `\n` if you are putting it all on one line. However, many systems support multi-line variables if you just paste the key directly inside the quotes.



## Running the Services

Once you have completed the setup, you can start all the services using Docker Compose:

```bash
docker-compose up -d
```

## Services

- **obsidian**: A self-hosted Obsidian vault, accessible via `https://atlas.tienzo.net`.
- **kratos**: The Kratos service, accessible via `https://kratos.tienzo.net`.
- **rclone**: Periodically synchronizes the `./vault_data` directory with a cloud storage provider using `rclone bisync`.
- **git-backup**: Commits and pushes any changes in `./vault_data` to a remote git repository every hour.
