# config.py
import os
from dotenv import load_dotenv

load_dotenv()

# Core
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")  # 兼容旧的 Header 方式（可选）
TIMEZONE = os.getenv("TZ", "Asia/Kuala_Lumpur")

# Admin 登录（推荐）
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
# 如未显式设置 ADMIN_PASS，则回退用 ADMIN_KEY（便于渐进迁移）
ADMIN_PASS = os.getenv("ADMIN_PASS") or os.getenv("ADMIN_KEY", "")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please")  # 用于签名会话 Cookie，务必在 Railway 设置为强随机串

# 数据目录（建议在 Railway 绑定 Volume 到 /data）
DATA_DIR = os.getenv("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)

# 文件与目录
USER_FILE = os.path.join(DATA_DIR, "users.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
RANDOM_DIR = os.path.join(MEDIA_DIR, "random")
GROUPS_FILE = os.path.join(DATA_DIR, "message_groups.json")
SCHEDULES_FILE = os.path.join(DATA_DIR, "schedules.json")

for d in [BACKUP_DIR, MEDIA_DIR, RANDOM_DIR]:
    os.makedirs(d, exist_ok=True)

# 文案模板（当没配置信息组时的兜底）
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
    {"hour": 9, "minute": 0},
    {"hour": 12, "minute": 0},
    {"hour": 15, "minute": 0},
]

# 兜底图片（如需）
DEFAULT_IMAGE = os.path.join(MEDIA_DIR, "default.jpg") if os.path.exists(MEDIA_DIR) else None
