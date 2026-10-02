import os
import re
import sqlite3
import asyncio
import logging
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from contextlib import contextmanager
from datetime import datetime

import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("stowsell")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PRODSELLER_API_KEY = os.getenv("PRODSELLER_API_KEY", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7631570288"))
BASE = os.getenv("PRODSELLER_BASE_URL", "https://prodseller.com/v1").rstrip("/")
DB = os.getenv("DB_PATH", "stowsell.db")
SYNC_SECONDS = int(os.getenv("SYNC_SECONDS", "300"))

WAIT_EMAIL, WAIT_PROOF = range(2)

# ---------------- Database ----------------
@contextmanager
def conn():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            api_price REAL NOT NULL DEFAULT 0,
            public_price REAL,
            sell_price REAL,
            image_url TEXT,
            delivery_type TEXT,
            in_stock INTEGER DEFAULT 1,
            stock INTEGER,
            sold INTEGER DEFAULT 0,
            requires_email INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            category TEXT DEFAULT 'أخرى',
            custom_price INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            buy_price REAL NOT NULL,
            sell_price REAL NOT NULL,
            email TEXT,
            status TEXT NOT NULL,
            proof_file_id TEXT,
            proof_type TEXT,
            prodseller_order_id TEXT,
            delivered_key TEXT,
            error_message TEXT,
            idempotency_key TEXT UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(telegram_id);
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        """)
        # Lightweight migrations for an older database.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(products)").fetchall()}
        for name, definition in [
            ("sold", "INTEGER DEFAULT 0"),
            ("custom_price", "INTEGER DEFAULT 0"),
        ]:
            if name not in cols:
                c.execute(f"ALTER TABLE products ADD COLUMN {name} {definition}")
        order_cols = {r["name"] for r in c.execute("PRAGMA table_info(orders)").fetchall()}
        for name, definition in [
            ("proof_file_id", "TEXT"), ("proof_type", "TEXT"),
            ("error_message", "TEXT"), ("idempotency_key", "TEXT"),
            ("updated_at", "TEXT")
        ]:
            if name not in order_cols:
                c.execute(f"ALTER TABLE orders ADD COLUMN {name} {definition}")
        defaults = {
            "store_name": "🛍️ StowSell",
            "welcome": "أهلاً بك 👋\nاختر المنتج الذي تريد شراءه:",
            "markup": os.getenv("DEFAULT_MARKUP_PERCENT", "20"),
            "binance_text": os.getenv("BINANCE_PAYMENT_TEXT", "ادفع المبلغ المطلوب عبر Binance ثم اضغط «أرسلت الدفع»."),
            "binance_to": os.getenv("BINANCE_PAY_TO", ""),
            "products_per_page": os.getenv("PRODUCTS_PER_PAGE", "8"),
            "currency": "USDT",
            "support": os.getenv("SUPPORT_USERNAME", ""),
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, str(v)))


def get_setting(key):
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else ""


def set_setting(key, value):
    with conn() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def money(v):
    return Decimal(str(v or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculated_price(api_price):
    markup = money(get_setting("markup") or "20")
    return money(api_price) * (Decimal("1") + markup / 100)


def is_admin(user_id):
    return user_id == ADMIN_ID


def register_user(user):
    if not user:
        return
    with conn() as c:
        c.execute("""INSERT INTO users(telegram_id,username,first_name) VALUES(?,?,?)
                     ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,last_seen=CURRENT_TIMESTAMP""",
                  (user.id, user.username or "", user.first_name or ""))

# ---------------- ProdSeller API ----------------
async def ps(method, path, **kwargs):
    if not PRODSELLER_API_KEY:
        raise RuntimeError("PRODSELLER_API_KEY غير مضبوط")
    headers = {"X-API-Key": PRODSELLER_API_KEY, "Accept": "application/json"}
    if method.upper() in ("POST", "PUT", "PATCH"):
        headers["Content-Type"] = "application/json"
    extra_headers = kwargs.pop("headers", None) or {}
    headers.update(extra_headers)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.request(method.upper(), BASE + path, headers=headers, **kwargs)
        try:
            data = r.json()
        except Exception:
            data = {"error": f"HTTP {r.status_code}", "raw": r.text[:500]}
        if r.status_code >= 400:
            detail = data.get("error") or data.get("message") or f"HTTP {r.status_code}"
            raise RuntimeError(str(detail))
        return data


async def sync_products():
    data = await ps("GET", "/products")
    items = data.get("products", []) if isinstance(data, dict) else []
    with conn() as c:
        for p in items:
            pid = str(p.get("id"))
            if not pid:
                continue
            old = c.execute("SELECT sell_price,enabled,category,custom_price FROM products WHERE id=?", (pid,)).fetchone()
            custom = int(old["custom_price"]) if old else 0
            sell = float(old["sell_price"]) if old and old["sell_price"] is not None and custom else float(calculated_price(p.get("price", 0)))
            enabled = int(old["enabled"]) if old else 1
            category = old["category"] if old else "أخرى"
            c.execute("""INSERT INTO products
                (id,name,description,api_price,public_price,sell_price,image_url,delivery_type,in_stock,stock,sold,requires_email,enabled,category,custom_price,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name,description=excluded.description,api_price=excluded.api_price,
                  public_price=excluded.public_price,image_url=excluded.image_url,delivery_type=excluded.delivery_type,
                  in_stock=excluded.in_stock,stock=excluded.stock,sold=excluded.sold,
                  requires_email=excluded.requires_email,updated_at=CURRENT_TIMESTAMP""",
                      (pid, p.get("name", ""), p.get("description", ""), float(p.get("price", 0)), p.get("publicPrice"),
                       sell, p.get("imageUrl"), (p.get("delivery") or {}).get("type", ""),
                       1 if p.get("inStock", True) else 0, p.get("stock"), p.get("sold", 0),
                       1 if p.get("requiresEmailActivation") else 0, enabled, category, custom))
    return len(items)

# ---------------- Data helpers ----------------
def get_product(pid):
    with conn() as c:
        return c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()


def all_products():
    with conn() as c:
        return c.execute("SELECT * FROM products ORDER BY category,name").fetchall()


def visible_products(category="all", search=""):
    q = "%" + search.strip() + "%"
    with conn() as c:
        if category != "all" and search.strip():
            return c.execute("SELECT * FROM products WHERE enabled=1 AND in_stock=1 AND category=? AND (name LIKE ? OR description LIKE ?) ORDER BY name", (category, q, q)).fetchall()
        if category != "all":
            return c.execute("SELECT * FROM products WHERE enabled=1 AND in_stock=1 AND category=? ORDER BY name", (category,)).fetchall()
        if search.strip():
            return c.execute("SELECT * FROM products WHERE enabled=1 AND in_stock=1 AND (name LIKE ? OR description LIKE ?) ORDER BY category,name", (q, q)).fetchall()
        return c.execute("SELECT * FROM products WHERE enabled=1 AND in_stock=1 ORDER BY category,name").fetchall()


def categories():
    with conn() as c:
        return [r["category"] for r in c.execute("SELECT DISTINCT category FROM products WHERE enabled=1 AND in_stock=1 ORDER BY category").fetchall()]


def user_orders(uid, limit=10):
    with conn() as c:
        return c.execute("SELECT * FROM orders WHERE telegram_id=? ORDER BY id DESC LIMIT ?", (uid, limit)).fetchall()


def order_by_id(oid):
    with conn() as c:
        return c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()

# ---------------- Customer UI ----------------
def catalog_keyboard(rows, page=0, category="all", search=""):
    per = max(1, int(get_setting("products_per_page") or 8))
    start, end = page * per, page * per + per
    buttons = [[InlineKeyboardButton(f"📦 {p['name'][:38]} — {money(p['sell_price']):.2f} {get_setting('currency')}", callback_data=f"view:{p['id']}")] for p in rows[start:end]]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"cat:{category}:{page-1}:{search[:30]}"))
    if end < len(rows):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"cat:{category}:{page+1}:{search[:30]}"))
    if nav: buttons.append(nav)
    if categories(): buttons.append([InlineKeyboardButton("🗂️ التصنيفات", callback_data="categories")])
    buttons.append([InlineKeyboardButton("🔎 بحث", callback_data="search_help"), InlineKeyboardButton("📋 طلباتي", callback_data="myorders")])
    buttons.append([InlineKeyboardButton("🔄 تحديث", callback_data="refresh")])
    return InlineKeyboardMarkup(buttons)


def catalog_text(category="all", search=""):
    title = get_setting("store_name")
    extra = f"\n🔎 البحث: `{search}`" if search else ""
    if category != "all": extra += f"\n📁 التصنيف: {category}"
    return f"*{title}*{extra}\n\n{get_setting('welcome')}"


async def show_catalog_message(message, category="all", page=0, search=""):
    rows = visible_products(category, search)
    await message.reply_text(catalog_text(category, search), parse_mode=ParseMode.MARKDOWN, reply_markup=catalog_keyboard(rows, page, category, search))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if not all_products():
        try: await sync_products()
        except Exception as e: log.warning("initial customer sync: %s", e)
    rows = visible_products()
    await update.message.reply_text(catalog_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=catalog_keyboard(rows))


async def refresh(q, context):
    await q.answer("جاري التحديث…")
    try: await sync_products()
    except Exception as e: log.warning("refresh sync: %s", e)
    rows = visible_products()
    await q.edit_message_text(catalog_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=catalog_keyboard(rows))


async def categories_cb(q, context):
    buttons = [[InlineKeyboardButton(f"📁 {x}", callback_data=f"cat:{x}:0:")] for x in categories()]
    buttons.append([InlineKeyboardButton("📦 كل المنتجات", callback_data="cat:all:0:")])
    buttons.append([InlineKeyboardButton("⬅️ رجوع", callback_data="refresh")])
    await q.edit_message_text("🗂️ *اختر التصنيف:*", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


async def cat_cb(q, context):
    parts = q.data.split(":", 3)
    cat = parts[1]; page = int(parts[2]); search = parts[3] if len(parts) > 3 else ""
    rows = visible_products(cat, search)
    await q.edit_message_text(catalog_text(cat, search), parse_mode=ParseMode.MARKDOWN, reply_markup=catalog_keyboard(rows, page, cat, search))


async def view_product(q, context):
    pid = q.data.split(":", 1)[1]
    p = get_product(pid)
    if not p:
        await q.answer("المنتج غير موجود", show_alert=True); return
    stock = str(p["stock"]) if p["stock"] is not None else ("متوفر" if p["in_stock"] else "غير متوفر")
    text = (f"📦 *{p['name']}*\n\n{p['description'] or 'بدون وصف'}\n\n"
            f"💰 السعر: *{money(p['sell_price']):.2f} {get_setting('currency')}*\n"
            f"📦 المخزون: {stock}\n")
    if p["requires_email"]: text += "\n📧 هذا المنتج يحتاج بريدك الإلكتروني للتفعيل."
    if p["delivery_type"]: text += f"\n⚡ التسليم: {p['delivery_type']}"
    kb = [[InlineKeyboardButton("🛒 شراء الآن", callback_data=f"buy:{pid}")], [InlineKeyboardButton("⬅️ المنتجات", callback_data="refresh")]]
    # If the API provides an image URL, try to show it. A failed image never blocks purchase.
    if p["image_url"]:
        try:
            await q.message.reply_photo(photo=p["image_url"], caption=text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
            await q.answer()
            return
        except Exception:
            pass
    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))


async def search_help(q, context):
    await q.answer()
    await q.message.reply_text("🔎 اكتب البحث بهذا الشكل:\n`/search اسم المنتج`", parse_mode=ParseMode.MARKDOWN)


async def search_cmd(update, context):
    term = " ".join(context.args).strip()
    if not term:
        return await update.message.reply_text("مثال: `/search Netflix`", parse_mode=ParseMode.MARKDOWN)
    rows = visible_products("all", term)
    await update.message.reply_text(catalog_text("all", term), parse_mode=ParseMode.MARKDOWN, reply_markup=catalog_keyboard(rows, 0, "all", term))


async def myorders_view(target, uid):
    rows = user_orders(uid, 10)
    if not rows:
        text = "📋 لا توجد طلبات حتى الآن."
    else:
        lines = ["📋 *آخر طلباتك:*", ""]
        labels = {"awaiting_payment":"بانتظار الدفع", "processing":"جاري التنفيذ", "awaiting_delivery":"بانتظار التسليم", "delivered":"تم التسليم", "rejected":"مرفوض", "failed":"فشل", "cancelled":"ملغى"}
        for r in rows:
            lines.append(f"#{r['id']} — {r['product_name'][:28]} — {labels.get(r['status'], r['status'])} — {money(r['sell_price']):.2f} {get_setting('currency')}")
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ المتجر", callback_data="refresh")]])
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await target.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def myorders_cmd(update, context):
    await myorders_view(update.message, update.effective_user.id)


# ---------------- Checkout ----------------
def create_local_order(uid, username, p, email=None):
    with conn() as c:
        idem = None
        cur = c.execute("""INSERT INTO orders(telegram_id,username,product_id,product_name,buy_price,sell_price,email,status,idempotency_key)
                          VALUES(?,?,?,?,?,?,?,?,NULL)""",
                        (uid, username or "", p["id"], p["name"], p["api_price"], p["sell_price"], email, "awaiting_payment"))
        oid = cur.lastrowid
        idem = f"stowsell-order-{oid}"
        c.execute("UPDATE orders SET idempotency_key=? WHERE id=?", (idem, oid))
    return oid


async def buy(q, context):
    pid = q.data.split(":", 1)[1]
    p = get_product(pid)
    if not p or not p["enabled"] or not p["in_stock"]:
        await q.answer("المنتج غير متوفر حالياً", show_alert=True); return ConversationHandler.END
    context.user_data["checkout"] = {"product_id": pid}
    if p["requires_email"]:
        await q.edit_message_text(f"📧 *{p['name']}*\n\nأرسل بريدك الإلكتروني المطلوب للتفعيل:", parse_mode=ParseMode.MARKDOWN)
        return WAIT_EMAIL
    await payment_screen(q.message, p)
    return WAIT_PROOF


async def email_received(update, context):
    email = update.message.text.strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        await update.message.reply_text("❌ البريد غير صالح. أرسله بصيغة صحيحة مثل name@example.com")
        return WAIT_EMAIL
    context.user_data["checkout"]["email"] = email
    p = get_product(context.user_data["checkout"]["product_id"])
    await payment_screen(update.message, p)
    return WAIT_PROOF


async def payment_screen(message, p):
    amount = money(p["sell_price"])
    text = (f"💳 *إتمام الطلب*\n\n📦 المنتج: *{p['name']}*\n"
            f"💰 المبلغ: *{amount:.2f} {get_setting('currency')}*\n\n"
            f"{get_setting('binance_text')}")
    if get_setting("binance_to"):
        text += f"\n\n📍 الدفع إلى:\n`{get_setting('binance_to')}`"
    text += "\n\nبعد الدفع اضغط الزر وأرسل إثبات الدفع (صورة) إن أمكن."
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ أرسلت الدفع", callback_data="paid")], [InlineKeyboardButton("❌ إلغاء", callback_data="cancelpay")]]))


async def paid(q, context):
    checkout = context.user_data.get("checkout")
    if not checkout:
        await q.answer("انتهت جلسة الشراء. أعد المحاولة.", show_alert=True); return ConversationHandler.END
    p = get_product(checkout["product_id"])
    if not p:
        await q.answer("المنتج غير موجود", show_alert=True); return ConversationHandler.END
    # Prevent creating multiple pending orders for the same user/product in the same session.
    with conn() as c:
        existing = c.execute("SELECT id FROM orders WHERE telegram_id=? AND product_id=? AND status IN ('awaiting_payment','processing','awaiting_delivery') ORDER BY id DESC LIMIT 1", (q.from_user.id, p["id"])).fetchone()
    if existing:
        oid = existing["id"]
    else:
        oid = create_local_order(q.from_user.id, q.from_user.username, p, checkout.get("email"))
    context.user_data["checkout_order_id"] = oid
    await q.answer("تم تسجيل طلبك")
    await q.edit_message_text(f"⏳ *تم تسجيل الطلب #{oid}*\n\nسيتم التحقق من الدفع يدوياً من الإدارة. يمكنك إرسال صورة إثبات الدفع الآن.", parse_mode=ParseMode.MARKDOWN,
                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📎 بدون صورة / إرسال لاحقاً", callback_data="no_proof")]]))
    await notify_admin_new_order(oid)
    return WAIT_PROOF


async def proof_photo(update, context):
    oid = context.user_data.get("checkout_order_id")
    if not oid:
        await update.message.reply_text("لا يوجد طلب مفتوح."); return ConversationHandler.END
    photo = update.message.photo[-1]
    with conn() as c:
        c.execute("UPDATE orders SET proof_file_id=?,proof_type='photo',updated_at=CURRENT_TIMESTAMP WHERE id=?", (photo.file_id, oid))
    await update.message.reply_text(f"✅ تم استلام إثبات الدفع للطلب #{oid}. بانتظار تأكيد الإدارة.")
    await notify_admin_new_order(oid, proof_update=True)
    return ConversationHandler.END


async def no_proof(q, context):
    oid = context.user_data.get("checkout_order_id")
    if oid:
        await q.edit_message_text(f"✅ الطلب #{oid} مسجل. بانتظار تأكيد الإدارة.")
    return ConversationHandler.END


async def cancelpay(q, context):
    context.user_data.pop("checkout", None)
    context.user_data.pop("checkout_order_id", None)
    await q.edit_message_text("❌ تم إلغاء عملية الشراء.")
    return ConversationHandler.END


async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("تم إلغاء العملية.")
    return ConversationHandler.END


async def notify_admin_new_order(oid, proof_update=False):
    # Sent by the main bot instance through context.application.bot when called from handlers.
    # This function is replaced with a closure-like global bot reference below.
    if not APP_BOT: return
    r = order_by_id(oid)
    if not r: return
    text = (f"🧾 *{'تحديث إثبات دفع' if proof_update else 'طلب دفع جديد'}*\n\n"
            f"🆔 الطلب: `#{r['id']}`\n👤 العميل: `{r['telegram_id']}` @{r['username'] or '-'}\n"
            f"📦 المنتج: *{r['product_name']}*\n💰 السعر: *{money(r['sell_price']):.2f} {get_setting('currency')}*\n"
            f"💵 تكلفة ProdSeller: {money(r['buy_price']):.2f}\n"
            f"📧 البريد: {r['email'] or '-'}\n📅 {r['created_at']}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ تأكيد الدفع", callback_data=f"admin_ok:{oid}"), InlineKeyboardButton("❌ رفض", callback_data=f"admin_no:{oid}")]])
    await APP_BOT.send_message(ADMIN_ID, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    if r["proof_file_id"] and proof_update:
        try:
            await APP_BOT.send_photo(ADMIN_ID, r["proof_file_id"], caption=f"📎 إثبات الدفع للطلب #{oid}")
        except Exception as e: log.warning("proof forward failed: %s", e)

# ---------------- Fulfillment ----------------
async def admin_action(q, context):
    if not is_admin(q.from_user.id): return
    action, raw = q.data.split(":", 1)
    oid = int(raw)
    r = order_by_id(oid)
    if not r:
        await q.answer("الطلب غير موجود", show_alert=True); return
    if action == "admin_no":
        with conn() as c:
            c.execute("UPDATE orders SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('awaiting_payment','processing')", (oid,))
        try: await APP_BOT.send_message(r["telegram_id"], f"❌ تم رفض الدفع للطلب #{oid}. إذا كنت قد دفعت فعلاً تواصل مع الإدارة.")
        except Exception: pass
        await q.edit_message_text(f"❌ تم رفض الطلب #{oid}.")
        return
    # Confirm atomically: only the first admin click moves the order to processing.
    with conn() as c:
        cur = c.execute("UPDATE orders SET status='processing',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='awaiting_payment'", (oid,))
        if cur.rowcount == 0:
            await q.answer("هذا الطلب تمت معالجته بالفعل.", show_alert=True); return
    await q.edit_message_text(f"⏳ الطلب #{oid}: جاري الشراء من ProdSeller…")
    await fulfill_order(oid)


async def fulfill_order(oid):
    r = order_by_id(oid)
    if not r: return
    body = {"productId": r["product_id"], "quantity": 1}
    if r["email"]: body["email"] = r["email"]
    try:
        data = await ps("POST", "/orders", json=body, headers={"Idempotency-Key": r["idempotency_key"]})
        po = data.get("order") if isinstance(data, dict) else None
        if not po: po = data
        ps_id = po.get("id") if isinstance(po, dict) else None
        delivered = po.get("deliveredKey") if isinstance(po, dict) else None
        delivered_keys = po.get("deliveredKeys") if isinstance(po, dict) else None
        status = str(po.get("status", data.get("status", ""))) if isinstance(po, dict) else ""
        if delivered:
            await mark_delivered(oid, ps_id, delivered)
            return
        if delivered_keys:
            await mark_delivered(oid, ps_id, "\n".join(map(str, delivered_keys)))
            return
        with conn() as c:
            c.execute("UPDATE orders SET status='awaiting_delivery',prodseller_order_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (ps_id, oid))
        if status.lower() in ("failed", "error"):
            await mark_failed(oid, f"ProdSeller status: {status}")
            return
        await APP_BOT.send_message(r["telegram_id"], f"⏳ تم قبول الطلب #{oid}. طلب ProdSeller رقم `{ps_id}` قيد التجهيز وسيتم إرسال المنتج تلقائياً عند التسليم.", parse_mode=ParseMode.MARKDOWN)
        if ps_id:
            asyncio.create_task(poll_delivery(oid, str(ps_id)))
    except Exception as e:
        log.exception("fulfillment failed")
        await mark_failed(oid, str(e))


async def poll_delivery(oid, ps_id):
    for _ in range(30):
        await asyncio.sleep(10)
        try:
            data = await ps("GET", f"/orders/{ps_id}")
            o = data.get("order") if isinstance(data, dict) else data
            if not isinstance(o, dict): continue
            delivered = o.get("deliveredKey")
            keys = o.get("deliveredKeys")
            status = str(o.get("status", "")).lower()
            if delivered or keys:
                await mark_delivered(oid, ps_id, delivered or "\n".join(map(str, keys)))
                return
            if status in ("failed", "cancelled", "error"):
                await mark_failed(oid, f"ProdSeller: {status}")
                return
        except Exception as e:
            log.warning("poll order %s failed: %s", ps_id, e)
    await mark_failed(oid, "انتهت مهلة انتظار التسليم من ProdSeller")


async def mark_delivered(oid, ps_id, key):
    with conn() as c:
        c.execute("UPDATE orders SET status='delivered',prodseller_order_id=?,delivered_key=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (ps_id, key, oid))
    r = order_by_id(oid)
    if not r: return
    try:
        await APP_BOT.send_message(r["telegram_id"], f"🎉 *تم تسليم طلبك #{oid}*\n\n📦 {r['product_name']}\n\n🔑 *بيانات التسليم:*\n`{key}`", parse_mode=ParseMode.MARKDOWN)
    except Exception as e: log.warning("customer delivery notification: %s", e)
    try: await APP_BOT.send_message(ADMIN_ID, f"✅ تم تسليم الطلب #{oid} — {r['product_name']}")
    except Exception: pass


async def mark_failed(oid, error):
    with conn() as c:
        c.execute("UPDATE orders SET status='failed',error_message=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (error[:1000], oid))
    r = order_by_id(oid)
    if r:
        try: await APP_BOT.send_message(r["telegram_id"], f"⚠️ تعذر تنفيذ الطلب #{oid}. تم إشعار الإدارة وسيتم التعامل معه.\nالسبب: {error[:500]}")
        except Exception: pass
        try: await APP_BOT.send_message(ADMIN_ID, f"⚠️ فشل تنفيذ الطلب #{oid}\n{error[:1000]}")
        except Exception: pass

# ---------------- Admin UI ----------------
async def admin_panel(update, context):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("⚙️ *لوحة إدارة StowSell*", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard())


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 مزامنة المنتجات", callback_data="adm:sync"), InlineKeyboardButton("💰 رصيد ProdSeller", callback_data="adm:balance")],
        [InlineKeyboardButton("📦 المنتجات", callback_data="adm:products"), InlineKeyboardButton("📋 الطلبات", callback_data="adm:orders")],
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="adm:stats"), InlineKeyboardButton("⚙️ إعدادات", callback_data="adm:settings")],
        [InlineKeyboardButton("🏷️ التصنيفات", callback_data="adm:categories"), InlineKeyboardButton("🔎 بحث منتج", callback_data="adm:search")],
    ])


def back_admin(): return InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ لوحة الإدارة", callback_data="adm:panel")]])


async def admin_panel_cb(q, context):
    if not is_admin(q.from_user.id): return
    action = q.data.split(":", 1)[1]
    if action == "panel":
        await q.edit_message_text("⚙️ *لوحة إدارة StowSell*", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard()); return
    if action == "sync":
        try:
            n = await sync_products(); text = f"✅ تمت مزامنة {n} منتج."
        except Exception as e: text = f"❌ فشلت المزامنة: {e}"
        await q.edit_message_text(text, reply_markup=back_admin()); return
    if action == "balance":
        try:
            d = await ps("GET", "/balance")
            await q.edit_message_text(f"💰 *رصيد ProdSeller*\n\nالرصيد: `{d.get('balance')}`\nالعضوية: `{d.get('membership','-')}`", parse_mode=ParseMode.MARKDOWN, reply_markup=back_admin())
        except Exception as e: await q.edit_message_text(f"❌ {e}", reply_markup=back_admin())
        return
    if action == "products":
        rows = all_products()
        buttons = [[InlineKeyboardButton(f"{'🟢' if p['enabled'] else '🔴'} {p['name'][:30]} | {money(p['sell_price']):.2f}", callback_data=f"editp:{p['id']}")] for p in rows[:40]]
        buttons.append([InlineKeyboardButton("⚙️ لوحة الإدارة", callback_data="adm:panel")])
        await q.edit_message_text(f"📦 *المنتجات* ({len(rows)})\nاختر منتجاً للتعديل:", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)); return
    if action == "orders":
        with conn() as c: rows = c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 20").fetchall()
        labels = {"awaiting_payment":"دفع", "processing":"تنفيذ", "awaiting_delivery":"تسليم", "delivered":"تم", "rejected":"مرفوض", "failed":"فشل"}
        buttons = [[InlineKeyboardButton(f"#{r['id']} {r['product_name'][:22]} — {labels.get(r['status'],r['status'])}", callback_data=f"admorder:{r['id']}")] for r in rows]
        buttons.append([InlineKeyboardButton("⚙️ رجوع", callback_data="adm:panel")])
        await q.edit_message_text("📋 *آخر الطلبات*\nاختر طلباً:", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)); return
    if action == "stats":
        with conn() as c:
            total = c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
            delivered = c.execute("SELECT COUNT(*) n FROM orders WHERE status='delivered'").fetchone()["n"]
            revenue = c.execute("SELECT COALESCE(SUM(sell_price),0) n FROM orders WHERE status='delivered'").fetchone()["n"]
            cost = c.execute("SELECT COALESCE(SUM(buy_price),0) n FROM orders WHERE status='delivered'").fetchone()["n"]
            customers = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        profit = money(revenue) - money(cost)
        text = (f"📊 *إحصائيات المتجر*\n\n🧾 إجمالي الطلبات: `{total}`\n✅ المسلّمة: `{delivered}`\n"
                f"👥 العملاء: `{customers}`\n💰 المبيعات: `{money(revenue):.2f} {get_setting('currency')}`\n"
                f"💵 التكلفة: `{money(cost):.2f}`\n📈 الربح الإجمالي: `{profit:.2f} {get_setting('currency')}`")
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_admin()); return
    if action == "settings":
        text = ("⚙️ *إعدادات المتجر*\n\n"
                f"🏪 الاسم: `{get_setting('store_name')}`\n"
                f"📝 الترحيب: `{get_setting('welcome')[:100]}`\n"
                f"📈 الهامش الافتراضي: `{get_setting('markup')}%`\n"
                f"📦 منتجات/صفحة: `{get_setting('products_per_page')}`\n"
                f"💳 وجهة Binance: `{get_setting('binance_to') or '-'}`\n"
                f"🆘 الدعم: `{get_setting('support') or '-'}`\n\n"
                "الأوامر السريعة:\n`/setmarkup 20`\n`/setstore الاسم`\n`/setpay النص`\n`/setbinance العنوان`\n`/setwelcome النص`\n`/setpages 8`\n`/setsupport @username`")
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_admin()); return
    if action == "categories":
        with conn() as c: rows = c.execute("SELECT DISTINCT category FROM products ORDER BY category").fetchall()
        buttons = [[InlineKeyboardButton(f"📁 {r['category']}", callback_data=f"admc:{r['category']}")] for r in rows]
        buttons.append([InlineKeyboardButton("➕ إنشاء/تعيين تصنيف عبر تعديل المنتج", callback_data="adm:products")])
        buttons.append([InlineKeyboardButton("⚙️ رجوع", callback_data="adm:panel")])
        await q.edit_message_text("🏷️ *التصنيفات*", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)); return
    if action == "search":
        await q.edit_message_text("🔎 استخدم `/findproduct كلمة` للبحث في المنتجات.", parse_mode=ParseMode.MARKDOWN, reply_markup=back_admin()); return


async def edit_product(q, context):
    if not is_admin(q.from_user.id): return
    pid = q.data.split(":", 1)[1]; p = get_product(pid)
    if not p: await q.answer("المنتج غير موجود", show_alert=True); return
    text = (f"📦 *{p['name']}*\n\n🆔 `{p['id']}`\n💵 تكلفة ProdSeller: `{money(p['api_price']):.2f}`\n"
            f"💰 سعر البيع: `{money(p['sell_price']):.2f}`\n📈 مخصص: `{'نعم' if p['custom_price'] else 'لا'}`\n"
            f"📁 التصنيف: `{p['category']}`\n👁️ الحالة: `{'ظاهر' if p['enabled'] else 'مخفي'}`\n"
            f"📦 المخزون: `{p['stock'] if p['stock'] is not None else '-'} `")
    kb = [
        [InlineKeyboardButton("💰 تعديل السعر", callback_data=f"askprice:{pid}"), InlineKeyboardButton("👁️ إظهار/إخفاء", callback_data=f"toggle:{pid}")],
        [InlineKeyboardButton("🏷️ تعديل التصنيف", callback_data=f"askcat:{pid}"), InlineKeyboardButton("📝 تعديل الوصف", callback_data=f"askdesc:{pid}")],
        [InlineKeyboardButton("🖼️ تغيير الصورة", callback_data=f"askimg:{pid}"), InlineKeyboardButton("🔄 سعر الهامش", callback_data=f"autoprice:{pid}")],
        [InlineKeyboardButton("⬅️ المنتجات", callback_data="adm:products")]
    ]
    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))


async def admin_product_action(q, context):
    if not is_admin(q.from_user.id): return
    action, pid = q.data.split(":", 1)
    p = get_product(pid)
    if not p: await q.answer("المنتج غير موجود", show_alert=True); return
    if action == "toggle":
        new = 0 if p["enabled"] else 1
        with conn() as c: c.execute("UPDATE products SET enabled=? WHERE id=?", (new, pid))
        await q.answer("تم التغيير")
        return await edit_product(q, context)
    if action == "autoprice":
        val = float(calculated_price(p["api_price"]))
        with conn() as c: c.execute("UPDATE products SET sell_price=?,custom_price=0 WHERE id=?", (val, pid))
        await q.answer("تم تطبيق الهامش الافتراضي")
        return await edit_product(q, context)
    mapping = {"askprice":"price", "askcat":"category", "askdesc":"description", "askimg":"image"}
    context.user_data["admin_edit"] = {"type": mapping[action], "pid": pid}
    prompts = {"price":"أرسل سعر البيع الجديد، مثال: `7.99`", "category":"أرسل اسم التصنيف الجديد.", "description":"أرسل الوصف الجديد.", "image":"أرسل صورة المنتج الآن كصورة Telegram."}
    await q.edit_message_text(f"✏️ {prompts[mapping[action]]}", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data=f"editp:{pid}")]]))


async def admin_order_detail(q, context):
    if not is_admin(q.from_user.id): return
    oid = int(q.data.split(":", 1)[1]); r = order_by_id(oid)
    if not r: return
    text = (f"🧾 *الطلب #{oid}*\n\n👤 `{r['telegram_id']}` @{r['username'] or '-'}\n📦 *{r['product_name']}*\n"
            f"💰 {money(r['sell_price']):.2f} {get_setting('currency')}\n💵 التكلفة {money(r['buy_price']):.2f}\n"
            f"📧 {r['email'] or '-'}\n📌 الحالة: `{r['status']}`\n🧩 ProdSeller: `{r['prodseller_order_id'] or '-'}`\n"
            f"🔑 التسليم: `{r['delivered_key'] or '-'}`\n⚠️ الخطأ: `{r['error_message'] or '-'}`")
    kb=[]
    if r["status"] == "awaiting_payment": kb.append([InlineKeyboardButton("✅ تأكيد الدفع", callback_data=f"admin_ok:{oid}"), InlineKeyboardButton("❌ رفض", callback_data=f"admin_no:{oid}")])
    if r["proof_file_id"]: kb.append([InlineKeyboardButton("📎 إعادة إرسال الإثبات", callback_data=f"proof:{oid}")])
    kb.append([InlineKeyboardButton("⬅️ الطلبات", callback_data="adm:orders")])
    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))


async def proof_forward(q, context):
    if not is_admin(q.from_user.id): return
    oid=int(q.data.split(":",1)[1]); r=order_by_id(oid)
    if r and r["proof_file_id"]:
        await APP_BOT.send_photo(ADMIN_ID, r["proof_file_id"], caption=f"📎 إثبات الدفع للطلب #{oid}")
        await q.answer("تم الإرسال")

# ---------------- Admin text commands ----------------
async def admin_text(update, context):
    if not is_admin(update.effective_user.id): return
    edit = context.user_data.get("admin_edit")
    if not edit: return
    p = get_product(edit["pid"])
    if not p: context.user_data.pop("admin_edit", None); return
    typ = edit["type"]
    if typ == "image":
        if not update.message.photo: return
        val = update.message.photo[-1].file_id
        # Telegram file_id is not a public URL, but can be stored for reliable Telegram display.
        with conn() as c: c.execute("UPDATE products SET image_url=? WHERE id=?", (val, p["id"]))
    elif typ == "price":
        try: val = float(money(update.message.text.strip()))
        except (InvalidOperation, ValueError): return await update.message.reply_text("❌ سعر غير صالح. أرسل رقماً مثل 7.99")
        if val < 0: return await update.message.reply_text("❌ السعر لا يمكن أن يكون سالباً.")
        with conn() as c: c.execute("UPDATE products SET sell_price=?,custom_price=1 WHERE id=?", (val, p["id"]))
    elif typ == "category":
        val=update.message.text.strip()[:60] or "أخرى"
        with conn() as c: c.execute("UPDATE products SET category=? WHERE id=?", (val, p["id"]))
    else:
        val=update.message.text.strip()
        with conn() as c: c.execute("UPDATE products SET description=? WHERE id=?", (val, p["id"]))
    context.user_data.pop("admin_edit", None)
    await update.message.reply_text("✅ تم حفظ التعديل.")
    # Show the product again in a fresh admin message.
    await update.message.reply_text(f"📦 {p['name']}\n🆔 `{p['id']}`\n💰 السعر: {money(get_product(p['id'])['sell_price']):.2f}", parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ تعديل المنتج", callback_data=f"editp:{p['id']}"), InlineKeyboardButton("⚙️ الإدارة", callback_data="adm:panel")]]))

# ---------------- Commands ----------------
async def admin_sync_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    try: n=await sync_products(); await update.message.reply_text(f"✅ تمت مزامنة {n} منتج.")
    except Exception as e: await update.message.reply_text(f"❌ {e}")

async def setmarkup(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: return await update.message.reply_text("مثال: /setmarkup 20")
    try: money(context.args[0])
    except: return await update.message.reply_text("❌ نسبة غير صالحة")
    set_setting("markup", context.args[0]); await update.message.reply_text(f"✅ الهامش الافتراضي: {context.args[0]}%. الأسعار المخصصة لا تتغير.")

async def resetprices(update, context):
    if not is_admin(update.effective_user.id): return
    with conn() as c:
        for p in c.execute("SELECT id,api_price FROM products").fetchall():
            c.execute("UPDATE products SET sell_price=?,custom_price=0 WHERE id=?", (float(calculated_price(p["api_price"])), p["id"]))
    await update.message.reply_text("✅ أُعيد حساب أسعار كل المنتجات حسب الهامش الافتراضي.")

async def price_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    if len(context.args)!=2: return await update.message.reply_text("/price PRODUCT_ID 7.99")
    try: val=float(money(context.args[1]))
    except: return await update.message.reply_text("❌ السعر غير صحيح")
    with conn() as c:
        cur=c.execute("UPDATE products SET sell_price=?,custom_price=1 WHERE id=?", (val, context.args[0]))
    await update.message.reply_text("✅ تم حفظ السعر." if cur.rowcount else "❌ المنتج غير موجود.")

async def toggle_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    if len(context.args)!=1: return await update.message.reply_text("/toggle PRODUCT_ID")
    with conn() as c:
        p=c.execute("SELECT enabled FROM products WHERE id=?", (context.args[0],)).fetchone()
        if not p: return await update.message.reply_text("❌ المنتج غير موجود")
        new=0 if p["enabled"] else 1; c.execute("UPDATE products SET enabled=? WHERE id=?", (new,context.args[0]))
    await update.message.reply_text(f"✅ المنتج أصبح {'ظاهراً' if new else 'مخفياً'}.")

async def simple_setting(update, context, key, default=None):
    if not is_admin(update.effective_user.id): return
    val=" ".join(context.args).strip()
    if not val and default is None: return await update.message.reply_text(f"أرسل قيمة بعد الأمر.")
    set_setting(key, val if val else default); await update.message.reply_text("✅ تم حفظ الإعداد.")

async def setstore(update, context): await simple_setting(update,context,"store_name","StowSell")
async def setpay(update, context): await simple_setting(update,context,"binance_text")
async def setbinance(update, context): await simple_setting(update,context,"binance_to")
async def setwelcome(update, context): await simple_setting(update,context,"welcome")
async def setsupport(update, context): await simple_setting(update,context,"support")

async def setpages(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args or not context.args[0].isdigit(): return await update.message.reply_text("مثال: /setpages 8")
    set_setting("products_per_page", max(1,min(20,int(context.args[0])))); await update.message.reply_text("✅ تم تغيير عدد المنتجات في الصفحة.")

async def balance(update, context):
    if not is_admin(update.effective_user.id): return
    try:
        d=await ps("GET","/balance"); await update.message.reply_text(f"💰 الرصيد: {d.get('balance')}\n🏷️ العضوية: {d.get('membership','-')}")
    except Exception as e: await update.message.reply_text(f"❌ {e}")

async def findproduct(update, context):
    if not is_admin(update.effective_user.id): return
    term=" ".join(context.args).strip()
    if not term: return await update.message.reply_text("/findproduct كلمة")
    q="%"+term+"%"
    with conn() as c: rows=c.execute("SELECT * FROM products WHERE name LIKE ? OR id LIKE ? ORDER BY name LIMIT 30",(q,q)).fetchall()
    if not rows: return await update.message.reply_text("لا توجد نتائج.")
    kb=[[InlineKeyboardButton(f"{p['name'][:35]} — {money(p['sell_price']):.2f}",callback_data=f"editp:{p['id']}")] for p in rows]
    await update.message.reply_text("🔎 النتائج:",reply_markup=InlineKeyboardMarkup(kb))

async def orders_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    with conn() as c: rows=c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 20").fetchall()
    if not rows: return await update.message.reply_text("لا توجد طلبات.")
    lines=[f"#{r['id']} — {r['product_name'][:25]} — {r['status']} — {money(r['sell_price']):.2f}" for r in rows]
    await update.message.reply_text("📋 الطلبات:\n\n"+"\n".join(lines))

# ---------------- Background sync ----------------
APP_BOT = None

async def periodic_sync():
    while True:
        await asyncio.sleep(SYNC_SECONDS)
        try:
            n=await sync_products(); log.info("synced %s products", n)
        except Exception as e: log.warning("periodic sync failed: %s", e)

async def post_init(app):
    global APP_BOT
    APP_BOT = app.bot
    init_db()
    try:
        n=await sync_products(); log.info("initial sync: %s products", n)
    except Exception as e: log.warning("initial sync failed: %s", e)
    app.create_task(periodic_sync())

# ---------------- Main ----------------
def main():
    if not BOT_TOKEN or not PRODSELLER_API_KEY:
        raise SystemExit("ضع BOT_TOKEN و PRODSELLER_API_KEY في ملف .env")
    init_db()
    app=Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    checkout_conv=ConversationHandler(
        entry_points=[CallbackQueryHandler(buy, pattern=r"^buy:")],
        states={
            WAIT_EMAIL:[MessageHandler(filters.TEXT & ~filters.COMMAND, email_received)],
            WAIT_PROOF:[CallbackQueryHandler(paid, pattern=r"^paid$"), CallbackQueryHandler(no_proof, pattern=r"^no_proof$"), CallbackQueryHandler(cancelpay, pattern=r"^cancelpay$"), MessageHandler(filters.PHOTO, proof_photo)]
        },
        fallbacks=[CommandHandler("cancel",cancel)], per_user=True, per_chat=True, allow_reentry=True
    )

    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("search",search_cmd))
    app.add_handler(CommandHandler("myorders",myorders_cmd))
    app.add_handler(CommandHandler("admin",admin_panel))
    app.add_handler(CommandHandler("balance",balance))
    app.add_handler(CommandHandler("sync",admin_sync_cmd))
    app.add_handler(CommandHandler("orders",orders_cmd))
    app.add_handler(CommandHandler("findproduct",findproduct))
    app.add_handler(CommandHandler("setmarkup",setmarkup))
    app.add_handler(CommandHandler("resetprices",resetprices))
    app.add_handler(CommandHandler("price",price_cmd))
    app.add_handler(CommandHandler("toggle",toggle_cmd))
    app.add_handler(CommandHandler("setstore",setstore))
    app.add_handler(CommandHandler("setpay",setpay))
    app.add_handler(CommandHandler("setbinance",setbinance))
    app.add_handler(CommandHandler("setwelcome",setwelcome))
    app.add_handler(CommandHandler("setpages",setpages))
    app.add_handler(CommandHandler("setsupport",setsupport))
    # Admin editing text must be before the customer conversation handler.
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, admin_text), group=0)
    app.add_handler(checkout_conv, group=1)
    app.add_handler(CallbackQueryHandler(refresh, pattern=r"^refresh$"))
    app.add_handler(CallbackQueryHandler(categories_cb, pattern=r"^categories$"))
    app.add_handler(CallbackQueryHandler(cat_cb, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(view_product, pattern=r"^view:"))
    app.add_handler(CallbackQueryHandler(myorders_view_cb, pattern=r"^myorders$"))
    app.add_handler(CallbackQueryHandler(search_help, pattern=r"^search_help$"))
    app.add_handler(CallbackQueryHandler(admin_action, pattern=r"^admin_(ok|no):"))
    app.add_handler(CallbackQueryHandler(admin_panel_cb, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(edit_product, pattern=r"^editp:"))
    app.add_handler(CallbackQueryHandler(admin_product_action, pattern=r"^(askprice|askcat|askdesc|askimg|toggle|autoprice):"))
    app.add_handler(CallbackQueryHandler(admin_order_detail, pattern=r"^admorder:"))
    app.add_handler(CallbackQueryHandler(proof_forward, pattern=r"^proof:"))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


async def myorders_view_cb(q, context):
    await q.answer(); await myorders_view(q, q.from_user.id)


if __name__ == "__main__":
    main()
