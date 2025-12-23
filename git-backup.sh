#!/bin/sh
set -e

echo "Initializing git backup script..."

# Validate that necessary environment variables are set
if [ -z "$GIT_USER_EMAIL" ] || [ -z "$GIT_USER_NAME" ] || [ -z "$GIT_REPO_URL" ]; then
  echo "Error: One or more required environment variables are not set."
  echo "Please set GIT_USER_EMAIL, GIT_USER_NAME, and GIT_REPO_URL."
  exit 1
fi

# Configure Git
git config --global user.email "${GIT_USER_EMAIL}"
git config --global user.name "${GIT_USER_NAME}"

# Ensure SSH directory exists and permissions are correct
mkdir -p /root/.ssh
chmod 700 /root/.ssh

# Add github.com to known_hosts
if ! ssh-keygen -F github.com &> /dev/null; then
  echo "Adding github.com to known_hosts..."
  ssh-keyscan github.com >> /root/.ssh/known_hosts 2>/dev/null
fi
chmod 600 /root/.ssh/known_hosts


# Initialize git repository if it doesn't exist
if [ ! -d ".git" ]; then
  echo "No .git directory found. Initializing a new repository."
  git init
  git branch -m main
  git remote add origin "${GIT_REPO_URL}"
fi

# Always ensure the remote URL is up-to-date
git remote set-url origin "${GIT_REPO_URL}"

# Main backup loop
while true; do
  echo "[$(date)] --- Starting hourly backup check ---"
  
  # Add all changes to staging
  git add .
  
  # Check if there are any changes to commit
  if git diff --staged --quiet; then
    echo "No changes detected in the vault. Skipping commit."
  else
    echo "Changes detected. Committing and pushing to remote."
    # Commit changes with a timestamp
    git commit -m "Hourly Vault Backup: $(date)"
    
    # Push changes to the remote repository
    # The -u flag sets the upstream branch for the current branch
    git push -u origin main
    echo "Successfully pushed changes to the remote repository."
  fi
  
  echo "Backup check complete. Sleeping for 1 hour..."
  sleep 3600
done
