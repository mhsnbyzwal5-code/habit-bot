"""
ربات تلگرامی ردیاب عادت‌های روزانه
-----------------------------------
قابلیت‌ها:
- افزودن/حذف عادت
- ارسال خودکار چک‌لیست روزانه در ساعت مشخص با دکمه‌های تیک
- محاسبه استریک (روزهای پشت‌سرهم انجام‌شده)
- آمار کلی هر عادت
"""

import logging
import os
import sqlite3
import httpx
from datetime import date, timedelta, time as dtime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# تنظیمات
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT-YOUR-TOKEN-HERE")
DB_PATH = os.environ.get("DB_PATH", "habits.db")
# ساعت ارسال خودکار چک‌لیست روزانه (به وقت سرور - معمولا UTC)
REMINDER_HOUR = int(os.environ.get("REMINDER_HOUR", "6"))
REMINDER_MINUTE = int(os.environ.get("REMINDER_MINUTE", "0"))

# --- تنظیمات هوش مصنوعی (Google Gemini، رایگان) ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# دیتابیس
# ---------------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            active INTEGER DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_id INTEGER NOT NULL,
            log_date TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            FOREIGN KEY (habit_id) REFERENCES habits(id),
            UNIQUE(habit_id, log_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            due_date TEXT,
            priority TEXT DEFAULT 'متوسط',
            done INTEGER DEFAULT 0,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def add_habit(chat_id: int, name: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO habits (chat_id, name) VALUES (?, ?)", (chat_id, name)
    )
    conn.commit()
    habit_id = cur.lastrowid
    conn.close()
    return habit_id


def get_habits(chat_id: int):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, name FROM habits WHERE chat_id = ? AND active = 1 ORDER BY id",
        (chat_id,),
    ).fetchall()
    conn.close()
    return rows


