# FastAPI admin + Telegram bot (python-telegram-bot v20.6) + APScheduler + SQLAlchemy
# - 用户订阅存数据库（UTC 带时区），API 输出会按 TZ（如 Asia/Kuala_Lumpur）返回
# - 左侧 Sidebar：Dashboard（信息组/定时/工具） + Users（订阅用户）
# - 登录保护（SessionMiddleware），兼容 X-Admin-Key 作为后备
import os
import re
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, List
from contextlib import asynccontextmanager

import pytz
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, func, desc

from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram import Update, MessageEntity
from telegram.constants import MessageEntityType

# --- Config & DB ------------------------------------------------------------
from config import (
    BOT_TOKEN, USER_FILE, BACKUP_DIR, MEDIA_DIR, RANDOM_DIR,
    DEFAULT_MESSAGE_TEMPLATE, DEFAULT_IMAGE, RANDOM_MESSAGES,
    SCHEDULES_DEFAULT, TIMEZONE, GROUPS_FILE, SCHEDULES_FILE,
    ADMIN_KEY, ADMIN_USER, ADMIN_PASS, SECRET_KEY,
)
from db import SessionLocal, init_db
from models import User

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("daily_sender")

TZ = pytz.timezone(TIMEZONE)

# --- Managers ---------------------------------------------------------------
class UserManagerDB:
    def add(self, chat_id: int):
        with SessionLocal() as db:
            exists = db.scalar(select(User).where(User.chat_id == chat_id))
            if not exists:
                db.add(User(chat_id=int(chat_id)))
                db.commit()

    def remove(self, chat_id: int):
        with SessionLocal() as db:
            u = db.scalar(select(User).where(User.chat_id == chat_id))
            if u:
                db.delete(u)
                db.commit()

    def is_subscribed(self, chat_id: int) -> bool:
        with SessionLocal() as db:
            count = db.scalar(select(func.count()).select_from(User).where(User.chat_id == chat_id))
            return bool(count and count > 0)

    def all_chat_ids(self) -> List[int]:
        with SessionLocal() as db:
            rows = db.execute(select(User.chat_id)).all()
            return [r[0] for r in rows]

