import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MANAGER_USERNAME = os.getenv("MANAGER_USERNAME", "Fishlme")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
SHOP_NAME = "Fishl Shop"

# GitHub backups. Token is stored only in Railway Variables, never in GitHub.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_BACKUP_BRANCH = os.getenv("GITHUB_BACKUP_BRANCH", "backups")
GITHUB_BACKUP_PATH = os.getenv("GITHUB_BACKUP_PATH", "backups/orders")
GITHUB_BACKUP_INTERVAL_MINUTES = int(
    os.getenv("GITHUB_BACKUP_INTERVAL_MINUTES", "360")
)
