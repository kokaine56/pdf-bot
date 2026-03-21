import os

# --- CONFIGURATION ---

# Bot Token
# WARNING: It is unsafe to hardcode tokens. Consider using only os.getenv in production.
TOKEN = os.getenv('BOT_TOKEN', '8247394659:AAHNbqmZFGnzwy2rhbcNQM4i1VvGpkGoByE')

# Admin ID for restricted commands
try:
    ADMIN_ID = int(os.getenv('ADMIN_ID', '8334095190'))
except ValueError:
    ADMIN_ID = 0

# Channel/Group ID where database backups will be sent automatically
# NOTE: The bot must be a member (and preferably admin) of this group/channel.
# You can get the ID by forwarding a message to @userinfobot or looking at logs.
try:
    DB_CHANNEL_ID = int(os.getenv('DB_CHANNEL_ID', '-1003589767050')) 
except ValueError:
    DB_CHANNEL_ID = 0

# Auto-backup interval in seconds (default: 86400s = 24 hours)
try:
    BACKUP_INTERVAL = int(os.getenv('BACKUP_INTERVAL', '1800'))
except ValueError:
    BACKUP_INTERVAL = 86400

# Path to Poppler (if not in PATH)
POPPLER_PATH = os.getenv('POPPLER_PATH', None)

# Local Telegram API Server URL (Required for files > 20MB)
# Changed to default to the standard local server address
LOCAL_API_URL = os.getenv('LOCAL_API_URL', 'https://pdf-bot-production-37a4.up.railway.app/')

# Max file size limit (500MB)
MAX_FILE_SIZE = 500 * 1024 * 1024
