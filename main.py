# -*- coding: utf-8 -*-
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
from telegram import Update

# --- Config & DB ---
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

# ---------------- Managers ----------------
class UserManagerDB:
    def add(self, chat_id: int):
        with SessionLocal() as db:
            exists = db.scalar(select(User).where(User.chat_id == chat_id))
            if not exists:
                db.add(User(chat_id=int(chat_id)))  # models.py 默认 UTC now()
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

# ---------------- Globals ----------------
user_manager     = UserManagerDB()
group_manager    = MessageGroupManager(GROUPS_FILE)
schedule_manager = ScheduleManager(SCHEDULES_FILE, SCHEDULES_DEFAULT)

telegram_app = None
scheduler    = AsyncIOScheduler(timezone=TZ)

# ---------------- Custom Emoji（HTML 标签法） ----------------
# 允许：<ce:123>, ＜ce：123＞, < ce : 123 > 等写法
CE_PATTERN = re.compile(r"[<＜]\s*ce\s*[:：]\s*(\d+)\s*[>＞]", re.IGNORECASE)

def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def render_text_with_ce(src: str):
    """
    把占位符替换为 <tg-emoji emoji-id="...">🙂</tg-emoji> ，并返回 (text, parse_mode)
    如果没有占位符，返回原文与 None。
    """
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

# 提取一条消息中的 custom_emoji_id（支持回复别人的消息）
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

# 自测：/ce_test <id>
async def cmd_ce_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/ce_test <custom_emoji_id>")
        return
    ceid = context.args[0].strip()
    html = f'测试 <tg-emoji emoji-id="{ceid}">🙂</tg-emoji> OK'
    await update.message.reply_text(html, parse_mode="HTML")

# ---------------- Core sending ----------------
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

    text, parse_mode = render_text_with_ce(message)

    for uid in user_manager.all_chat_ids():
        try:
            if image:
                with open(image, "rb") as fp:
                    await telegram_app.bot.send_photo(
                        chat_id=uid,
                        photo=fp,
                        caption=text,
                        parse_mode=parse_mode  # 只有用到 CE 才是 "HTML"
                    )
            else:
                await telegram_app.bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode=parse_mode
                )
            logger.info(f"✅ 已发送给 {uid}")
        except Exception as e:
            logger.error(f"发送给 {uid} 失败: {e}")

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

