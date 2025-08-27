# config.py (for Railway/containers)
import os
from dotenv import load_dotenv

load_dotenv()

# Core
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")  # if set, required in X-Admin-Key header for write actions
TIMEZONE = os.getenv("TZ", "Asia/Kuala_Lumpur")

# Use a data dir so you can mount a Railway Volume at /data for persistence
DATA_DIR = os.getenv("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)

# Files & Dirs
USER_FILE = os.path.join(DATA_DIR, "users.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
RANDOM_DIR = os.path.join(MEDIA_DIR, "random")
GROUPS_FILE = os.path.join(DATA_DIR, "message_groups.json")
SCHEDULES_FILE = os.path.join(DATA_DIR, "schedules.json")

for d in [BACKUP_DIR, MEDIA_DIR, RANDOM_DIR]:
    os.makedirs(d, exist_ok=True)

# Messages
DEFAULT_MESSAGE_TEMPLATE = "🌞 每日提醒：现在是 {time}"
RANDOM_MESSAGES = [
    "新的一天，新的开始！\n\n愿你今天充满活力，事事顺心！",
    "早安，愿你今天充满活力！\n\n记得保持微笑，生活会更美好！",
    "每一天都是新的机会，加油！\n\n相信自己，你可以做到最好！",
    "保持微笑，生活会更美好！\n\n让快乐成为你今天的主题！",
    "愿你今天比昨天更进步！\n\n继续努力，未来可期！",
]

# Default schedules (used only if schedules.json is empty/missing)
SCHEDULES_DEFAULT = [
    {"hour": 9, "minute": 0},
    {"hour": 12, "minute": 0},
    {"hour": 15, "minute": 0},
]
# Media fallback
DEFAULT_IMAGE = os.path.join(MEDIA_DIR, "default.jpg") if os.path.exists(MEDIA_DIR) else None