def deactivate_habit(chat_id: int, habit_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "UPDATE habits SET active = 0 WHERE chat_id = ? AND id = ?",
        (chat_id, habit_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def is_done_today(habit_id: int, day: str) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT done FROM logs WHERE habit_id = ? AND log_date = ?",
        (habit_id, day),
    ).fetchone()
    conn.close()
    return bool(row and row[0] == 1)


def toggle_habit(habit_id: int, day: str) -> bool:
    """وضعیت تیک را برعکس می‌کند و مقدار جدید را برمی‌گرداند."""
    conn = get_conn()
    row = conn.execute(
        "SELECT done FROM logs WHERE habit_id = ? AND log_date = ?",
        (habit_id, day),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO logs (habit_id, log_date, done) VALUES (?, ?, 1)",
            (habit_id, day),
        )
        new_val = True
    else:
        new_val = row[0] == 0
        conn.execute(
            "UPDATE logs SET done = ? WHERE habit_id = ? AND log_date = ?",
            (1 if new_val else 0, habit_id, day),
        )
    conn.commit()
    conn.close()
    return new_val


def compute_streak(habit_id: int) -> int:
    """تعداد روزهای پشت‌سرهمی که تا امروز عادت انجام شده."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT log_date FROM logs WHERE habit_id = ? AND done = 1 ORDER BY log_date DESC",
        (habit_id,),
    ).fetchall()
    conn.close()
    done_dates = {r[0] for r in rows}

    streak = 0
    cursor_day = date.today()
    while cursor_day.isoformat() in done_dates:
        streak += 1
        cursor_day -= timedelta(days=1)
    return streak


def completion_rate(habit_id: int, days: int = 30) -> float:
    conn = get_conn()
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM logs WHERE habit_id = ? AND done = 1 AND log_date >= ?",
        (habit_id, since),
    ).fetchone()
    conn.close()
    done_count = row[0] if row else 0
    return round(100 * done_count / days, 1)


# ---------------------------------------------------------------------------
# تسک‌ها (کارهای یک‌باره با تاریخ و اولویت)
# ---------------------------------------------------------------------------
def add_task(chat_id: int, title: str, due_date: str = None, priority: str = "متوسط") -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO tasks (chat_id, title, due_date, priority, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, title, due_date, priority, date.today().isoformat()),
    )
    conn.commit()
    task_id = cur.lastrowid
    conn.close()
    return task_id


def get_open_tasks(chat_id: int):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, title, due_date, priority FROM tasks "
        "WHERE chat_id = ? AND done = 0 "
        "ORDER BY (due_date IS NULL), due_date ASC, id ASC",
        (chat_id,),
    ).fetchall()
    conn.close()
    return rows


def get_tasks_due_today_or_overdue(chat_id: int):
    today_str = date.today().isoformat()
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, title, due_date, priority FROM tasks "
        "WHERE chat_id = ? AND done = 0 AND due_date IS NOT NULL AND due_date <= ? "
        "ORDER BY due_date ASC",
        (chat_id, today_str),
    ).fetchall()
    conn.close()
    return rows


def complete_task(chat_id: int, task_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "UPDATE tasks SET done = 1 WHERE chat_id = ? AND id = ?", (chat_id, task_id)
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def delete_task(chat_id: int, task_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM tasks WHERE chat_id = ? AND id = ?", (chat_id, task_id)
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def set_task_date(chat_id: int, task_id: int, due_date: str) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "UPDATE tasks SET due_date = ? WHERE chat_id = ? AND id = ?",
        (due_date, chat_id, task_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def set_task_priority(chat_id: int, task_id: int, priority: str) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "UPDATE tasks SET priority = ? WHERE chat_id = ? AND id = ?",
        (priority, chat_id, task_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


# ---------------------------------------------------------------------------
# کیبورد چک‌لیست روزانه
# ---------------------------------------------------------------------------
def build_today_keyboard(chat_id: int):
    today = date.today().isoformat()
    habits = get_habits(chat_id)
    buttons = []
    for habit_id, name in habits:
        done = is_done_today(habit_id, today)
        label = f"✅ {name}" if done else f"⬜ {name}"
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"toggle:{habit_id}:{today}")]
        )
    return InlineKeyboardMarkup(buttons), habits


# ---------------------------------------------------------------------------
# هوش مصنوعی (Gemini)
# ---------------------------------------------------------------------------
async def ask_gemini(prompt: str) -> str:
    if not GEMINI_API_KEY:
        return (
            "⚠️ کلید Gemini تنظیم نشده. متغیر GEMINI_API_KEY رو ست کن "
            "(راهنما توی README هست)."
        )
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(GEMINI_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logger.warning(f"خطای Gemini: {e}")
        return "⚠️ الان نتونستم به هوش مصنوعی وصل بشم، بعداً دوباره امتحان کن."


# ---------------------------------------------------------------------------
# دستورات
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "سلام! 👋 این ربات کمکت می‌کنه عادت‌های روزانه و کارهات رو مدیریت کنی.\n\n"
        "📌 عادت‌های روزانه:\n"
        "/addhabit نام‌عادت — افزودن عادت جدید\n"
        "/habits — نمایش لیست عادت‌ها با شماره\n"
        "/delhabit شماره — حذف یک عادت\n"
        "/today — نمایش چک‌لیست امروز (با دکمه تیک)\n"
        "/stats — نمایش استریک و درصد موفقیت هر عادت\n\n"
        "✅ تسک‌های یک‌باره (با تاریخ و اولویت):\n"
        "/addtask عنوان — افزودن تسک ساده\n"
        "/addtask عنوان | تاریخ | اولویت — افزودن با جزئیات\n"
        "/tasks — نمایش تسک‌های باز\n"
        "/donetask شماره — تکمیل یک تسک\n"
        "/deltask شماره — حذف یک تسک\n"
        "/settaskdate شماره تاریخ — تنظیم/تغییر تاریخ\n"
        "/settaskpriority شماره اولویت — تنظیم اولویت (کم/متوسط/زیاد)\n\n"
        "🤖 هوش مصنوعی:\n"
        "/motivate — پیام انگیزشی هوشمند بر اساس وضعیت واقعیت\n"
        "/ask سوالت — پرسیدن هر سوالی درباره عادت‌سازی و برنامه‌ریزی\n\n"
        "هر روز ساعت مشخص، چک‌لیست عادت‌ها و تسک‌های امروز/عقب‌افتاده رو خودم برات می‌فرستم."
    )
    await update.message.reply_text(text)


async def add_habit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("فرمت درست: /addhabit نام عادت")
        return
    name = " ".join(context.args)
    add_habit(update.effective_chat.id, name)
    await update.message.reply_text(f"✅ عادت «{name}» اضافه شد.")


async def list_habits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    habits = get_habits(update.effective_chat.id)
    if not habits:
        await update.message.reply_text("هنوز عادتی ثبت نکردی. با /addhabit اضافه کن.")
        return
    lines = [f"{i+1}. {name}  (شناسه: {hid})" for i, (hid, name) in enumerate(habits)]
    await update.message.reply_text("لیست عادت‌ها:\n" + "\n".join(lines))


async def del_habit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("فرمت درست: /delhabit شناسه  (شناسه رو از /habits ببین)")
        return
    habit_id = int(context.args[0])
    ok = deactivate_habit(update.effective_chat.id, habit_id)
    if ok:
        await update.message.reply_text("عادت حذف شد.")
    else:
        await update.message.reply_text("همچین عادتی پیدا نشد.")


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    keyboard, habits = build_today_keyboard(chat_id)
    if not habits:
        await update.message.reply_text("هنوز عادتی ثبت نکردی. با /addhabit اضافه کن.")
        return
    today_str = date.today().strftime("%Y-%m-%d")
    await update.message.reply_text(
        f"چک‌لیست امروز ({today_str}):", reply_markup=keyboard
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    habits = get_habits(update.effective_chat.id)
    if not habits:
        await update.message.reply_text("هنوز عادتی ثبت نکردی.")
        return
    lines = []
    for habit_id, name in habits:
        streak = compute_streak(habit_id)
        rate = completion_rate(habit_id, 30)
        lines.append(f"• {name}: استریک {streak} روز | ۳۰ روز اخیر: {rate}%")
    await update.message.reply_text("📊 آمار عادت‌ها:\n" + "\n".join(lines))


async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "فرمت درست: /ask سوالت\nمثال: /ask چطور عادت مطالعه رو نگه دارم؟"
        )
        return
    question = " ".join(context.args)
    await update.message.chat.send_action("typing")
    answer = await ask_gemini(question)
    await update.message.reply_text(answer)


async def motivate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    habits = get_habits(update.effective_chat.id)
    if not habits:
        await update.message.reply_text("هنوز عادتی ثبت نکردی.")
        return
    summary = []
    for habit_id, name in habits:
        streak = compute_streak(habit_id)
        rate = completion_rate(habit_id, 30)
        summary.append(f"{name}: استریک {streak} روز، ۳۰ روز اخیر {rate}%")
    summary_text = "\n".join(summary)
    prompt = (
        "تو یه مربی عادت‌سازی مهربون و مختصرگو هستی. بر اساس این آمار واقعی کاربر، "
        "یه پیام انگیزشی کوتاه (حداکثر ۴-۵ خط) به فارسی بنویس. اگه جایی افت داشته، "
        "با لحن حمایتی (نه سرزنش‌گر) تشویقش کن ادامه بده:\n\n" + summary_text
    )
    await update.message.chat.send_action("typing")
    text = await ask_gemini(prompt)
    await update.message.reply_text("🤖 " + text)


# ---------------------------------------------------------------------------
# دستورات تسک (کارهای یک‌باره با تاریخ و اولویت)
# ---------------------------------------------------------------------------
async def addtask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    فرمت: /addtask عنوان کار
    یا با تاریخ: /addtask عنوان کار | 2026-09-25
    یا با تاریخ و اولویت: /addtask عنوان کار | 2026-09-25 | زیاد
    """
    if not context.args:
        await update.message.reply_text(
            "فرمت درست:\n"
            "/addtask عنوان کار\n"
            "یا: /addtask عنوان کار | 1404-07-05\n"
            "یا: /addtask عنوان کار | 1404-07-05 | زیاد\n"
            "(اولویت: کم / متوسط / زیاد)"
        )
        return
    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]
    title = parts[0]
    due_date = None
    priority = "متوسط"
    if len(parts) >= 2 and parts[1]:
        due_date = parts[1]
    if len(parts) >= 3 and parts[2]:
        priority = parts[2]
    add_task(update.effective_chat.id, title, due_date, priority)
    msg = f"✅ تسک «{title}» اضافه شد."
    if due_date:
        msg += f"\n📅 موعد: {due_date}"
    msg += f"\n⭐ اولویت: {priority}"
    await update.message.reply_text(msg)


