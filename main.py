# main.py
# FastAPI admin + Telegram bot (python-telegram-bot v20.6) + APScheduler
# - 使用 Session 登录保护管理页与写接口
# - 使用 lifespan 代替 on_event，避免 Deprecation 警告
import os
import json
import asyncio
import logging
from datetime import datetime
from typing import Optional, List
from contextlib import asynccontextmanager

import pytz
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram import Update

# --- Config & Paths ---------------------------------------------------------
from config import (
    BOT_TOKEN, USER_FILE, BACKUP_DIR, MEDIA_DIR, RANDOM_DIR,
    DEFAULT_MESSAGE_TEMPLATE, DEFAULT_IMAGE, RANDOM_MESSAGES,
    SCHEDULES_DEFAULT, TIMEZONE, GROUPS_FILE, SCHEDULES_FILE,
    ADMIN_KEY, ADMIN_USER, ADMIN_PASS, SECRET_KEY,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("daily_sender")

TZ = pytz.timezone(TIMEZONE)

# --- Storage helpers --------------------------------------------------------
class UserManager:
    def __init__(self, user_file: str):
        self.user_file = user_file
        self.users = self._load_users()
        self._ensure_dirs()

    def _ensure_dirs(self):
        for d in [os.path.dirname(self.user_file) or ".", BACKUP_DIR, MEDIA_DIR, RANDOM_DIR]:
            if d and not os.path.exists(d):
                os.makedirs(d, exist_ok=True)

    def _load_users(self):
        try:
            if os.path.exists(self.user_file):
                with open(self.user_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return set(data if isinstance(data, list) else [])
        except Exception as e:
            logger.error(f"加载用户文件失败: {e}")
        return set()

    def save(self):
        try:
            with open(self.user_file, "w", encoding="utf-8") as f:
                json.dump(sorted(list(self.users)), f, ensure_ascii=False, indent=2)
            backup_file = os.path.join(BACKUP_DIR, f"users_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            with open(backup_file, "w", encoding="utf-8") as f:
                json.dump(sorted(list(self.users)), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存用户失败: {e}")

    def add(self, user_id: int):
        self.users.add(user_id)
        self.save()

    def remove(self, user_id: int):
        self.users.discard(user_id)
        self.save()

    def is_subscribed(self, user_id: int) -> bool:
        return user_id in self.users


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
            "message": message.strip()
        })
        self.save()

    def delete(self, idx: int):
        if 0 <= idx < len(self.groups):
            del self.groups[idx]
            self.save()
        else:
            raise IndexError("group index out of range")

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
        uniq = {(x["hour"], x["minute"]) for x in items}
        items = [{"hour": h, "minute": m} for (h, m) in sorted(uniq)]
        self.save_all(items)

    def delete(self, hour: int, minute: int):
        items = self.list()
        items = [x for x in items if not (x.get("hour") == hour and x.get("minute") == minute)]
        self.save_all(items)


# --- Globals ---------------------------------------------------------------
user_manager = UserManager(USER_FILE)
group_manager = MessageGroupManager(GROUPS_FILE)
schedule_manager = ScheduleManager(SCHEDULES_FILE, SCHEDULES_DEFAULT)

telegram_app = None
scheduler = AsyncIOScheduler(timezone=TZ)

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

# --- Core sending logic -----------------------------------------------------
async def send_daily_message():
    group = group_manager.random()
    image = None
    message = None
    if group:
        message = group.get("message") or DEFAULT_MESSAGE_TEMPLATE.format(time=datetime.now(TZ).strftime("%H:%M"))
        if group.get("image"):
            candidate = os.path.join(MEDIA_DIR, group["image"])
            if os.path.exists(candidate):
                image = candidate

    if not message:
        message = DEFAULT_MESSAGE_TEMPLATE.format(time=datetime.now(TZ).strftime("%H:%M"))

    for uid in list(user_manager.users):
        try:
            if image:
                with open(image, "rb") as fp:
                    await telegram_app.bot.send_photo(chat_id=uid, photo=fp, caption=message)
            else:
                await telegram_app.bot.send_message(chat_id=uid, text=message)
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
    # 已登录 或 正确的 X-Admin-Key 二择一
    if is_logged_in(request):
        return
    require_admin_header(x_admin_key)

# --- Admin HTML -------------------------------------------------------------
ADMIN_HTML = r"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Daily Sender Admin</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 0; background: #0b1320; color: #eef2ff; }
    header { padding: 16px 20px; background: #111a2e; border-bottom: 1px solid #203055; display:flex; align-items:center; gap:12px; }
    h1 { margin: 0; font-size: 18px; }
    main { max-width: 920px; margin: 24px auto; padding: 0 16px; }
    section { background: #121d33; border: 1px solid #223054; border-radius: 14px; padding: 16px; margin-bottom: 18px; }
    h2 { margin: 6px 0 14px; font-size: 16px; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; }
    input, textarea, select, button { font: inherit; padding: 10px 12px; border-radius: 10px; border: 1px solid #334770; background: #0f1a2d; color: #eaf0ff; }
    button { cursor: pointer; background: #2546f2; border-color: #2546f2; }
    button:disabled{ opacity: .6; }
    label { font-size: 12px; opacity: .85; display:block; margin-bottom: 6px; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    .muted { opacity: .85; font-size: 13px; }
    table{ width:100%; border-collapse: collapse; }
    th, td{ border-bottom:1px solid #23365f; padding: 10px 8px; vertical-align: top; }
    .pill{ font-size:12px; padding:2px 8px; background:#1b2d52; border-radius:999px; border:1px solid #29437a;}
    .inline{ display:inline-flex; gap:8px; align-items:center;}
    .danger{ background:#b3363f; border-color:#b3363f;}
    .success{ background:#21955e; border-color:#21955e;}
    #flash{ position: fixed; right:16px; bottom:16px; background:#1c2c50; border:1px solid #2c4781; padding:10px 12px; border-radius:12px; display:none; }
    .spacer{flex:1}
  </style>
</head>
<body>
  <header>
    <h1>Daily Sender Admin</h1>
    <div class="spacer"></div>
    <form action="/logout" method="post"><button>Logout</button></form>
  </header>
  <main>
    <section>
      <h2>Add Message Group</h2>
      <div class="grid">
        <div>
          <label>Image (optional)</label>
          <input type="file" id="image"/>
        </div>
        <div>
          <label>Message Text</label>
          <textarea id="message" rows="5" placeholder="Enter message text..."></textarea>
        </div>
      </div>
      <div style="margin-top:10px;" class="inline">
        <button onclick="addGroup()">Add Group</button>
        <button class="success" onclick="sendNowRandom()">Send Random Now (All)</button>
      </div>
    </section>

    <section>
      <h2>Groups</h2>
      <div id="groups"></div>
    </section>

    <section>
      <h2>Schedules</h2>
      <div class="row">
        <div>
          <label>Hour</label>
          <input type="number" id="h" min="0" max="23" value="9"/>
        </div>
        <div>
          <label>Minute</label>
          <input type="number" id="m" min="0" max="59" value="0"/>
        </div>
        <div class="inline">
          <button onclick="addSchedule()">Add</button>
          <span class="muted">Server timezone is used.</span>
        </div>
      </div>
      <div id="schedules" style="margin-top:10px;"></div>
    </section>

    <section>
      <h2>Utilities</h2>
      <div class="inline">
        <button onclick="sendNowRandom()">Send Random Now (All)</button>
        <button onclick="reloadJobs()">Reload Cron Jobs</button>
        <span class="muted">Use for quick tests</span>
      </div>
    </section>
  </main>

  <div id="flash"></div>

<script>
  // 现在不需要 X-Admin-Key 了（已使用会话登录）
  const flash = (msg) => { const f=document.getElementById('flash'); f.textContent=msg; f.style.display='block'; setTimeout(()=>f.style.display='none',1500)};

  async function loadAll(){ await loadGroups(); await loadSchedules(); }

  async function loadGroups(){
    const r = await fetch('/api/groups'); const j = await r.json();
    const el = document.getElementById('groups');
    if(!j.length){ el.innerHTML='<p class="muted">No groups yet.</p>'; return; }
    el.innerHTML = `<table>
      <thead><tr><th>#</th><th>Image</th><th>Message</th><th></th></tr></thead>
      <tbody>${j.map((g,i)=>`<tr>
        <td>${i+1}</td>
        <td>${g.image? `<span class="inline"><span class="pill">IMG</span> <a target="_blank" href="/media/${g.image}">${g.image}</a></span>` : '<span class="muted">None</span>'}</td>
        <td style="white-space: pre-wrap;">${g.message.replaceAll('<','&lt;')}</td>
        <td><button class="danger" onclick="delGroup(${i})">Delete</button></td>
      </tr>`).join('')}</tbody></table>`;
  }

  async function addGroup(){
    const fd = new FormData();
    const f = document.getElementById('image').files[0];
    if(f){ fd.append('file', f); }
    const msg = document.getElementById('message').value.trim();
    if(!msg){ flash('Message required'); return; }
    fd.append('message', msg);
    const r = await fetch('/api/groups', { method:'POST', body: fd });
    if(r.ok){ flash('Added'); document.getElementById('message').value=''; document.getElementById('image').value=''; loadGroups(); }
    else{ flash('Add failed'); }
  }

  async function delGroup(idx){
    if(!confirm('Delete this group?')) return;
    const r = await fetch('/api/groups/'+idx, { method:'DELETE' });
    if(r.ok){ flash('Deleted'); loadGroups(); }
    else{ flash('Delete failed'); }
  }

  async function sendNowRandom(){
    const r = await fetch('/api/send-now', { method:'POST' });
    flash(r.ok? 'Sent':'Failed');
  }

  async function reloadJobs(){
    const r = await fetch('/api/reload', { method:'POST' });
    flash(r.ok? 'Reloaded':'Failed');
    loadSchedules();
  }

  async function loadSchedules(){
    const r = await fetch('/api/schedules'); const j = await r.json();
    const el = document.getElementById('schedules');
    if(!j.length){ el.innerHTML='<p class="muted">No schedules. Add one above.</p>'; return; }
    el.innerHTML = `<table>
      <thead><tr><th>Time</th><th></th></tr></thead>
      <tbody>${j.map(s=>`<tr>
        <td>${String(s.hour).padStart(2,'0')}:${String(s.minute).padStart(2,'0')}</td>
        <td><button class="danger" onclick="delSchedule(${s.hour},${s.minute})">Delete</button></td>
      </tr>`).join('')}</tbody></table>`;
  }

  async function addSchedule(){
    const h = +document.getElementById('h').value; const m= +document.getElementById('m').value;
    const r = await fetch('/api/schedules', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({hour:h, minute:m}) });
    if(r.ok){ flash('Added'); reloadJobs(); }
    else{ flash('Failed'); }
  }
  async function delSchedule(h,m){
    const r = await fetch(`/api/schedules/${h}/${m}`, { method:'DELETE' });
    if(r.ok){ flash('Deleted'); reloadJobs(); }
    else{ flash('Failed'); }
  }

  loadAll();
</script>
</body>
</html>
"""

LOGIN_HTML = r"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Login</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin:0; background:#0b1320; color:#eef2ff; display:flex; align-items:center; justify-content:center; min-height:100vh;}
    form { width: min(92vw, 420px); background:#121d33; border:1px solid #223054; border-radius:14px; padding:22px; }
    h1 { margin:0 0 12px; font-size:18px;}
    label{ font-size:12px; opacity:.85; display:block; margin:10px 0 6px;}
    input,button{ width:100%; padding:10px 12px; border-radius:10px; border:1px solid #334770; background:#0f1a2d; color:#eaf0ff; }
    button{ margin-top:12px; background:#2546f2; border-color:#2546f2; cursor:pointer;}
    .err{ color:#ff8f8f; margin:8px 0 0; font-size:13px; min-height:1.2em;}
  </style>
</head>
<body>
  <form method="post" action="/login">
    <h1>Admin Login</h1>
    <label>Username</label>
    <input name="username" autocomplete="username" required />
    <label>Password</label>
    <input name="password" type="password" autocomplete="current-password" required />
    <button>Login</button>
    <div class="err">%ERR%</div>
  </form>
</body>
</html>
"""

# --- FastAPI lifespan: 启动/停止逻辑 -----------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("stop", cmd_stop))
    telegram_app.add_handler(CommandHandler("test", cmd_test))

    if not scheduler.running:
        scheduler.start()
    # 默认按文件加载所有 cron
    for job in scheduler.get_jobs():
        job.remove()
    for s in schedule_manager.list():
        hour = int(s.get("hour", 9))
        minute = int(s.get("minute", 0))
        scheduler.add_job(send_daily_message, "cron", hour=hour, minute=minute)
        logger.info(f"⏰ 已添加计划任务: {hour:02d}:{minute:02d}")

    async def run_bot():
        await telegram_app.initialize()
        await telegram_app.start()
        if getattr(telegram_app, "updater", None):
            await telegram_app.updater.start_polling()
        while True:
            await asyncio.sleep(3600)

    asyncio.create_task(run_bot())
    logger.info("✅ Startup complete: admin + bot running")

    yield

    try:
        scheduler.shutdown(wait=False)
    except Exception:
        pass
    if telegram_app:
        try:
            await telegram_app.stop()
        except Exception:
            pass

# --- App init ---------------------------------------------------------------
app = FastAPI(title="Daily Sender Admin", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="admin_session", same_site="lax")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")

# --- Routes (login protected) -----------------------------------------------
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
    # 失败
    html = LOGIN_HTML.replace("%ERR%", "Invalid username or password.")
    return HTMLResponse(content=html)

@app.post("/logout")
async def do_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")

# Health（无需登录）
@app.get("/health")
async def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}

# --- APIs（需要：已登录 或 有 X-Admin-Key） -------------------------------
@app.get("/api/groups")
async def api_groups(request: Request):
    if not is_logged_in(request):
        # 读接口你也可以放行，这里统一要求登录；如需放开可删这行
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
    # 立即重载
    for job in scheduler.get_jobs():
        job.remove()
    for s in schedule_manager.list():
        scheduler.add_job(send_daily_message, "cron", hour=int(s["hour"]), minute=int(s["minute"]))
    return {"ok": True}

@app.delete("/api/schedules/{hour}/{minute}")
async def api_del_schedule(hour: int, minute: int, request: Request, x_admin_key: Optional[str] = Header(None)):
    require_admin_access(request, x_admin_key)
    schedule_manager.delete(hour, minute)
    # 立即重载
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
