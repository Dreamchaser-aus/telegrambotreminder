# FastAPI admin + Telegram bot (python-telegram-bot v20.6) + APScheduler + SQLAlchemy
# - 用户订阅存数据库（UTC 带时区），API 输出会按 TZ（如 Asia/Kuala_Lumpur）返回
# - 左侧 Sidebar：Dashboard（信息组/定时/工具） + Users（订阅用户）
# - 登录保护（SessionMiddleware），兼容 X-Admin-Key 作为后备
import os
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
from telegram import Update

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
    """DB-backed user manager."""
    def add(self, chat_id: int):
        with SessionLocal() as db:
            exists = db.scalar(select(User).where(User.chat_id == chat_id))
            if not exists:
                db.add(User(chat_id=int(chat_id)))  # created_at 默认写入 UTC
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
        # 规范化图片文件名
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
user_manager   = UserManagerDB()
group_manager  = MessageGroupManager(GROUPS_FILE)
schedule_manager = ScheduleManager(SCHEDULES_FILE, SCHEDULES_DEFAULT)

telegram_app = None
scheduler    = AsyncIOScheduler(timezone=TZ)

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
    group  = group_manager.random()
    image  = None
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

    for uid in user_manager.all_chat_ids():
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
    if is_logged_in(request):
        return
    require_admin_header(x_admin_key)

