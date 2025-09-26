# -*- coding: utf-8 -*-
"""
main.py - Daily Sender Admin with Visual Buttons Editor + Drag-drop + Stats + Preview
Replace your existing main.py with this file (backup original first).
"""

import os
import re
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from threading import Lock

import pytz
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, func, desc

from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton

# --- Config & DB (assumes your config.py, db.py, models.py exist) ---
from config import (
    BOT_TOKEN, USER_FILE, BACKUP_DIR, MEDIA_DIR, RANDOM_DIR,
    DEFAULT_MESSAGE_TEMPLATE, DEFAULT_IMAGE, RANDOM_MESSAGES,
    SCHEDULES_DEFAULT, TIMEZONE, GROUPS_FILE, SCHEDULES_FILE,
    ADMIN_KEY, ADMIN_USER, ADMIN_PASS, SECRET_KEY,
)
from db import SessionLocal, init_db
from models import User

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("daily_sender")

TZ = pytz.timezone(TIMEZONE)

# stats file path (use BACKUP_DIR if available)
STATS_FILE = os.path.join(BACKUP_DIR if BACKUP_DIR else ".", "button_clicks.json")
os.makedirs(os.path.dirname(STATS_FILE) or ".", exist_ok=True)
_stats_lock = Lock()

def _load_stats() -> Dict[str, Any]:
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"加载统计文件失败: {e}")
    # structure: { "counts": {"callback_data": n, ...}, "records": [ {ts, user_id, callback_data, chat_id}, ... ] }
    return {"counts": {}, "records": []}