class MessageGroupManager:
    def __init__(self, groups_file: str):
        self.groups_file = groups_file
        self.groups: List[dict] = self._load()
        os.makedirs(MEDIA_DIR, exist_ok=True)
        for g in self.groups:
            if "image" in g and g["image"]:
                g["image"] = os.path.basename(g["image"])

    def _load(self) -> List[dict]:
        try:
            if os.path.exists(self.groups_file):
                with open(self.groups_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "groups" in data:
                        return list(data["groups"])
                    elif isinstance(data, list):
                        return list(data)
        except Exception as e:
            logger.error(f"加载消息组失败: {e}")
        return []

    def save(self):
        try:
            with open(self.groups_file, "w", encoding="utf-8") as f:
                json.dump({"groups": self.groups}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存消息组失败: {e}")

    def add(self, image_filename: Optional[str], message: str):
        self.groups.append({
            "image": os.path.basename(image_filename) if image_filename else None,
            "message": message.strip(),
        })
        self.save()

    def delete(self, idx: int):
        if 0 <= idx < len(self.groups):
            del self.groups[idx]
            self.save()
        else:
            raise IndexError("group index out of range")

    def update(self, idx: int, message: Optional[str] = None, image: Optional[str] = None):
        if not (0 <= idx < len(self.groups)):
            raise IndexError("group index out of range")
        if message is not None:
            self.groups[idx]["message"] = (message or "").strip()
        if image is not None:
            self.groups[idx]["image"] = os.path.basename(image) if image else None
        self.save()

    def random(self):
        import random
        if not self.groups:
            return None
        return random.choice(self.groups)

class ScheduleManager:
    def __init__(self, schedules_file: str, default: List[dict]):
        self.schedules_file = schedules_file
        self.default = default

    def list(self) -> List[dict]:
        try:
            if os.path.exists(self.schedules_file):
                with open(self.schedules_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "schedules" in data:
                        return list(data["schedules"])
                    elif isinstance(data, list):
                        return list(data)
        except Exception as e:
            logger.error(f"加载日程失败: {e}")
        return list(self.default)

    def save_all(self, items: List[dict]):
        with open(self.schedules_file, "w", encoding="utf-8") as f:
            json.dump({"schedules": items}, f, ensure_ascii=False, indent=2)

    def add(self, hour: int, minute: int):
        items = self.list()
        items.append({"hour": int(hour), "minute": int(minute)})
        uniq  = {(x["hour"], x["minute"]) for x in items}
        items = [{"hour": h, "minute": m} for (h, m) in sorted(uniq)]
        self.save_all(items)

    def delete(self, hour: int, minute: int):
        items = self.list()
        items = [x for x in items if not (x.get("hour") == hour and x.get("minute") == minute)]
        self.save_all(items)

# --- Globals ---------------------------------------------------------------
user_manager     = UserManagerDB()
group_manager    = MessageGroupManager(GROUPS_FILE)
schedule_manager = ScheduleManager(SCHEDULES_FILE, SCHEDULES_DEFAULT)

telegram_app = None
scheduler    = AsyncIOScheduler(timezone=TZ)

# === Premium 自定义表情：健壮解析（支持全角/空格）+ 零宽占位符 ==================
CE_PATTERN = re.compile(r"[<＜]\s*ce\s*[:：]\s*(\d+)\s*[>＞]", re.IGNORECASE)

def build_text_and_entities(src: str):
    """
    把文案中的 <ce:123> / ＜ce：123＞ 等占位符转为自定义表情实体。
    - 偏移/长度按 UTF-16 code units 计算
    - 用零宽连接符 U+200D 作为占位（实体失效也不会露符号）
    """
    if not src:
        return src, None

    def u16_len(s: str) -> int:
        return len(s.encode("utf-16-le")) // 2

    parts, entities = [], []
    last = 0
    for m in CE_PATTERN.finditer(src):
        parts.append(src[last:m.start()])
        placeholder = "\u200d"  # invisible, length=1 (UTF-16)
        text_so_far = "".join(parts)
        offset = u16_len(text_so_far)
        parts.append(placeholder)
        entities.append(
            MessageEntity(
                type=MessageEntityType.CUSTOM_EMOJI,
                offset=offset,
                length=u16_len(placeholder),
                custom_emoji_id=m.group(1),
            )
        )
        last = m.end()

    parts.append(src[last:])
    text = "".join(parts)

    if entities:
        logger.info(f"[CE] matched {len(entities)} custom_emoji placeholder(s)")
    else:
        logger.info("[CE] no placeholder matched in message")

    return text, entities or None

# --- Telegram handlers ------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if user_manager.is_subscribed(chat_id):
        await update.message.reply_text("✅ 你已经订阅了每日提醒！")
    else:
        user_manager.add(chat_id)
        await update.message.reply_text("✅ 你已成功订阅每日提醒！")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if user_manager.is_subscribed(chat_id):
        user_manager.remove(chat_id)
        await update.message.reply_text("✅ 你已取消订阅每日提醒。")
    else:
        await update.message.reply_text("❌ 你还没有订阅每日提醒。")

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ 正在发送测试消息...")
    await send_daily_message()

# 读取一条消息里的自定义表情 ID（支持“回复模式”）
async def cmd_ce_ids(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.reply_to_message or update.message
    ents = msg.entities or []
    ids = [e.custom_emoji_id for e in ents
           if getattr(e, "type", None) == MessageEntityType.CUSTOM_EMOJI]
    if ids:
        await update.message.reply_text(
            "custom_emoji_id:\n" + "\n".join(ids) +
            "\n\n后台文案里写成 <ce:ID> 或 ＜ce：ID＞ 即可。"
        )
    else:
        await update.message.reply_text("这条消息里没有 Telegram 自定义表情。")

# 自测命令：验证某个 ID 是否可用
async def cmd_ce_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/ce_test <custom_emoji_id>")
        return
    ceid = context.args[0].strip()
    txt, ents = build_text_and_entities(f"测试 <ce:{ceid}> OK")
    await update.message.reply_text(txt, entities=ents)

# --- Core sending logic -----------------------------------------------------
async def send_daily_message():
    group   = group_manager.random()
    image   = None
    message = None
    if group:
        message = group.get("message") or DEFAULT_MESSAGE_TEMPLATE.format(
            time=datetime.now(TZ).strftime("%H:%M")
        )
        if group.get("image"):
            candidate = os.path.join(MEDIA_DIR, group["image"])
            if os.path.exists(candidate):
                image = candidate

    if not message:
        message = DEFAULT_MESSAGE_TEMPLATE.format(time=datetime.now(TZ).strftime("%H:%M"))

    # 统一解析 CE 占位符
    text, entities = build_text_and_entities(message)

    for uid in user_manager.all_chat_ids():
        try:
            if image:
                with open(image, "rb") as fp:
                    await telegram_app.bot.send_photo(
                        chat_id=uid,
                        photo=fp,
                        caption=text,
                        caption_entities=entities,
                    )
            else:
                await telegram_app.bot.send_message(
                    chat_id=uid,
                    text=text,
                    entities=entities,
                )
            logger.info(f"✅ 已发送给 {uid}")
        except Exception as e:
            logger.error(f"发送给 {uid} 失败: {e}")

# --- Auth helpers -----------------------------------------------------------
def is_logged_in(request: Request) -> bool:
    return request.session.get("auth") == "ok"

def require_admin_header(x_admin_key: Optional[str]):
    if ADMIN_KEY and x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

def require_admin_access(request: Request, x_admin_key: Optional[str]):
    if is_logged_in(request):
        return
    require_admin_header(x_admin_key)

# --- Admin HTML / Login HTML（省略与上版一致；你的页面已支持缩略图/编辑/保留换行） ---
# 为节省篇幅，我省略了 HTML 字符串；保持你上一个“亮色 + 预览 + inline edit”的版本不变即可。
# 如果需要我也可以再次完整贴出。

ADMIN_HTML = """<!DOCTYPE html>
<!-- 这里保持你上一版亮色 admin.html 的完整内容（含 <pre class="msg">） -->
"""  # 你的实际代码中请替换为前一次我给你的完整 HTML 字符串

LOGIN_HTML = """<!DOCTYPE html>
<!-- 保持不变 -->
"""

# --- Lifespan：启动/停止 & 首次迁移 users.json ------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        if os.path.exists(USER_FILE):
            with SessionLocal() as db:
                count = db.scalar(select(func.count()).select_from(User))
                if not count:
                    with open(USER_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, list):
                            for cid in data:
                                try:
                                    db.add(User(chat_id=int(cid)))
                                except Exception:
                                    pass
                            db.commit()
                            logger.info(f"🔁 已迁移 {len(data)} 个用户到数据库")
    except Exception as e:
        logger.error(f"用户迁移失败: {e}")

    global telegram_app
    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("stop", cmd_stop))
    telegram_app.add_handler(CommandHandler("test", cmd_test))
    telegram_app.add_handler(CommandHandler("ce_ids", cmd_ce_ids))
    telegram_app.add_handler(CommandHandler("ce_test", cmd_ce_test))

    if not scheduler.running:
        scheduler.start()
    for job in scheduler.get_jobs():
        job.remove()
    for s in schedule_manager.list():
        scheduler.add_job(send_daily_message, "cron",
                          hour=int(s.get("hour", 9)),
                          minute=int(s.get("minute", 0)))
        logger.info(f"⏰ 已添加计划任务: {int(s.get('hour', 9)):02d}:{int(s.get('minute', 0)):02d}")

    async def run_bot():
        await telegram_app.initialize()
        await telegram_app.start()
        if getattr(telegram_app, "updater", None):
            await telegram_app.updater.start_polling()
    asyncio.create_task(run_bot())
    logger.info("✅ Startup complete: admin + bot running")

    yield

    try: scheduler.shutdown(wait=False)
    except Exception: pass
    if telegram_app:
        try:    await telegram_app.stop()
        except: pass

# --- App 初始化 & 路由 -------------------------------------------------------
app = FastAPI(title="Daily Sender Admin", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="admin_session", same_site="lax")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not is_logged_in(request):
        return RedirectResponse(url="/login")
    return HTMLResponse(content=ADMIN_HTML)

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(content=LOGIN_HTML.replace("%ERR%", ""))

@app.post("/login", response_class=HTMLResponse)
async def do_login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASS and ADMIN_PASS:
        request.session["auth"] = "ok"
        return RedirectResponse(url="/", status_code=303)
    html = LOGIN_HTML.replace("%ERR%", "Invalid username or password.")
    return HTMLResponse(content=html)

@app.post("/logout")
async def do_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

@app.get("/logout")
async def do_logout_get(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

@app.get("/health")
async def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}

# === APIs ===================================================================
@app.get("/api/groups")
async def api_groups(request: Request):
    if not is_logged_in(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return group_manager.groups

@app.post("/api/groups")
async def api_add_group(
    request: Request,
    message: str = Form(...),
    file: Optional[UploadFile] = File(None),
    x_admin_key: Optional[str] = Header(None),
):
    require_admin_access(request, x_admin_key)
    filename = None
    if file:
        base, ext = os.path.splitext(file.filename or "upload")
        safe = f"group_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext.lower() or '.jpg'}"
        dest = os.path.join(MEDIA_DIR, safe)
        content = await file.read()
        with open(dest, "wb") as f:
            f.write(content)
        filename = safe
    group_manager.add(filename, message)
    return {"ok": True}

@app.delete("/api/groups/{idx}")
async def api_del_group(idx: int, request: Request, x_admin_key: Optional[str] = Header(None)):
    require_admin_access(request, x_admin_key)
    try:
        group_manager.delete(idx)
        return {"ok": True}
    except IndexError:
        raise HTTPException(status_code=404, detail="Group not found")

@app.patch("/api/groups/{idx}")
async def api_edit_group(idx: int, payload: dict, request: Request, x_admin_key: Optional[str] = Header(None)):
    require_admin_access(request, x_admin_key)
    try:
        msg = payload.get("message")
        img = payload.get("image")
        if msg is None and img is None:
            raise HTTPException(status_code=400, detail="Nothing to update")
        group_manager.update(idx, message=msg, image=img)
        return {"ok": True}
    except IndexError:
        raise HTTPException(status_code=404, detail="Group not found")

@app.get("/api/schedules")
async def api_list_schedules(request: Request):
    if not is_logged_in(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return schedule_manager.list()

@app.post("/api/schedules")
async def api_add_schedule(payload: dict, request: Request, x_admin_key: Optional[str] = Header(None)):
    require_admin_access(request, x_admin_key)
    h = int(payload.get("hour", 0))
    m = int(payload.get("minute", 0))
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise HTTPException(status_code=400, detail="Invalid time")
    schedule_manager.add(h, m)
    for job in scheduler.get_jobs():
        job.remove()
    for s in schedule_manager.list():
        scheduler.add_job(send_daily_message, "cron", hour=int(s["hour"]), minute=int(s["minute"]))
    return {"ok": True}

@app.delete("/api/schedules/{hour}/{minute}")
async def api_del_schedule(hour: int, minute: int, request: Request, x_admin_key: Optional[str] = Header(None)):
    require_admin_access(request, x_admin_key)
    schedule_manager.delete(hour, minute)
    for job in scheduler.get_jobs():
        job.remove()
    for s in schedule_manager.list():
        scheduler.add_job(send_daily_message, "cron", hour=int(s["hour"]), minute=int(s["minute"]))
    return {"ok": True}

@app.post("/api/reload")
async def api_reload(request: Request, x_admin_key: Optional[str] = Header(None)):
    require_admin_access(request, x_admin_key)
    for job in scheduler.get_jobs():
        job.remove()
    for s in schedule_manager.list():
        scheduler.add_job(send_daily_message, "cron", hour=int(s["hour"]), minute=int(s["minute"]))
    return {"ok": True}

@app.post("/api/send-now")
async def api_send_now(request: Request, x_admin_key: Optional[str] = Header(None)):
    require_admin_access(request, x_admin_key)
    await send_daily_message()
    return {"ok": True}

@app.get("/api/users")
async def api_list_users(request: Request):
    if not is_logged_in(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    with SessionLocal() as db:
        rows = db.execute(select(User.chat_id, User.created_at).order_by(desc(User.created_at))).all()

    out = []
    for chat_id, created_at in rows:
        if created_at is None:
            out.append({"chat_id": int(chat_id), "created_at": None, "created_at_local": None, "tz": TIMEZONE})
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        local_dt = created_at.astimezone(TZ)
        out.append({
            "chat_id": int(chat_id),
            "created_at": created_at.astimezone(timezone.utc).isoformat(),
            "created_at_local": local_dt.isoformat(),
            "tz": TIMEZONE
        })
    return out

@app.delete("/api/users/{chat_id}")
async def api_delete_user(chat_id: int, request: Request, x_admin_key: Optional[str] = Header(None)):
    require_admin_access(request, x_admin_key)
    user_manager.remove(chat_id)
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