async def tasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = get_open_tasks(update.effective_chat.id)
    if not tasks:
        await update.message.reply_text("هیچ تسک بازی نداری 🎉")
        return
    lines = []
    for tid, title, due_date, priority in tasks:
        line = f"#{tid} — {title}"
        if due_date:
            line += f" | 📅 {due_date}"
        line += f" | ⭐ {priority}"
        lines.append(line)
    await update.message.reply_text(
        "📋 تسک‌های باز:\n" + "\n".join(lines) +
        "\n\nبرای تکمیل: /donetask شماره\nبرای حذف: /deltask شماره"
    )


async def donetask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("فرمت درست: /donetask شماره")
        return
    ok = complete_task(update.effective_chat.id, int(context.args[0]))
    await update.message.reply_text("✅ تسک تکمیل شد." if ok else "همچین تسکی پیدا نشد.")


async def deltask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("فرمت درست: /deltask شماره")
        return
    ok = delete_task(update.effective_chat.id, int(context.args[0]))
    await update.message.reply_text("🗑 تسک حذف شد." if ok else "همچین تسکی پیدا نشد.")


async def settaskdate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text("فرمت درست: /settaskdate شماره تاریخ\nمثال: /settaskdate 3 2026-09-25")
        return
    ok = set_task_date(update.effective_chat.id, int(context.args[0]), context.args[1])
    await update.message.reply_text("📅 تاریخ ست شد." if ok else "همچین تسکی پیدا نشد.")