# --- Admin HTML / Login HTML ------------------------------------------------
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Daily Sender Admin</title>
  <style>
    :root{
      --bg:#0b1320; --panel:#121d33; --line:#223054; --line2:#23365f;
      --text:#eef2ff; --muted:#9fb0d9; --accent:#2546f2; --danger:#b3363f; --ok:#21955e;
    }
    *{ box-sizing:border-box; }
    body{ font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; margin:0; background:var(--bg); color:var(--text); }
    header{ padding:16px 20px; background:#111a2e; border-bottom:1px solid #203055; display:flex; align-items:center; gap:12px; }
    h1{ margin:0; font-size:18px; }
    .layout{ max-width:1200px; margin:18px auto; padding:0 16px; display:grid; grid-template-columns:220px 1fr; gap:16px; }
    aside{ background:#111a2e; border:1px solid #203055; border-radius:14px; padding:10px; height:fit-content; position:sticky; top:14px; }
    .navlink{ display:block; width:100%; text-align:left; border:1px solid var(--line); background:#0f1a2d; color:var(--text); padding:10px 12px; border-radius:10px; margin:6px 0; cursor:pointer; }
    .navlink.active{ background:var(--accent); border-color:var(--accent); }
    main{ display:block; }
    section{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:16px; margin-bottom:18px; }
    h2{ margin:6px 0 14px; font-size:16px; }
    .row{ display:flex; gap:12px; flex-wrap:wrap; }
    input,textarea,select,button{ font:inherit; padding:10px 12px; border-radius:10px; border:1px solid #334770; background:#0f1a2d; color:#eaf0ff; }
    button{ cursor:pointer; background:var(--accent); border-color:var(--accent); line-height:1.2; }
    button:disabled{ opacity:.6; }
    label{ font-size:12px; opacity:.85; display:block; margin-bottom:6px; }
    .grid{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }
    .muted{ opacity:.85; font-size:13px; color:var(--muted); }
    .pill{ font-size:12px; padding:2px 8px; background:#1b2d52; border-radius:999px; border:1px solid #29437a; }
    .inline{ display:inline-flex; gap:8px; align-items:center; }
    .danger{ background:var(--danger); border-color:var(--danger); }
    .success{ background:var(--ok); border-color:var(--ok); }
    .spacer{ flex:1; }
    #flash{ position:fixed; right:16px; bottom:16px; background:#1c2c50; border:1px solid #2c4781; padding:10px 12px; border-radius:12px; display:none; }
    .panel{ display:none; }
    .panel.active{ display:block; }
    .stat{ display:inline-block; padding:8px 10px; border:1px solid var(--line); background:#0f1a2d; border-radius:10px; margin-right:8px; }
    .searchbar{ display:flex; gap:8px; align-items:center; margin:8px 0 12px; }
    .mono{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace; }

    /* 表格：避免重叠与溢出 */
    table{
      width:100%;
      border-collapse:separate;     /* 比 collapse 更稳 */
      border-spacing:0;
      table-layout:auto;            /* 自适应列宽 */
    }
    th,td{
      border-bottom:1px solid var(--line2);
      padding:12px 10px;
      vertical-align:middle;
      text-align:left;
    }
    th:nth-child(1),td:nth-child(1){ width:56px; }     /* 序号列 */
    th:nth-child(2),td:nth-child(2){ width:280px; }    /* 图片列 */
    th:nth-child(4),td:nth-child(4){ width:130px; }    /* 操作列 */
    th:nth-child(3),td:nth-child(3){ min-width:320px; }/* 文案列最小宽度 */
    td.actions{ text-align:right; }
    td .link{
      display:inline-block; max-width:100%;
      overflow:hidden; text-overflow:ellipsis; white-space:nowrap; /* 文件名过长省略 */
    }
    td .msg{
      white-space:pre-wrap; word-break:break-word; /* 文案换行 */
    }
  </style>
</head>
<body>
  <header>
    <h1>Daily Sender Admin</h1>
    <div class="spacer"></div>
    <form action="/logout" method="post"><button>Logout</button></form>
  </header>

  <div class="layout">
    <aside>
      <button class="navlink active" data-name="dashboard" onclick="switchPanel('dashboard')">Dashboard</button>
      <button class="navlink" data-name="users" onclick="switchPanel('users')">Users</button>
    </aside>

    <main>
      <!-- DASHBOARD -->
      <div id="panel-dashboard" class="panel active">
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
      </div>

      <!-- USERS -->
      <div id="panel-users" class="panel">
        <section>
          <h2>Subscribed Users</h2>
          <div class="inline" style="margin-bottom:8px;">
            <span class="stat">Total: <b id="userCount">0</b></span>
            <button onclick="loadUsers()">Refresh</button>
            <button onclick="exportUsersCSV()">Export CSV</button>
          </div>
          <div class="searchbar">
            <input id="userSearch" placeholder="Filter by chat_id..." oninput="renderUsers()"/>
          </div>
          <div id="usersTable"></div>
        </section>
      </div>
    </main>
  </div>

  <div id="flash"></div>

<script>
  // ===== Utilities =====
  const flash = (msg) => { const f=document.getElementById('flash'); f.textContent=msg; f.style.display='block'; setTimeout(()=>f.style.display='none',1500); };
  function switchPanel(name){
    document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
    document.getElementById('panel-'+name).classList.add('active');
    document.querySelectorAll('.navlink').forEach(a=>a.classList.toggle('active', a.dataset.name===name));
    if(name==='dashboard'){ loadGroups(); loadSchedules(); }
    if(name==='users'){ loadUsers(); }
  }
  const escapeHtml = (s)=> (s||"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
  const fmtLocalIso = (s)=> s ? s.replace('T',' ').replace(/\.\d+/, '') : '';

  // ===== Groups =====
  async function loadGroups(){
    const r  = await fetch('/api/groups');
    const j  = await r.json();
    const el = document.getElementById('groups');
    if(!j.length){ el.innerHTML='<p class="muted">No groups yet.</p>'; return; }
    el.innerHTML = `<table>
      <thead>
        <tr><th class="mono">#</th><th>Image</th><th>Message</th><th></th></tr>
      </thead>
      <tbody>
        ${j.map((g,i)=>`
          <tr>
            <td class="mono">${i+1}</td>
            <td>
              ${g.image
                ? `<span class="inline">
                     <span class="pill">IMG</span>
                     <a class="link" target="_blank" href="/media/${encodeURIComponent(g.image)}">${escapeHtml(g.image)}</a>
                   </span>`
                : '<span class="muted">None</span>'
              }
            </td>
            <td class="msg">${escapeHtml(g.message)}</td>
            <td class="actions"><button class="danger" onclick="delGroup(${i})">Delete</button></td>
          </tr>
        `).join('')}
      </tbody>
    </table>`;
  }
  async function addGroup(){
    const fd = new FormData();
    const f  = document.getElementById('image').files[0];
    if(f){ fd.append('file', f); }
    const msg = document.getElementById('message').value.trim();
    if(!msg){ flash('Message required'); return; }
    fd.append('message', msg);
    const r = await fetch('/api/groups', { method:'POST', body:fd });
    if(r.ok){ flash('Added'); document.getElementById('message').value=''; document.getElementById('image').value=''; loadGroups(); }
    else{ flash('Add failed'); }
  }
  async function delGroup(idx){
    if(!confirm('Delete this group?')) return;
    const r = await fetch('/api/groups/'+idx, { method:'DELETE' });
    if(r.ok){ flash('Deleted'); loadGroups(); } else { flash('Delete failed'); }
  }

  // ===== Schedules =====
  async function loadSchedules(){
    const r = await fetch('/api/schedules'); const j = await r.json();
    const el = document.getElementById('schedules');
    if(!j.length){ el.innerHTML='<p class="muted">No schedules. Add one above.</p>'; return; }
    el.innerHTML = `<table>
      <thead><tr><th>Time</th><th></th></tr></thead>
      <tbody>${j.map(s=>`<tr>
        <td class="mono">${String(s.hour).padStart(2,'0')}:${String(s.minute).padStart(2,'0')}</td>
        <td class="actions"><button class="danger" onclick="delSchedule(${s.hour},${s.minute})">Delete</button></td>
      </tr>`).join('')}</tbody></table>`;
  }
  async function addSchedule(){
    const h = +document.getElementById('h').value, m = +document.getElementById('m').value;
    const r = await fetch('/api/schedules', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({hour:h, minute:m})
    });
    if(r.ok){ flash('Added'); reloadJobs(); } else { flash('Failed'); }
  }
  async function delSchedule(h,m){
    const r = await fetch(`/api/schedules/${h}/${m}`, { method:'DELETE' });
    if(r.ok){ flash('Deleted'); reloadJobs(); } else { flash('Failed'); }
  }
  async function reloadJobs(){ const r = await fetch('/api/reload', { method:'POST' }); flash(r.ok?'Reloaded':'Failed'); loadSchedules(); }
  async function sendNowRandom(){ const r = await fetch('/api/send-now', { method:'POST' }); flash(r.ok?'Sent':'Failed'); }

  // ===== Users =====
  let _users = [];
  async function loadUsers(){
    const r = await fetch('/api/users');
    const el = document.getElementById('usersTable');
    if(!r.ok){ el.innerHTML = '<p class="muted">Unauthorized</p>'; return; }
    _users = await r.json();
    document.getElementById('userCount').textContent = _users.length;
    renderUsers();
  }
  function renderUsers(){
    const q = (document.getElementById('userSearch').value||'').trim();
    const data = _users.filter(u => !q || String(u.chat_id).includes(q));
    const el = document.getElementById('usersTable');
    if(!data.length){ el.innerHTML = '<p class="muted">No users.</p>'; return; }
    const tz = (data[0] && data[0].tz) ? data[0].tz : 'Local';
    el.innerHTML = `<table>
      <thead><tr><th>#</th><th>chat_id</th><th>Subscribed (${tz})</th><th></th></tr></thead>
      <tbody>${data.map((u,i)=>`<tr>
        <td class="mono">${i+1}</td>
        <td class="mono">${u.chat_id}</td>
        <td class="mono">${fmtLocalIso(u.created_at_local)}</td>
        <td class="actions"><button class="danger" onclick="delUser(${u.chat_id})">Remove</button></td>
      </tr>`).join('')}</tbody></table>`;
  }
  async function delUser(chat_id){
    if(!confirm('Remove this user?')) return;
    const r = await fetch('/api/users/'+chat_id, { method:'DELETE' });
    if(r.ok){ flash('Removed'); loadUsers(); } else { flash('Failed'); }
  }
  function exportUsersCSV(){
    const rows = [['chat_id','created_at_utc','created_at_local','tz']]
      .concat(_users.map(u=>[u.chat_id, u.created_at||'', u.created_at_local||'', u.tz||'']));
    const csv = rows.map(r=>r.map(x=>`"${String(x).replaceAll('"','""')}"`).join(',')).join('\n');
    const blob = new Blob([csv], {type:'text/csv;charset=utf-8;'});
    const url  = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href=url; a.download='subscribed_users.csv'; a.click();
    URL.revokeObjectURL(url);
  }

  // 初始进入 Dashboard
  switchPanel('dashboard');
</script>
</body>
</html>

LOGIN_HTML = r"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Login</title>
  <style>
    body{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin:0; background:#0b1320; color:#eef2ff; display:flex; align-items:center; justify-content:center; min-height:100vh; }
    form{ width:min(92vw,420px); background:#121d33; border:1px solid #223054; border-radius:14px; padding:22px; }
    h1{ margin:0 0 12px; font-size:18px; }
    label{ font-size:12px; opacity:.85; display:block; margin:10px 0 6px; }
    input,button{ width:100%; padding:10px 12px; border-radius:10px; border:1px solid #334770; background:#0f1a2d; color:#eaf0ff; }
    button{ margin-top:12px; background:#2546f2; border-color:#2546f2; cursor:pointer; }
    .err{ color:#ff8f8f; margin:8px 0 0; font-size:13px; min-height:1.2em; }
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

# --- Lifespan：启动/停止 & 首次迁移 users.json ------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化数据库 & 迁移旧 users.json（若 DB 为空）
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
                                    db.add(User(chat_id=int(cid)))  # UTC 默认时间
                                except Exception:
                                    pass
                            db.commit()
                            logger.info(f"🔁 已迁移 {len(data)} 个用户到数据库")
    except Exception as e:
        logger.error(f"用户迁移失败: {e}")

    # 启动 Telegram 机器人
    global telegram_app
    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("stop", cmd_stop))
    telegram_app.add_handler(CommandHandler("test", cmd_test))

    # 启动定时器
    if not scheduler.running:
        scheduler.start()
    for job in scheduler.get_jobs():
        job.remove()
    for s in schedule_manager.list():
        scheduler.add_job(send_daily_message, "cron",
                          hour=int(s.get("hour", 9)),
                          minute=int(s.get("minute", 0)))
        logger.info(f"⏰ 已添加计划任务: {int(s.get('hour', 9)):02d}:{int(s.get('minute', 0)):02d}")

    # 后台轮询
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
    return RedirectResponse(url="/login")

@app.get("/health")
async def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}

# === APIs（登录或 X-Admin-Key） ============================================
# 信息组
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

# 定时
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

# 用户（按 TZ 返回本地时间字符串）
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
        # 若 DB 中是 naive 时间，则按 UTC 处理
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        local_dt = created_at.astimezone(TZ)
        out.append({
            "chat_id": int(chat_id),
            "created_at": created_at.astimezone(timezone.utc).isoformat(),  # UTC
            "created_at_local": local_dt.isoformat(),                        # 按 TZ
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