# ---------------- Admin HTML（亮色 + 缩略图 + 在线编辑 + 保留换行） ----------------
ADMIN_HTML = r'''
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Daily Sender Admin</title>
  <style>
    :root{ --bg:#f7fafc; --panel:#ffffff; --line:#e5e7eb; --line2:#e5e7eb;
           --text:#0f172a; --muted:#64748b; --accent:#2563eb; --danger:#dc2626; --ok:#16a34a; }
    *{ box-sizing:border-box; } html,body{ height:100%; }
    body{ font-family: system-ui,-apple-system,Segoe UI,Roboto,Arial; margin:0; background:var(--bg); color:var(--text); }
    a{ color:var(--accent); text-decoration:none; } a:hover{ text-decoration:underline; }
    header{ padding:16px 20px; background:#fff; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:12px; position:sticky; top:0; z-index:5; }
    h1{ margin:0; font-size:18px; font-weight:600; }
    .layout{ max-width:1200px; margin:18px auto; padding:0 16px; display:grid; grid-template-columns:230px 1fr; gap:16px; }
    aside{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:10px; height:fit-content; position:sticky; top:90px; }
    .navlink{ display:block; width:100%; text-align:left; border:1px solid var(--line); background:#fff; color:var(--text); padding:10px 12px; border-radius:10px; margin:6px 0; cursor:pointer; }
    .navlink:hover{ background:#f3f6fb; } .navlink.active{ background:var(--accent); border-color:var(--accent); color:#fff; }
    main{ display:block; }
    section{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:16px; margin-bottom:18px; box-shadow:0 1px 2px rgba(0,0,0,.03); }
    h2{ margin:6px 0 14px; font-size:16px; font-weight:600; }
    .row{ display:flex; gap:12px; flex-wrap:wrap; }
    input,textarea,select,button{ font:inherit; padding:10px 12px; border-radius:10px; border:1px solid #cbd5e1; background:#fff; color:var(--text); }
    textarea{ resize:vertical; }
    button{ cursor:pointer; background:var(--accent); border-color:var(--accent); color:#fff; line-height:1.2; }
    button:hover{ filter:brightness(.97); } button:disabled{ opacity:.6; cursor:not-allowed; }
    .ghost{ background:#fff; color:var(--text); border-color:#cbd5e1; } .ghost:hover{ background:#f3f6fb; }
    .danger{ background:var(--danger); border-color:var(--danger); } .success{ background:var(--ok); border-color:var(--ok); }
    label{ font-size:12px; color:var(--muted); display:block; margin-bottom:6px; }
    .grid{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }
    .muted{ font-size:13px; color:var(--muted); }
    .pill{ font-size:12px; padding:2px 8px; background:#eef2ff; color:#324aa8; border-radius:999px; border:1px solid #dbe4ff; }
    .inline{ display:inline-flex; gap:8px; align-items:center; }
    .spacer{ flex:1; }
    #flash{ position:fixed; right:16px; bottom:16px; background:#111827; color:#fff; border:1px solid #0b1220; padding:10px 12px; border-radius:12px; display:none; box-shadow:0 8px 24px rgba(0,0,0,.15); }
    .panel{ display:none; } .panel.active{ display:block; }
    .stat{ display:inline-block; padding:8px 10px; border:1px solid var(--line); background:#fff; border-radius:10px; margin-right:8px; }
    .searchbar{ display:flex; gap:8px; align-items:center; margin:8px 0 12px; }
    .mono{ font-family: ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace; }

    table{ width:100%; border-collapse:separate; border-spacing:0; table-layout:auto; background:#fff; border-radius:12px; overflow:hidden; }
    thead th{ background:#f8fafc; }
    th,td{ border-bottom:1px solid var(--line2); padding:12px 10px; vertical-align:middle; text-align:left; }
    th:nth-child(1),td:nth-child(1){ width:56px; }
    th:nth-child(2),td:nth-child(2){ width:320px; }
    th:nth-child(4),td:nth-child(4){ width:200px; }
    th:nth-child(3),td:nth-child(3){ min-width:320px; }
    td.actions{ text-align:right; white-space:nowrap; }
    td .link{ display:inline-block; max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

    .thumbWrap{ display:flex; flex-direction:column; gap:6px; max-width:300px; }
    .fileRow{ display:flex; align-items:center; gap:8px; }
    .thumb{ width:300px; height:160px; border:1px solid var(--line); border-radius:10px; background:#f8fafc; display:flex; align-items:center; justify-content:center; overflow:hidden; }
    .thumb img{ max-width:100%; max-height:100%; object-fit:contain; display:block; }

    .msgCell pre.msg{
      white-space: pre-wrap; word-break: break-word; overflow-wrap: anywhere;
      font-family: inherit; line-height: 1.55; margin: 0;
    }

    @media (max-width:880px){
      .grid{ grid-template-columns:1fr; }
      .layout{ grid-template-columns:1fr; }
      aside{ position:static; }
      .thumb{ width:100%; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Daily Sender Admin</h1>
    <div class="spacer"></div>
    <form action="/logout" method="post"><button class="ghost">Logout</button></form>
  </header>

  <div class="layout">
    <aside>
      <button class="navlink active" data-name="dashboard" onclick="switchPanel('dashboard')">Dashboard</button>
      <button class="navlink" data-name="users" onclick="switchPanel('users')">Users</button>
    </aside>

    <main>
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

      <div id="panel-users" class="panel">
        <section>
          <h2>Subscribed Users</h2>
          <div class="inline" style="margin-bottom:8px;">
            <span class="stat">Total: <b id="userCount">0</b></span>
            <button class="ghost" onclick="loadUsers()">Refresh</button>
            <button class="ghost" onclick="exportUsersCSV()">Export CSV</button>
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
  const flash=(m)=>{const f=document.getElementById('flash');f.textContent=m;f.style.display='block';setTimeout(()=>f.style.display='none',1500);};
  function switchPanel(name){
    document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
    document.getElementById('panel-'+name).classList.add('active');
    document.querySelectorAll('.navlink').forEach(a=>a.classList.toggle('active', a.dataset.name===name));
    if(name==='dashboard'){ loadGroups(); loadSchedules(); }
    if(name==='users'){ loadUsers(); }
  }
  const escapeHtml=(s)=>(s||"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
  const fmtLocalShort=(s)=> s ? s.substring(0,16).replace('T',' ') : '';

  async function loadGroups(){
    const r=await fetch('/api/groups'); const j=await r.json(); const el=document.getElementById('groups');
    if(!j.length){ el.innerHTML='<p class="muted">No groups yet.</p>'; return; }
    el.innerHTML = `<table>
      <thead><tr><th class="mono">#</th><th>Image</th><th>Message</th><th></th></tr></thead>
      <tbody>
      ${j.map((g,i)=>`
        <tr>
          <td class="mono">${i+1}</td>
          <td>
            ${g.image ? `
              <div class="thumbWrap">
                <a href="/media/${encodeURIComponent(g.image)}" target="_blank" title="${escapeHtml(g.image)}">
                  <div class="thumb"><img src="/media/${encodeURIComponent(g.image)}" alt="img" loading="lazy"/></div>
                </a>
                <div class="fileRow"><span class="pill">IMG</span>
                  <a class="link" target="_blank" href="/media/${encodeURIComponent(g.image)}">${escapeHtml(g.image)}</a>
                </div>
              </div>` : '<span class="muted">None</span>'}
          </td>
          <td class="msgCell" id="msg-${i}"><pre class="msg">${escapeHtml(g.message||'')}</pre></td>
          <td class="actions">
            <button class="ghost" onclick="startEdit(${i})">Edit</button>
            <button class="danger" onclick="delGroup(${i})">Delete</button>
          </td>
        </tr>`).join('')}
      </tbody></table>`;
  }
  function startEdit(i){
    const cell=document.getElementById('msg-'+i); if(!cell) return;
    const original=cell.textContent; cell.dataset.original=original;
    cell.innerHTML = `
      <textarea id="edit-${i}" rows="8" style="width:100%;"></textarea>
      <div class="inline" style="margin-top:8px;">
        <button onclick="saveEdit(${i})">Save</button>
        <button class="ghost" onclick="cancelEdit(${i})">Cancel</button>
      </div>`;
    document.getElementById('edit-'+i).value=original;
  }
  async function saveEdit(i){
    const ta=document.getElementById('edit-'+i); if(!ta) return;
    const r=await fetch('/api/groups/'+i,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:ta.value})});
    if(r.ok){ flash('Updated'); loadGroups(); } else { flash('Update failed'); }
  }
  function cancelEdit(i){
    const cell=document.getElementById('msg-'+i); const original=cell?.dataset.original||''; cell.innerHTML=`<pre class="msg">${escapeHtml(original)}</pre>`;
  }
  async function addGroup(){
    const fd=new FormData(); const f=document.getElementById('image').files[0]; if(f) fd.append('file',f);
    const msg=document.getElementById('message').value.trim(); if(!msg){ flash('Message required'); return; }
    fd.append('message',msg);
    const r=await fetch('/api/groups',{method:'POST',body:fd});
    if(r.ok){ flash('Added'); document.getElementById('message').value=''; document.getElementById('image').value=''; loadGroups(); }
    else{ flash('Add failed'); }
  }
  async function delGroup(idx){
    if(!confirm('Delete this group?')) return;
    const r=await fetch('/api/groups/'+idx,{method:'DELETE'}); if(r.ok){ flash('Deleted'); loadGroups(); } else { flash('Failed'); }
  }

  async function loadSchedules(){
    const r=await fetch('/api/schedules'); const j=await r.json(); const el=document.getElementById('schedules');
    if(!j.length){ el.innerHTML='<p class="muted">No schedules. Add one above.</p>'; return; }
    el.innerHTML = `<table>
      <thead><tr><th>Time</th><th></th></tr></thead>
      <tbody>${j.map(s=>`<tr>
        <td class="mono">${String(s.hour).padStart(2,'0')}:${String(s.minute).padStart(2,'0')}</td>
        <td class="actions"><button class="danger" onclick="delSchedule(${s.hour},${s.minute})">Delete</button></td>
      </tr>`).join('')}</tbody></table>`;
  }
  async function addSchedule(){
    const h=+document.getElementById('h').value, m=+document.getElementById('m').value;
    const r=await fetch('/api/schedules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hour:h,minute:m})});
    if(r.ok){ flash('Added'); reloadJobs(); } else { flash('Failed'); }
  }
  async function delSchedule(h,m){
    const r=await fetch(`/api/schedules/${h}/${m}`,{method:'DELETE'}); if(r.ok){ flash('Deleted'); reloadJobs(); } else { flash('Failed'); }
  }
  async function reloadJobs(){ const r=await fetch('/api/reload',{method:'POST'}); flash(r.ok?'Reloaded':'Failed'); loadSchedules(); }
  async function sendNowRandom(){ const r=await fetch('/api/send-now',{method:'POST'}); flash(r.ok?'Sent':'Failed'); }

  let _users=[];
  async function loadUsers(){
    const r=await fetch('/api/users'); const el=document.getElementById('usersTable');
    if(!r.ok){ el.innerHTML='<p class="muted">Unauthorized</p>'; return; }
    _users=await r.json(); document.getElementById('userCount').textContent=_users.length; renderUsers();
  }
  function renderUsers(){
    const q=(document.getElementById('userSearch').value||'').trim();
    const data=_users.filter(u=>!q||String(u.chat_id).includes(q));
    const el=document.getElementById('usersTable');
    if(!data.length){ el.innerHTML='<p class="muted">No users.</p>'; return; }
    const tz=(data[0]&&data[0].tz)?data[0].tz:'Local';
    el.innerHTML=`<table><thead><tr><th>#</th><th>chat_id</th><th>Subscribed (${tz})</th><th></th></tr></thead>
      <tbody>${data.map((u,i)=>`<tr>
        <td class="mono">${i+1}</td><td class="mono">${u.chat_id}</td>
        <td class="mono">${fmtLocalShort(u.created_at_local)}</td>
        <td class="actions"><button class="danger" onclick="delUser(${u.chat_id})">Remove</button></td>
      </tr>`).join('')}</tbody></table>`;
  }
  async function delUser(chat_id){
    if(!confirm('Remove this user?')) return;
    const r=await fetch('/api/users/'+chat_id,{method:'DELETE'}); if(r.ok){ flash('Removed'); loadUsers(); } else { flash('Failed'); }
  }
  function exportUsersCSV(){
    const rows=[['chat_id','created_at_utc','created_at_local(short)','tz']]
      .concat(_users.map(u=>[u.chat_id,u.created_at||'',fmtLocalShort(u.created_at_local)||'',u.tz||'']));
    const csv=rows.map(r=>r.map(x=>`"${String(x).replaceAll('"','""')}"`).join(',')).join('\n');
    const blob=new Blob([csv],{type:'text/csv;charset=utf-8;'}); const url=URL.createObjectURL(blob);
    const a=document.createElement('a'); a.href=url; a.download='subscribed_users.csv'; a.click(); URL.revokeObjectURL(url);
  }

  switchPanel('dashboard');
</script>
</body>
</html>
'''

LOGIN_HTML = r'''
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Login</title>
  <style>
    body{ font-family: system-ui,-apple-system,Segoe UI,Roboto,Arial; margin:0; background:#0b1320; color:#eef2ff; display:flex; align-items:center; justify-content:center; min-height:100vh; }
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
    <label>Username</label><input name="username" autocomplete="username" required />
    <label>Password</label><input name="password" type="password" autocomplete="current-password" required />
    <button>Login</button>
    <div class="err">%ERR%</div>
  </form>
</body>
</html>
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