async def settaskpriority_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text("فرمت درست: /settaskpriority شماره اولویت\n(اولویت: کم/متوسط/زیاد)")
        return
    ok = set_task_priority(update.effective_chat.id, int(context.args[0]), context.args[1])
    await update.message.reply_text("⭐ اولویت ست شد." if ok else "همچین تسکی پیدا نشد.")


async def toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, habit_id_str, day = query.data.split(":")
    habit_id = int(habit_id_str)
    toggle_habit(habit_id, day)
    chat_id = query.message.chat_id
    keyboard, _ = build_today_keyboard(chat_id)
    await query.edit_message_reply_markup(reply_markup=keyboard)


# ---------------------------------------------------------------------------
# ارسال خودکار روزانه
# ---------------------------------------------------------------------------
async def send_daily_checklist(context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    chat_ids = set(
        r[0] for r in conn.execute("SELECT DISTINCT chat_id FROM habits WHERE active = 1")
    )
    chat_ids |= set(
        r[0] for r in conn.execute("SELECT DISTINCT chat_id FROM tasks WHERE done = 0")
    )
    conn.close()
    today_str = date.today().strftime("%Y-%m-%d")
    for chat_id in chat_ids:
        keyboard, habits = build_today_keyboard(chat_id)
        due_tasks = get_tasks_due_today_or_overdue(chat_id)
        if not habits and not due_tasks:
            continue
        try:
            if habits:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🌅 چک‌لیست امروز ({today_str}):",
                    reply_markup=keyboard,
                )
            if due_tasks:
                lines = []
                for tid, title, due_date, priority in due_tasks:
                    flag = "🔴" if due_date and due_date < today_str else "🟡"
                    lines.append(f"{flag} #{tid} — {title} | ⭐ {priority}")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="📌 تسک‌های امروز/عقب‌افتاده:\n" + "\n".join(lines) +
                    "\n\nبرای تکمیل: /donetask شماره",
                )
        except Exception as e:
            logger.warning(f"ارسال به {chat_id} ناموفق بود: {e}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addhabit", add_habit_cmd))
    app.add_handler(CommandHandler("habits", list_habits_cmd))
    app.add_handler(CommandHandler("delhabit", del_habit_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("motivate", motivate_cmd))
    app.add_handler(CommandHandler("ask", ask_cmd))
    app.add_handler(CommandHandler("addtask", addtask_cmd))
    app.add_handler(CommandHandler("tasks", tasks_cmd))
    app.add_handler(CommandHandler("donetask", donetask_cmd))
    app.add_handler(CommandHandler("deltask", deltask_cmd))
    app.add_handler(CommandHandler("settaskdate", settaskdate_cmd))
    app.add_handler(CommandHandler("settaskpriority", settaskpriority_cmd))
    app.add_handler(CallbackQueryHandler(toggle_callback, pattern=r"^toggle:"))

    # زمان‌بندی ارسال خودکار روزانه (به وقت UTC سرور)
    app.job_queue.run_daily(
        send_daily_checklist,
        time=dtime(hour=REMINDER_HOUR, minute=REMINDER_MINUTE),
    )

    logger.info("ربات در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
