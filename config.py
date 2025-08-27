import os
from dotenv import load_dotenv

load_dotenv()

# Core
BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
ADMIN_KEY  = os.getenv("ADMIN_KEY", "")  # 可选：兼容旧的 X-Admin-Key
TIMEZONE   = os.getenv("TZ", "Asia/Kuala_Lumpur")  # 例如 Asia/Kuala_Lumpur / Asia/Shanghai

# Admin 登录
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS") or os.getenv("ADMIN_KEY", "")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please")  # 会话签名，务必换成强随机串

# 文件与目录（建议绑定 Volume 到 /data）
DATA_DIR   = os.getenv("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)

# 数据库：优先用 DATABASE_URL（Railway Postgres），否则回退到本地 SQLite
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{os.path.join(DATA_DIR, 'app.db')}"

# 仅信息组/排程仍走文件（方便导入导出）
USER_FILE      = os.path.join(DATA_DIR, "users.json")  # 仅用于首次迁移
BACKUP_DIR     = os.path.join(DATA_DIR, "backups")
MEDIA_DIR      = os.path.join(DATA_DIR, "media")
RANDOM_DIR     = os.path.join(MEDIA_DIR, "random")
GROUPS_FILE    = os.path.join(DATA_DIR, "message_groups.json")
SCHEDULES_FILE = os.path.join(DATA_DIR, "schedules.json")

for d in (BACKUP_DIR, MEDIA_DIR, RANDOM_DIR):
    os.makedirs(d, exist_ok=True)

# 文案模板（当没有信息组时的兜底）
DEFAULT_MESSAGE_TEMPLATE = "🌞 每日提醒：现在是 {time}"
RANDOM_MESSAGES = [
    "新的一天，新的开始！\n\n愿你今天充满活力，事事顺心！",
    "早安，愿你今天充满活力！\n\n记得保持微笑，生活会更美好！",
    "每一天都是新的机会，加油！\n\n相信自己，你可以做到最好！",
    "保持微笑，生活会更美好！\n\n让快乐成为你今天的主题！",
    "愿你今天比昨天更进步！\n\n继续努力，未来可期！",
]

# 默认定时（当 schedules.json 不存在/为空时使用）
SCHEDULES_DEFAULT = [
    {"hour": 9,  "minute": 0},
    {"hour": 12, "minute": 0},
    {"hour": 15, "minute": 0},
]

# 兜底图片（如需）
DEFAULT_IMAGE = os.path.join(MEDIA_DIR, "default.jpg") if os.path.exists(MEDIA_DIR) else None
