import os, time, json, hmac, hashlib, threading, sqlite3
from contextlib import contextmanager
from urllib.parse import parse_qsl

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Body
from fastapi.responses import FileResponse
import telebot
from telebot import types

# ================== SOZLAMALAR ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ACCESS_CODE = os.getenv("ACCESS_CODE", "1111")  # brauzer-ilova uchun umumiy kod
DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "ombor.db")
HERE = os.path.dirname(__file__)


# ================== BAZA ==================
@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            tg_id INTEGER PRIMARY KEY,
            role TEXT NOT NULL DEFAULT 'xodim',
            added_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            qty INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS ops(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pid INTEGER NOT NULL,
            type TEXT NOT NULL,
            qty INTEGER NOT NULL,
            ts INTEGER NOT NULL,
            FOREIGN KEY(pid) REFERENCES products(id) ON DELETE CASCADE);
        """)
    admin_id = os.getenv("ADMIN_ID")
    if admin_id:
        try:
            add_user(int(admin_id), "admin")
        except Exception:
            pass


def add_user(tg_id, role="xodim"):
    with conn() as c:
        c.execute("INSERT INTO users(tg_id,role,added_at) VALUES(?,?,?) "
                  "ON CONFLICT(tg_id) DO UPDATE SET role=excluded.role",
                  (tg_id, role, int(time.time())))


def remove_user(tg_id):
    with conn() as c:
        c.execute("DELETE FROM users WHERE tg_id=? AND role!='admin'", (tg_id,))


def get_role(tg_id):
    with conn() as c:
        r = c.execute("SELECT role FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        return r["role"] if r else None


def list_users():
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT tg_id,role,added_at FROM users ORDER BY role,added_at").fetchall()]


def list_products():
    with conn() as c:
        out = []
        for r in c.execute("SELECT id,name,qty FROM products ORDER BY name").fetchall():
            d = dict(r)
            d["ops_count"] = c.execute("SELECT COUNT(*) n FROM ops WHERE pid=?",
                                       (r["id"],)).fetchone()["n"]
            out.append(d)
        return out


def add_product(name, qty):
    with conn() as c:
        pid = c.execute("INSERT INTO products(name,qty) VALUES(?,?)", (name, qty)).lastrowid
        if qty > 0:
            c.execute("INSERT INTO ops(pid,type,qty,ts) VALUES(?,'in',?,?)",
                      (pid, qty, int(time.time())))
        return pid


def do_op(pid, type_, qty):
    with conn() as c:
        p = c.execute("SELECT qty FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            raise ValueError("Mahsulot topilmadi")
        if type_ == "out" and qty > p["qty"]:
            raise ValueError(f"Qoldiq yetarli emas. Bor: {p['qty']}")
        new_qty = p["qty"] + (qty if type_ == "in" else -qty)
        c.execute("UPDATE products SET qty=? WHERE id=?", (new_qty, pid))
        c.execute("INSERT INTO ops(pid,type,qty,ts) VALUES(?,?,?,?)",
                  (pid, type_, qty, int(time.time())))
        return new_qty


def product_history(pid):
    with conn() as c:
        p = c.execute("SELECT id,name,qty FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            return None
        ops = c.execute("SELECT type,qty,ts FROM ops WHERE pid=? ORDER BY ts DESC",
                        (pid,)).fetchall()
        return {"product": dict(p), "ops": [dict(o) for o in ops]}


# ================== AUTH (Telegram initData) ==================
def verify_init_data(init_data):
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", None)
        if not got:
            return None
        check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        return json.loads(pairs.get("user", "{}"))
    except Exception:
        return None


def current_user(init_data, code=""):
    # 1) Telegram ichidan ochilgan bo'lsa — initData bilan
    u = verify_init_data(init_data)
    if u:
        role = get_role(u["id"])
        if not role:
            raise HTTPException(403, "Sizga ruxsat yo'q. Admin sizni qo'shishi kerak.")
        return {"id": u["id"], "role": role}
    # 2) Brauzerdan ochilgan bo'lsa — umumiy kod bilan (xodim huquqi)
    if code and code == ACCESS_CODE:
        return {"id": 0, "role": "xodim"}
    raise HTTPException(401, "Kirish kodi noto'g'ri yoki botdan qayta oching.")


# ================== API ==================
app = FastAPI(title="Ombor")


@app.get("/api/me")
def me(x: str = Header(None, alias="X-Init-Data"),
       code: str = Header(None, alias="X-Access-Code")):
    return current_user(x or "", code or "")


@app.get("/api/products")
def products(x: str = Header(None, alias="X-Init-Data"),
       code: str = Header(None, alias="X-Access-Code")):
    current_user(x or "", code or "")
    return list_products()


@app.post("/api/products")
def create_product(body: dict = Body(...), x: str = Header(None, alias="X-Init-Data"),
       code: str = Header(None, alias="X-Access-Code")):
    current_user(x or "", code or "")
    name = (body.get("name") or "").strip()
    qty = int(body.get("qty") or 0)
    if not name:
        raise HTTPException(400, "Nomi bo'sh")
    return {"id": add_product(name, qty)}


@app.post("/api/products/{pid}/op")
def op(pid: int, body: dict = Body(...), x: str = Header(None, alias="X-Init-Data"),
       code: str = Header(None, alias="X-Access-Code")):
    current_user(x or "", code or "")
    t = body.get("type")
    qty = int(body.get("qty") or 0)
    if t not in ("in", "out") or qty <= 0:
        raise HTTPException(400, "Noto'g'ri amal")
    try:
        return {"qty": do_op(pid, t, qty)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/products/{pid}/history")
def history(pid: int, x: str = Header(None, alias="X-Init-Data"),
       code: str = Header(None, alias="X-Access-Code")):
    current_user(x or "", code or "")
    h = product_history(pid)
    if not h:
        raise HTTPException(404, "Topilmadi")
    return h


@app.get("/api/users")
def users(x: str = Header(None, alias="X-Init-Data"),
       code: str = Header(None, alias="X-Access-Code")):
    u = current_user(x or "", code or "")
    if u["role"] != "admin":
        raise HTTPException(403, "Faqat admin")
    return list_users()


@app.post("/api/users")
def add_user_api(body: dict = Body(...), x: str = Header(None, alias="X-Init-Data"),
       code: str = Header(None, alias="X-Access-Code")):
    u = current_user(x or "", code or "")
    if u["role"] != "admin":
        raise HTTPException(403, "Faqat admin")
    tid = int(body.get("tg_id") or 0)
    role = body.get("role") or "xodim"
    if tid <= 0 or role not in ("xodim", "admin"):
        raise HTTPException(400, "Noto'g'ri ID yoki rol")
    add_user(tid, role)
    return {"ok": True}


@app.delete("/api/users/{tg_id}")
def del_user_api(tg_id: int, x: str = Header(None, alias="X-Init-Data"),
       code: str = Header(None, alias="X-Access-Code")):
    u = current_user(x or "", code or "")
    if u["role"] != "admin":
        raise HTTPException(403, "Faqat admin")
    if tg_id == u["id"]:
        raise HTTPException(400, "O'zingizni o'chira olmaysiz")
    remove_user(tg_id)
    return {"ok": True}


# --- static (papkasiz) ---
@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "index.html"))


@app.get("/manifest.json")
def manifest():
    return FileResponse(os.path.join(HERE, "manifest.json"))


@app.get("/sw.js")
def sw():
    return FileResponse(os.path.join(HERE, "sw.js"), media_type="application/javascript")


@app.get("/icon-192.png")
def i192():
    return FileResponse(os.path.join(HERE, "icon-192.png"))


@app.get("/icon-512.png")
def i512():
    return FileResponse(os.path.join(HERE, "icon-512.png"))


# ================== BOT ==================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML") if BOT_TOKEN else None


def _pid(m):
    p = m.text.split()
    return int(p[1]) if len(p) > 1 and p[1].lstrip("-").isdigit() else None


if bot:
    @bot.message_handler(commands=["start"])
    def _start(m):
        uid = m.from_user.id
        if not get_role(uid):
            bot.reply_to(m, f"Salom! Sizga hali ruxsat yo'q.\nID: <code>{uid}</code>\n"
                            f"Bu ID ni adminga yuboring.")
            return
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("📦 Omborni ochish",
               web_app=types.WebAppInfo(url=WEBAPP_URL)))
        extra = "\n\nSiz adminsiz. /help" if get_role(uid) == "admin" else ""
        bot.send_message(m.chat.id, f"Ombor tayyor 👇{extra}", reply_markup=kb)

    @bot.message_handler(commands=["id"])
    def _id(m):
        bot.reply_to(m, f"ID: <code>{m.from_user.id}</code>")

    @bot.message_handler(commands=["help"])
    def _help(m):
        if get_role(m.from_user.id) != "admin":
            bot.reply_to(m, "Ochish uchun /start")
            return
        bot.reply_to(m, "<b>Admin:</b>\n/add_xodim ID\n/add_admin ID\n"
                        "/remove ID\n/xodimlar\n\nXodim /id yozib ID sini olsin.")

    @bot.message_handler(commands=["add_xodim"])
    def _ax(m):
        if get_role(m.from_user.id) != "admin":
            return
        t = _pid(m)
        if not t:
            bot.reply_to(m, "Format: /add_xodim ID"); return
        add_user(t, "xodim"); bot.reply_to(m, f"✅ Xodim: <code>{t}</code>")

    @bot.message_handler(commands=["add_admin"])
    def _aa(m):
        if get_role(m.from_user.id) != "admin":
            return
        t = _pid(m)
        if not t:
            bot.reply_to(m, "Format: /add_admin ID"); return
        add_user(t, "admin"); bot.reply_to(m, f"✅ Admin: <code>{t}</code>")

    @bot.message_handler(commands=["remove"])
    def _rm(m):
        if get_role(m.from_user.id) != "admin":
            return
        t = _pid(m)
        if not t:
            bot.reply_to(m, "Format: /remove ID"); return
        if t == m.from_user.id:
            bot.reply_to(m, "O'zingizni chiqara olmaysiz."); return
        remove_user(t); bot.reply_to(m, f"🚫 Chiqarildi: <code>{t}</code>")

    @bot.message_handler(commands=["xodimlar"])
    def _lst(m):
        if get_role(m.from_user.id) != "admin":
            return
        us = list_users()
        if not us:
            bot.reply_to(m, "Bo'sh."); return
        lines = [f"{'👑' if u['role']=='admin' else '👤'} <code>{u['tg_id']}</code> — {u['role']}"
                 for u in us]
        bot.reply_to(m, "<b>Foydalanuvchilar:</b>\n" + "\n".join(lines))


# ================== ISHGA TUSHIRISH ==================
if __name__ == "__main__":
    init_db()
    if bot:
        threading.Thread(target=lambda: bot.infinity_polling(skip_pending=True),
                         daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