def _save_stats(data: Dict[str, Any]):
    try:
        with _stats_lock:
            with open(STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存统计文件失败: {e}")

# ---------------- Managers ----------------
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
            if g.get("image"):
                g["image"] = os.path.basename(g["image"])
            if "buttons" not in g or not isinstance(g.get("buttons"), list):
                g["buttons"] = []

    def _load(self) -> List[dict]:
        try:
            if os.path.exists(self.groups_file):
                with open(self.groups_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    groups = []
                    if isinstance(data, dict) and "groups" in data:
                        groups = list(data["groups"])
                    elif isinstance(data, list):
                        groups = list(data)
                    for g in groups:
                        if "buttons" not in g or not isinstance(g.get("buttons"), list):
                            g["buttons"] = []
                    return groups
        except Exception as e:
            logger.error(f"加载消息组失败: {e}")
        return []

    def save(self):
        try:
            with open(self.groups_file, "w", encoding="utf-8") as f:
                json.dump({"groups": self.groups}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存消息组失败: {e}")

    def add(self, image_filename: Optional[str], message: str, buttons: Optional[List[dict]] = None):
        self.groups.append({
            "image": os.path.basename(image_filename) if image_filename else None,
            "message": message.strip(),
            "buttons": buttons or []
        })
        self.save()

    def delete(self, idx: int):
        if 0 <= idx < len(self.groups):
            del self.groups[idx]
            self.save()
        else:
            raise IndexError("group index out of range")

    def update(self, idx: int, message: Optional[str] = None, image: Optional[str] = None, buttons: Optional[List[dict]] = None):
        if not (0 <= idx < len(self.groups)):
            raise IndexError("group index out of range")
        if message is not None:
            self.groups[idx]["message"] = (message or "").strip()
        if image is not None:
            self.groups[idx]["image"] = os.path.basename(image) if image else None
        if buttons is not None:
            clean = []
            for b in (buttons or []):
                if not isinstance(b, dict):
                    continue
                text = str(b.get("text","")).strip()
                url = b.get("url")
                cb  = b.get("callback_data")
                if not text:
                    continue
                if url:
                    clean.append({"text": text, "url": str(url)})
                elif cb:
                    clean.append({"text": text, "callback_data": str(cb)})
                else:
                    continue
            self.groups[idx]["buttons"] = clean
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

# ---------------- Globals ----------------
user_manager     = UserManagerDB()
group_manager    = MessageGroupManager(GROUPS_FILE)
schedule_manager = ScheduleManager(SCHEDULES_FILE, SCHEDULES_DEFAULT)

telegram_app = None
scheduler    = AsyncIOScheduler(timezone=TZ)

# ---------------- Custom Emoji（HTML 标签法） ----------------
CE_PATTERN = re.compile(r"[<＜]\s*ce\s*[:：]\s*(\d+)\s*[>＞]", re.IGNORECASE)

def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def render_text_with_ce(src: str):
    if not src:
        return src, None

    parts = []
    last = 0
    matched = 0
    for m in CE_PATTERN.finditer(src):
        parts.append(_html_escape(src[last:m.start()]))
        ceid = m.group(1)
        parts.append(f'<tg-emoji emoji-id="{ceid}">🙂</tg-emoji>')
        last = m.end()
        matched += 1
    parts.append(_html_escape(src[last:]))

    if matched:
        logger.info(f"[CE/HTML] matched {matched} custom emoji placeholder(s)")
        return "".join(parts), "HTML"
    else:
        logger.info("[CE/HTML] no placeholder matched")
        return src, None

# ---------------- Telegram handlers ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if user_manager.is_subscribed(chat_id):
        await update.message.reply_text("✅ Cheers for jumpin’ on the MONOAUD Bot, mate! We’ll sling ya the latest promos as soon as they drop — stay tuned for the good stuff！")
    else:
        user_manager.add(chat_id)
        await update.message.reply_text("✅ Cheers for jumpin’ on the MONOAUD Bot, mate! We’ll sling ya the latest promos as soon as they drop — stay tuned for the good stuff！")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if user_manager.is_subscribed(chat_id):
        user_manager.remove(chat_id)
        await update.message.reply_text("✅ You’ve unsubscribed from the MONOAUD Bot. No worries, mate — you can rejoin anytime to catch our latest promos and offers!")
    else:
        await update.message.reply_text("❌ G’day mate 👋 You haven’t joined the MONOAUD Bot yet! Subscribe now to get the latest promos, free chips, and hot offers straight to your Telegram. Don’t miss out!")

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ 正在发送测试消息...")
    await send_daily_message()

async def cmd_ce_ids(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.reply_to_message or update.message
    ents = getattr(msg, "entities", []) or getattr(msg, "caption_entities", []) or []
    ids = [getattr(e, "custom_emoji_id", None) for e in ents if getattr(e, "type", None) == "custom_emoji"]
    ids = [x for x in ids if x]
    if ids:
        await update.message.reply_text(
            "custom_emoji_id:\n" + "\n".join(ids) +
            "\n\n后台文案可写成 <ce:ID> 或 ＜ce：ID＞ 发送这些自定义表情。"
        )
    else:
        await update.message.reply_text("这条消息里没有 Telegram 自定义表情。")

async def cmd_ce_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/ce_test <custom_emoji_id>")
        return
    ceid = context.args[0].strip()
    html = f'测试 <tg-emoji emoji-id="{ceid}">🙂</tg-emoji> OK'
    await update.message.reply_text(html, parse_mode="HTML")

# ---------------- Core sending (with Inline Buttons) ----------------
async def send_daily_message():
    group   = group_manager.random()
    image   = None
    message = None
    buttons = None
    if group:
        message = group.get("message") or DEFAULT_MESSAGE_TEMPLATE.format(
            time=datetime.now(TZ).strftime("%H:%M")
        )
        buttons = group.get("buttons") or []
        if group.get("image"):
            candidate = os.path.join(MEDIA_DIR, group["image"])
            if os.path.exists(candidate):
                image = candidate
    if not message:
        message = DEFAULT_MESSAGE_TEMPLATE.format(time=datetime.now(TZ).strftime("%H:%M"))

    text, parse_mode = render_text_with_ce(message)

    reply_markup = None
    if buttons:
        kb_rows = []
        for b in buttons:
            if b.get("url"):
                kb_rows.append([InlineKeyboardButton(text=b["text"], url=b["url"])])
            elif b.get("callback_data"):
                kb_rows.append([InlineKeyboardButton(text=b["text"], callback_data=b["callback_data"])])
        if kb_rows:
            reply_markup = InlineKeyboardMarkup(kb_rows)

    for uid in user_manager.all_chat_ids():
        try:
            if image:
                with open(image, "rb") as fp:
                    await telegram_app.bot.send_photo(
                        chat_id=uid,
                        photo=fp,
                        caption=text,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup
                    )
            else:
                await telegram_app.bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup
                )
            logger.info(f"✅ 已发送给 {uid}")
        except Exception as e:
            logger.error(f"发送给 {uid} 失败: {e}")

# ---------------- Callback handling + stats recording ----------------
async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query
        if not q:
            return
        data = q.data or ""
        # respond quickly
        try:
            await q.answer(text="已接收 👍")
        except Exception:
            pass

        # record stats if callback_data present
        if data:
            try:
                with _stats_lock:
                    stats = _load_stats()
                    stats_counts = stats.get("counts", {})
                    stats_records = stats.get("records", [])
                    stats_counts[data] = stats_counts.get(data, 0) + 1
                    stats_records.insert(0, {
                        "ts": datetime.utcnow().isoformat(),
                        "user_id": getattr(q.from_user, "id", None),
                        "username": getattr(q.from_user, "username", None),
                        "callback_data": data,
                        "chat_id": getattr(q.message.chat, "id", None) if q.message else None
                    })
                    # keep only recent 500 records
                    stats["counts"] = stats_counts
                    stats["records"] = stats_records[:500]
                    _save_stats(stats)
            except Exception as e:
                logger.error(f"记录点击统计失败: {e}")

        logger.info(f"Callback received: {data} from {q.from_user.id}")
        # example custom handling:
        if data.startswith("info:"):
            await q.message.reply_text(f"Info requested: {data[5:]}")
    except Exception as e:
        logger.error(f"callback handler error: {e}")

# ---------------- Auth helpers ----------------
def is_logged_in(request: Request) -> bool:
    return request.session.get("auth") == "ok"

def require_admin_header(x_admin_key: Optional[str]):
    if ADMIN_KEY and x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

def require_admin_access(request: Request, x_admin_key: Optional[str]):
    if is_logged_in(request):
        return
    require_admin_header(x_admin_key)

# ---------------- Admin HTML（包含 更友好编辑器 + drag/drop + preview + stats） ----------------
# For brevity in this response I keep HTML/JS concise but fully functional.
ADMIN_HTML = r'''
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Daily Sender Admin</title>
  <style>
    /* (styles similar to previous file, with some additions) */
    :root{ --bg:#f7fafc; --panel:#ffffff; --line:#e5e7eb; --muted:#64748b; --accent:#2563eb; --danger:#dc2626; --ok:#16a34a; --text:#0f172a;}
    *{box-sizing:border-box} body{font-family:system-ui,Segoe UI,Roboto,Arial;margin:0;background:var(--bg);color:var(--text)}
    header{padding:12px 16px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center}
    .layout{max-width:1200px;margin:18px auto;padding:0 16px;display:grid;grid-template-columns:220px 1fr;gap:16px}
    aside{background:var(--panel);border-radius:12px;padding:10px;border:1px solid var(--line);position:sticky;top:76px}
    .navlink{display:block;padding:10px;border-radius:8px;margin:6px 0;background:#fff;border:1px solid var(--line);cursor:pointer}
    .navlink.active{background:var(--accent);color:#fff;border-color:var(--accent)}
    main{min-height:60vh}
    section{background:var(--panel);padding:14px;border-radius:12px;border:1px solid var(--line);margin-bottom:12px}
    input,textarea,select,button{font:inherit;padding:8px;border-radius:8px;border:1px solid #d1d5db}
    .inline{display:inline-flex;gap:8px;align-items:center}
    .muted{color:var(--muted);font-size:13px}
    .buttons-editor{margin-top:10px;border:1px dashed var(--line);padding:8px;border-radius:8px;background:#fff}
    .btn-row{display:flex;gap:8px;align-items:center;margin-bottom:8px;padding:6px;border-radius:8px;background:#fbfdff;border:1px solid #f1f5f9;cursor:grab}
    .btn-row.dragging{opacity:0.5}
    .btn-row input[type="text"]{flex:1}
    .btn-row select{width:120px}
    .small{padding:6px 8px;border-radius:8px}
    .pill{display:inline-block;padding:4px 8px;border-radius:999px;background:#eef2ff;border:1px solid #dbe4ff}
    table{width:100%;border-collapse:collapse}
    th,td{padding:8px;border-bottom:1px solid #eef2f6;text-align:left}
    .preview-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
    .preview-button{padding:8px;border-radius:8px;border:1px solid #e2e8f0;background:#fff;text-align:center}
    .stats-table{max-height:260px;overflow:auto}
    @media(max-width:880px){.layout{grid-template-columns:1fr}.preview-grid{grid-template-columns:1fr}}
  </style>
</head>
<body>
  <header><h3 style="margin:0">Daily Sender Admin</h3><div style="flex:1"></div><form action="/logout" method="post"><button style="padding:8px;border-radius:8px">Logout</button></form></header>
  <div class="layout">
    <aside>
      <div class="navlink active" data-name="dashboard" onclick="switchPanel('dashboard')">Dashboard</div>
      <div class="navlink" data-name="users" onclick="switchPanel('users')">Users</div>
      <div class="navlink" data-name="stats" onclick="switchPanel('stats')">Stats</div>
    </aside>
    <main>
      <div id="panel-dashboard" class="panel active">
        <section>
          <h4>Add Message Group</h4>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
            <div>
              <label>Image (optional)</label><br/><input type="file" id="image"/>
            </div>
            <div>
              <label>Message Text</label><br/><textarea id="message" rows="4" placeholder="Enter message..."></textarea>
            </div>
          </div>

          <div style="margin-top:10px;">
            <label>Buttons (visual editor) — 支持拖拽排序</label>
            <div id="add-buttons-editor" class="buttons-editor" ondragover="onDragOver(event)" ondrop="onDrop(event)"></div>
            <div style="margin-top:6px" class="inline">
              <button type="button" onclick="addButtonToEditor('add-buttons-editor')" class="small">+ Add Button</button>
              <button type="button" onclick="previewEditor('add-buttons-editor','message')" class="small">Preview (local)</button>
              <input id="preview_chat_id" placeholder="Test chat_id" style="width:160px;padding:8px;border-radius:8px;margin-left:8px"/>
              <button type="button" onclick="sendPreview('add-buttons-editor','message')" class="small">Send Preview</button>
              <span class="muted" style="margin-left:8px">Preview -> send a test message to a chat_id (bot must be able to message that id)</span>
            </div>
            <div id="add-preview-area" style="margin-top:8px"></div>
          </div>

          <div style="margin-top:10px" class="inline">
            <button onclick="addGroup()">Add Group</button>
            <button onclick="sendNowRandom()" style="background:#16a34a;color:#fff;padding:8px;border-radius:8px;border:none;margin-left:8px">Send Now (All)</button>
          </div>
        </section>

        <section>
          <h4>Groups</h4>
          <div id="groups"></div>
        </section>

        <section>
          <h4>Schedules</h4>
          <div style="display:flex;gap:8px;align-items:center">
            <div><label>Hour</label><br/><input type="number" id="h" min="0" max="23" value="9"/></div>
            <div><label>Minute</label><br/><input type="number" id="m" min="0" max="59" value="0"/></div>
            <div><button onclick="addSchedule()">Add</button></div>
          </div>
          <div id="schedules" style="margin-top:8px"></div>
        </section>
      </div>

      <div id="panel-users" class="panel" style="display:none">
        <section>
          <h4>Subscribed Users</h4>
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
            <div class="pill">Total: <b id="userCount">0</b></div>
            <button onclick="loadUsers()">Refresh</button>
            <button onclick="exportUsersCSV()">Export CSV</button>
            <input id="userSearch" placeholder="Filter chat_id..." style="margin-left:8px;padding:6px;border-radius:6px" oninput="renderUsers()"/>
          </div>
          <div id="usersTable"></div>
        </section>
      </div>

      <div id="panel-stats" class="panel" style="display:none">
        <section>
          <h4>Button Click Stats</h4>
          <div style="display:flex;gap:8px;align-items:center;">
            <div style="flex:1">
              <h5>Top callbacks</h5>
              <div id="stats-top"></div>
            </div>
            <div style="width:320px">
              <h5>Recent records</h5>
              <div class="stats-table" id="stats-records"></div>
              <div style="margin-top:8px" class="inline">
                <button onclick="reloadStats()">Refresh</button>
                <button onclick="resetStats()" style="background:#dc2626;color:#fff;padding:6px;border-radius:6px">Clear</button>
              </div>
            </div>
          </div>
        </section>
      </div>
    </main>
  </div>

<div id="flash" style="position:fixed;right:16px;bottom:16px;padding:10px;background:#111;color:#fff;border-radius:8px;display:none"></div>

<script>
  const flash=(m)=>{const f=document.getElementById('flash');f.textContent=m;f.style.display='block';setTimeout(()=>f.style.display='none',1800);};

  function switchPanel(name){
    document.querySelectorAll('.panel').forEach(p=>p.style.display='none');
    document.getElementById('panel-'+name).style.display='block';
    document.querySelectorAll('.navlink').forEach(n=>n.classList.toggle('active', n.dataset.name===name));
    if(name==='dashboard'){ loadGroups(); loadSchedules(); initAddButtonsEditor(); }
    if(name==='users'){ loadUsers(); }
    if(name==='stats'){ reloadStats(); }
  }

  // ---------- Buttons editor (drag/drop + visual) ----------
  function initAddButtonsEditor(){
    const el=document.getElementById('add-buttons-editor');
    el.innerHTML='';
    addButtonToEditor('add-buttons-editor',{text:'',type:'url',value:''});
  }

  function addButtonToEditor(editorId, item=null){
    const container=document.getElementById(editorId);
    if(!container) return;
    const row=document.createElement('div');
    row.className='btn-row';
    row.draggable=true;

    row.ondragstart=(e)=>{ row.classList.add('dragging'); e.dataTransfer.setData('text/plain',''); row._dragging=true; };
    row.ondragend=(e)=>{ row.classList.remove('dragging'); row._dragging=false; };

    const txt=document.createElement('input'); txt.type='text'; txt.placeholder='Button text'; txt.className='txt'; txt.value = item?item.text||'':'';
    const sel=document.createElement('select'); sel.className='type'; sel.innerHTML='<option value="url">URL</option><option value="callback">Callback</option>'; sel.value=item?item.type||'url':'url';
    const val=document.createElement('input'); val.type='text'; val.placeholder='URL or callback_data'; val.className='val'; val.value = item?item.value||'':''; sel.onchange=()=>{ val.placeholder = sel.value==='url'?'https://...':'callback_data'; };

    const up=document.createElement('button'); up.textContent='↑'; up.className='small'; up.onclick=()=>{ const prev=row.previousElementSibling; if(prev) container.insertBefore(row, prev); };
    const down=document.createElement('button'); down.textContent='↓'; down.className='small'; down.onclick=()=>{ const next=row.nextElementSibling; if(next) container.insertBefore(next, row); };
    const rm=document.createElement('button'); rm.textContent='Remove'; rm.onclick=()=>{ row.remove(); };
    const preview=document.createElement('button'); preview.textContent='Preview'; preview.className='small'; preview.onclick=()=>previewEditorRow(row);

    row.appendChild(txt); row.appendChild(sel); row.appendChild(val); row.appendChild(preview); row.appendChild(up); row.appendChild(down); row.appendChild(rm);
    container.appendChild(row);
    attachDragHandlers(container);
  }

  function attachDragHandlers(container){
    // set dragover insertion logic
    container.querySelectorAll('.btn-row').forEach(r=>{
      r.ondragover=(e)=>{
        e.preventDefault();
        const dragging = container.querySelector('.dragging');
        if(!dragging || dragging===r) return;
        const rect = r.getBoundingClientRect();
        const offset = e.clientY - rect.top;
        if(offset > rect.height/2) r.after(dragging); else r.before(dragging);
      };
    });
  }

  function onDragOver(e){ e.preventDefault(); attachDragHandlers(e.currentTarget); }
  function onDrop(e){ e.preventDefault(); const c=e.currentTarget; c.querySelectorAll('.btn-row').forEach(r=>r.classList.remove('dragging')); }

  function getButtonsFromEditor(editorId){
    const container=document.getElementById(editorId);
    if(!container) return [];
    const out=[];
    container.querySelectorAll('.btn-row').forEach(r=>{
      const text = r.querySelector('.txt')?.value?.trim() || '';
      const type = r.querySelector('.type')?.value || 'url';
      const value = r.querySelector('.val')?.value?.trim() || '';
      if(!text) return;
      if(type==='url' && value) out.push({text:text,url:value});
      else if(type==='callback') out.push({text:text,callback_data:value||text});
    });
    return out;
  }

  function populateButtonsEditor(editorId, buttons){
    const container=document.getElementById(editorId);
    if(!container) return;
    container.innerHTML='';
    if(!buttons || !buttons.length){ addButtonToEditor(editorId,{text:'',type:'url',value:''}); return; }
    buttons.forEach(b=>{
      if(b.url) addButtonToEditor(editorId,{text:b.text||'',type:'url',value:b.url||''});
      else if(b.callback_data) addButtonToEditor(editorId,{text:b.text||'',type:'callback',value:b.callback_data||''});
      else addButtonToEditor(editorId,{text:b.text||'',type:'url',value:''});
    });
  }

  // preview a single row into preview area
  function previewEditorRow(row){
    const text = row.querySelector('.txt')?.value || '';
    const type = row.querySelector('.type')?.value || 'url';
    const value = row.querySelector('.val')?.value || '';
    const preview=document.getElementById('add-preview-area');
    preview.innerHTML = `<div style="padding:8px;border:1px solid #e2e8f0;border-radius:8px;background:#fff">
      <div style="font-weight:600;margin-bottom:6px">${escapeHtml(text)}</div>
      <div>${type==='url'?`URL: <a href="${escapeHtml(value)}" target="_blank">${escapeHtml(value)}</a>`:`Callback: ${escapeHtml(value)}`}</div>
    </div>`;
  }

  // preview whole editor (local render)
  function previewEditor(editorId, messageId){
    const buttons = getButtonsFromEditor(editorId);
    const msg = document.getElementById(messageId).value || '';
    const area = document.getElementById('add-preview-area');
    if(!buttons.length) area.innerHTML = `<div style="padding:8px;border:1px dashed #e2e8f0;border-radius:8px;background:#fff">${escapeHtml(msg)}</div>`;
    else {
      const rows = Math.ceil(buttons.length/2);
      let html = `<div style="padding:8px;border:1px solid #e2e8f0;border-radius:8px;background:#fff"><div style="font-weight:600;margin-bottom:6px">${escapeHtml(msg)}</div><div class="preview-grid">`;
      for(let i=0;i<buttons.length;i++){
        const b=buttons[i];
        html += `<div class="preview-button">${escapeHtml(b.text)}<div style="font-size:12px;color:#666;margin-top:6px">${b.url?escapeHtml(b.url):escapeHtml(b.callback_data||'')}</div></div>`;
      }
      html += `</div></div>`;
      area.innerHTML = html;
    }
  }

  // ---------- Groups CRUD + preview send ----------
  async function loadGroups(){
    const r=await fetch('/api/groups'); const j=await r.json(); const el=document.getElementById('groups');
    window._loaded_groups = j || [];
    if(!j.length){ el.innerHTML='<div class="muted">No groups yet.</div>'; return; }
    el.innerHTML = '<table><thead><tr><th>#</th><th>Image</th><th>Message</th><th></th></tr></thead><tbody>' + j.map((g,i)=>`
      <tr>
        <td>${i+1}</td>
        <td>${g.image?`<a href="/media/${encodeURIComponent(g.image)}" target="_blank"><img src="/media/${encodeURIComponent(g.image)}" style="width:120px;height:64px;object-fit:cover;border-radius:8px"/></a>`:'<span class="muted">None</span>'}</td>
        <td><div style="font-weight:600">${escapeHtml(g.message||'')}</div>
            ${(g.buttons && g.buttons.length)?('<div style="margin-top:6px;">'+g.buttons.map(b=>`<span class="pill" style="margin-right:6px">${escapeHtml(b.text)}</span>`).join('')+'</div>'):''
        }</td>
        <td><div style="display:flex;gap:6px;flex-direction:column"><button onclick="startEdit(${i})">Edit</button><button onclick="delGroup(${i})" style="background:#dc2626;color:#fff">Delete</button></div></td>
      </tr>`).join('') + '</tbody></table>';
  }

  function startEdit(i){
    const group = window._loaded_groups && window._loaded_groups[i];
    if(!group) return;
    const cell = document.querySelector(`#groups table tbody tr:nth-child(${i+1}) td:nth-child(3)`);
    if(!cell) return;
    cell.dataset.original = group.message || '';
    cell.innerHTML = `<textarea id="edit-msg-${i}" rows="4" style="width:100%">${escapeHtml(group.message||'')}</textarea>
      <div style="margin-top:8px">
        <div id="edit-buttons-editor-${i}" class="buttons-editor" ondragover="onDragOver(event)" ondrop="onDrop(event)"></div>
        <div style="margin-top:6px" class="inline">
          <button onclick="addButtonToEditor('edit-buttons-editor-${i}')" class="small">+ Add Button</button>
          <button onclick="previewEditor('edit-buttons-editor-${i}','edit-msg-${i}')" class="small">Preview</button>
          <input id="edit_preview_chat_${i}" placeholder="Test chat_id" style="width:140px;padding:6px;border-radius:6px"/>
          <button onclick="sendPreview('edit-buttons-editor-${i}','edit-msg-${i}', ${i})" class="small">Send Preview</button>
        </div>
      </div>
      <div style="margin-top:8px" class="inline"><button onclick="saveEdit(${i})">Save</button><button onclick="cancelEdit(${i})" class="small">Cancel</button></div>`;
    populateButtonsEditor(`edit-buttons-editor-${i}`, group.buttons || []);
  }

  async function saveEdit(i){
    const msg = document.getElementById(`edit-msg-${i}`).value;
    const buttons = getButtonsFromEditor(`edit-buttons-editor-${i}`);
    const payload = { message: msg, buttons: buttons };
    const r=await fetch(`/api/groups/${i}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    if(r.ok){ flash('Updated'); loadGroups(); } else { flash('Update failed'); }
  }

  function cancelEdit(i){
    loadGroups();
  }

  async function addGroup(){
    const fd=new FormData();
    const f=document.getElementById('image').files[0]; if(f) fd.append('file', f);
    const msg=document.getElementById('message').value.trim(); if(!msg){ flash('Message required'); return; }
    fd.append('message', msg);
    const buttons = getButtonsFromEditor('add-buttons-editor');
    if(buttons && buttons.length) fd.append('buttons', JSON.stringify(buttons));
    const r=await fetch('/api/groups', {method:'POST', body:fd});
    if(r.ok){ flash('Added'); document.getElementById('message').value=''; initAddButtonsEditor(); loadGroups(); } else { flash('Add failed'); }
  }

  async function delGroup(idx){
    if(!confirm('Delete this group?')) return;
    const r=await fetch(`/api/groups/${idx}`,{method:'DELETE'});
    if(r.ok){ flash('Deleted'); loadGroups(); } else { flash('Failed'); }
  }

  async function sendNowRandom(){ const r=await fetch('/api/send-now',{method:'POST'}); flash(r.ok?'Sent':'Failed'); }

  // send preview to a chat_id (editorId, messageId, optional group index)
  async function sendPreview(editorId, messageId, groupIndex=null){
    const chat_id = groupIndex===null ? document.getElementById('preview_chat_id').value.trim() : document.getElementById(`edit_preview_chat_${groupIndex}`).value.trim();
    if(!chat_id){ flash('Please enter test chat_id'); return; }
    const payload = {
      chat_id: chat_id,
      message: document.getElementById(messageId).value || '',
      buttons: getButtonsFromEditor(editorId)
    };
    const r = await fetch('/api/preview-send', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    if(r.ok) flash('Preview sent'); else flash('Preview failed');
  }

  // ---------- Schedules ----------
  async function loadSchedules(){
    const r=await fetch('/api/schedules'); const j=await r.json(); const el=document.getElementById('schedules');
    if(!j.length){ el.innerHTML='<div class="muted">No schedules</div>'; return; }
    el.innerHTML = '<table><tbody>' + j.map(s=>`<tr><td>${String(s.hour).padStart(2,'0')}:${String(s.minute).padStart(2,'0')}</td><td><button onclick="delSchedule(${s.hour},${s.minute})" style="background:#dc2626;color:#fff">Delete</button></td></tr>`).join('') + '</tbody></table>';
  }
  async function addSchedule(){ const h=+document.getElementById('h').value, m=+document.getElementById('m').value; const r=await fetch('/api/schedules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hour:h,minute:m})}); if(r.ok){ flash('Added'); loadSchedules(); } else { flash('Failed'); } }
  async function delSchedule(h,m){ const r=await fetch(`/api/schedules/${h}/${m}`,{method:'DELETE'}); if(r.ok){ flash('Deleted'); loadSchedules(); } else { flash('Failed'); } }

  // ---------- Users ----------
  let _users=[];
  async function loadUsers(){ const r=await fetch('/api/users'); if(!r.ok){ document.getElementById('usersTable').innerHTML='<div class="muted">Unauthorized</div>'; return; } _users=await r.json(); document.getElementById('userCount').textContent=_users.length; renderUsers(); }
  function renderUsers(){ const q=(document.getElementById('userSearch')||{}).value||''; const data=_users.filter(u=>!q||String(u.chat_id).includes(q)); if(!data.length){ document.getElementById('usersTable').innerHTML='<div class="muted">No users</div>'; return; } let html='<table><thead><tr><th>#</th><th>chat_id</th><th>subscribed</th><th></th></tr></thead><tbody>'; html+=data.map((u,i)=>`<tr><td>${i+1}</td><td>${u.chat_id}</td><td>${(u.created_at_local||'')}</td><td><button onclick="delUser(${u.chat_id})" style="background:#dc2626;color:#fff">Remove</button></td></tr>`).join(''); html+='</tbody></table>'; document.getElementById('usersTable').innerHTML=html; }
  async function delUser(chat_id){ if(!confirm('Remove this user?')) return; const r=await fetch(`/api/users/${chat_id}`,{method:'DELETE'}); if(r.ok){ flash('Removed'); loadUsers(); } else flash('Failed'); }
  function exportUsersCSV(){ const rows=[['chat_id','created_at_utc','created_at_local','tz']].concat(_users.map(u=>[u.chat_id,u.created_at||'',u.created_at_local||'',u.tz||''])); const csv=rows.map(r=>r.map(c=>`"${String(c).replace(/"/g,'""')}"`).join(',')).join('\n'); const blob=new Blob([csv],{type:'text/csv'}); const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='users.csv'; a.click(); URL.revokeObjectURL(a.href); }

  // ---------- Stats ----------
  async function reloadStats(){
    const r=await fetch('/api/stats'); if(!r.ok){ document.getElementById('stats-top').innerHTML='Error'; return; }
    const j=await r.json();
    const top = j.counts || {};
    const records = j.records || [];
    let topHtml = '<table><thead><tr><th>callback_data</th><th>count</th></tr></thead><tbody>';
    Object.entries(top).sort((a,b)=>b[1]-a[1]).slice(0,50).forEach(([k,v])=>{ topHtml += `<tr><td style="max-width:320px;overflow:hidden;text-overflow:ellipsis">${escapeHtml(k)}</td><td>${v}</td></tr>`; });
    topHtml += '</tbody></table>';
    document.getElementById('stats-top').innerHTML = topHtml;
    let recHtml = '<table><thead><tr><th>ts</th><th>user</th><th>callback</th></tr></thead><tbody>';
    records.slice(0,200).forEach(r=>{ recHtml += `<tr><td>${escapeHtml(r.ts||'')}</td><td>${escapeHtml(String(r.user_id||'') + (r.username?(' / @'+r.username):''))}</td><td>${escapeHtml(r.callback_data||'')}</td></tr>`; });
    recHtml += '</tbody></table>';
    document.getElementById('stats-records').innerHTML = recHtml;
  }
  async function resetStats(){ if(!confirm('Clear all stats?')) return; const r=await fetch('/api/stats/reset',{method:'POST'}); if(r.ok){ flash('Cleared'); reloadStats(); } else flash('Failed'); }

  // ---------- misc ----------
  function escapeHtml(s){ return (s||'').toString().replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'); }

  // init default
  switchPanel('dashboard');
</script>
</body>
</html>
'''

LOGIN_HTML = r'''
<!DOCTYPE html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>Login</title>
<style>body{font-family:system-ui,Segoe UI,Roboto,Arial;margin:0;background:#081226;color:#eef2ff;display:flex;align-items:center;justify-content:center;height:100vh}form{width:360px;background:#0f1b33;padding:18px;border-radius:12px;border:1px solid #20314d}input,button{width:100%;padding:10px;border-radius:8px;border:1px solid #334770;background:#0f1a2d;color:#eaf0ff}button{margin-top:12px;background:#2546f2;border-color:#2546f2}label{font-size:12px;opacity:.85} .err{color:#ff8f8f;margin-top:8px}</style>
</head><body><form method="post" action="/login"><h3 style="margin:0 0 12px">Admin Login</h3><label>Username</label><input name="username" autocomplete="username" required/><label>Password</label><input name="password" type="password" autocomplete="current-password" required/><button>Login</button><div class="err">%ERR%</div></form></body></html>
'''

# ---------------- Lifespan ----------------
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
    telegram_app.add_handler(CallbackQueryHandler(on_callback_query))

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

# ---------------- App & Routes ----------------
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

# ---- APIs ----
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
    buttons: Optional[str] = Form(None),
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

    parsed_buttons = []
    if buttons:
        try:
            data = json.loads(buttons)
            if isinstance(data, list):
                for b in data:
                    if isinstance(b, dict) and b.get("text"):
                        parsed_buttons.append(b)
        except Exception:
            for line in buttons.splitlines():
                line = line.strip()
                if not line:
                    continue
                if "|" in line:
                    text, url = line.split("|", 1)
                    parsed_buttons.append({"text": text.strip(), "url": url.strip()})
                else:
                    parsed_buttons.append({"text": line.strip(), "callback_data": line.strip()})

    group_manager.add(filename, message, buttons=parsed_buttons)
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
        buttons = payload.get("buttons")
        if isinstance(buttons, str):
            try:
                buttons = json.loads(buttons)
            except Exception:
                buttons = None
        if msg is None and img is None and buttons is None:
            raise HTTPException(status_code=400, detail="Nothing to update")
        group_manager.update(idx, message=msg, image=img, buttons=buttons)
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

# preview send: sends message + inline buttons to a given chat_id (admin-only)
@app.post("/api/preview-send")
async def api_preview_send(payload: dict, request: Request, x_admin_key: Optional[str] = Header(None)):
    require_admin_access(request, x_admin_key)
    chat_id = payload.get("chat_id")
    message = payload.get("message", "")
    buttons = payload.get("buttons", []) or []
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id required")
    # build reply_markup
    reply_markup = None
    kb_rows = []
    for b in buttons:
        if b.get("url"):
            kb_rows.append([InlineKeyboardButton(text=b["text"], url=b["url"])])
        elif b.get("callback_data"):
            kb_rows.append([InlineKeyboardButton(text=b["text"], callback_data=b["callback_data"])])
    if kb_rows:
        reply_markup = InlineKeyboardMarkup(kb_rows)
    # attempt to send (bot must be able to send to that chat)
    try:
        # send as text
        await telegram_app.bot.send_message(chat_id=chat_id, text=message or "Preview", reply_markup=reply_markup)
        return {"ok": True}
    except Exception as e:
        logger.error(f"preview send failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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

# stats endpoints
@app.get("/api/stats")
async def api_get_stats(request: Request):
    if not is_logged_in(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    stats = _load_stats()
    return stats

@app.post("/api/stats/reset")
async def api_reset_stats(request: Request, x_admin_key: Optional[str] = Header(None)):
    require_admin_access(request, x_admin_key)
    s = {"counts": {}, "records": []}
    _save_stats(s)
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT","8000")), reload=False)
