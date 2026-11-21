import io
import os
import sqlite3
import textwrap
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import telebot
from telebot import types
from PIL import Image, ImageDraw, ImageFont

TOKEN = os.getenv("BOT_TOKEN", "9186881586:AAEIrfeVTdEW1QMkgiSqJJCl9yGMvho3Ph4")
PORT = int(os.getenv("PORT", 10000))

bot = telebot.TeleBot(TOKEN, parse_mode="Markdown")
db_lock = threading.Lock()


# --- МИКРО-СЕРВЕР ДЛЯ RENDER ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Battle Judge Bot is running!")

    def log_message(self, format, *args):
        pass


def run_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()


# --- БАЗА ДАННЫХ ---
def init_db():
    with db_lock:
        conn = sqlite3.connect("battle_judge.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                ignore_sec INTEGER DEFAULT 3600,
                window_sec INTEGER DEFAULT 7200,
                auto_unpin INTEGER DEFAULT 1,
                strict_mode INTEGER DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS registered_fighters (
                chat_id INTEGER,
                username TEXT,
                PRIMARY KEY (chat_id, username)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS fighters_activity (
                chat_id INTEGER,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                last_msg_text TEXT,
                last_msg_id INTEGER,
                last_msg_ts REAL,
                first_seen_ts REAL,
                pinged INTEGER DEFAULT 0,
                pinned_msg_id INTEGER DEFAULT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS battle_stats (
                chat_id INTEGER,
                user_id INTEGER,
                full_name TEXT,
                losses INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        conn.commit()
        conn.close()


def get_settings(chat_id):
    with db_lock:
        conn = sqlite3.connect("battle_judge.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        c.execute("SELECT ignore_sec, window_sec, auto_unpin, strict_mode FROM chat_settings WHERE chat_id = ?", (chat_id,))
        row = c.fetchone()
        conn.close()
        return {
            "ignore_sec": row[0],
            "window_sec": row[1],
            "auto_unpin": bool(row[2]),
            "strict_mode": bool(row[3]),
        }


def get_registered_fighters(chat_id):
    with db_lock:
        conn = sqlite3.connect("battle_judge.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT username FROM registered_fighters WHERE chat_id = ?", (chat_id,))
        rows = {r[0].lower() for r in c.fetchall()}
        conn.close()
        return rows


def is_admin(message):
    """Проверяет, является ли отправитель админом, создателем или анонимным админом группы."""
    if message.chat.type == "private":
        return True
    # Анонимный администратор группы в Telegram имеет ID 1087968824 или sender_chat == chat
    if message.from_user.id == 1087968824 or (message.sender_chat and message.sender_chat.id == message.chat.id):
        return True
    try:
        member = bot.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def escape_md(text):
    if not text:
        return ""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def format_duration(seconds):
    mins = int(seconds // 60)
    hours = mins // 60
    rem_mins = mins % 60
    if hours > 0:
        return f"{hours}ч {rem_mins}м"
    return f"{rem_mins}м"


# --- ГЕНЕРАЦИЯ БОЕВОЙ КАРТОЧКИ-ПРУФА ---
def get_font(size):
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def generate_battle_card(chat_title, full_name, username, msg_text, msg_time_str, now_str, uptime_str, losses):
    width = 800
    padding = 30
    font_tag = get_font(18)
    font_name = get_font(22)
    font_text = get_font(20)
    font_small = get_font(17)

    fighter_title = f"{full_name} (@{username})" if username else full_name
    clean_text = (msg_text or "[Медиа / Стикер / Голосовое]").strip()
    if len(clean_text) > 250:
        clean_text = clean_text[:247] + "..."

    wrapped = []
    for para in clean_text.split("\n"):
        wrapped.extend(textwrap.wrap(para, width=50) or [""])

    line_h = 28
    bubble_h = 75 + len(wrapped) * line_h + 40
    height = bubble_h + 185

    img = Image.new("RGB", (width, height), color=(13, 15, 22))
    draw = ImageDraw.Draw(img)

    # Красная верхняя полоса вердикта
    draw.rectangle([0, 0, width, 54], fill=(183, 28, 28))
    draw.text((padding, 13), "⚔️ ВЕРДИКТ СУДЬИ: ТЕХНИЧЕСКОЕ ПОРАЖЕНИЕ (ЧАС ИГНОРА)", fill=(255, 255, 255), font=font_tag)

    draw.text((padding, 68), f"Арена: {chat_title[:32]}  |  Время фиксации: {now_str}", fill=(158, 167, 184), font=font_small)

    # Бабл последнего выпада
    b_top = 102
    b_bot = b_top + bubble_h
    draw.rounded_rectangle([padding, b_top, width - padding, b_bot], radius=16, fill=(24, 28, 40), outline=(229, 57, 53), width=2)

    draw.text((padding + 20, b_top + 15), f"Боец: {fighter_title[:42]}", fill=(255, 138, 128), font=font_name)

    y = b_top + 52
    for line in wrapped:
        draw.text((padding + 20, y), line, fill=(236, 239, 244), font=font_text)
        y += line_h

    draw.text((width - padding - 275, b_bot - 30), f"Последний выпад: {msg_time_str}", fill=(144, 164, 174), font=font_small)

    # Статистика бойца
    draw.text((padding, b_bot + 16), f"⏱ Продержался в замесе: {uptime_str}   |   💀 Всего сливов: {losses}", fill=(255, 202, 40), font=font_tag)
    draw.text((padding, b_bot + 46), "СТАТУС: СЛИЛСЯ (НЕ ОТВЕЧАЛ БОЛЕЕ 60 МИНУТ)", fill=(239, 83, 80), font=font_name)

    bio = io.BytesIO()
    bio.name = "sliv_proof.png"
    img.save(bio, "PNG")
    bio.seek(0)
    return bio


# --- СТАРТ И ИНСТРУКЦИЯ ---
@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    if message.chat.type != "private" and not is_admin(message):
        return

    bot_username = bot.get_me().username
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⚔️ Добавить Судью в чат", url=f"https://t.me/{bot_username}?startgroup=true"))

    text = (
        "⚔️ *Я — Бот-Судья для троллинг-баттлов!*\n"
        "🔒 *Доступ к управлению есть только у администраторов чата.*\n\n"
        "*Как запустить баттл только среди нужных участников (и не трогать наблюдателей):*\n"
        "Введи в чате команду со списком юзернеймов бойцов:\n"
        "👉 `/battle @bot1 @bot2 @bot3 @bot4 @bot5`\n\n"
        "После этого я буду следить **строго за этими аккаунтами**, игнорируя всех остальных зрителей в чате.\n\n"
        "*Все команды управления (только для админов в группе):*\n"
        "🔥 `/battle @u1 @u2 ...` — старт баттла строго для указанных юзернеймов (если без ников — слежу за всеми)\n"
        "➕ `/addfighter @username` — добавить бойца в текущий баттл\n"
        "➖ `/delfighter @username` — убрать бойца из баттла\n"
        "📋 `/fighters` — список зарегистрированных бойцов\n"
        "⏱ `/timers` — живая сводка: кто сколько держится и сколько осталось до слива\n"
        "🏆 `/top` — таблица сливов (кто сколько раз проебал час игнора)\n"
        "⚙️ `/settime <минуты>` — время на ответ (по умолчанию `60`)\n"
        "📌 `/unpin` — вкл/выкл автооткрепление при ответе слившегося\n\n"
        "*⚠️ Настройка перед стартом:*\n"
        "1. Дай мне права *Администратора* (право *Закреплять сообщения*).\n"
        "2. В `@BotFather` должен быть выключен *Group Privacy* (`/setprivacy` -> `Disable`)."
    )
    bot.send_message(message.chat.id, text, reply_markup=markup)


@bot.message_handler(commands=["battle"])
def cmd_battle(message):
    if message.chat.type == "private" or not is_admin(message):
        return

    parts = message.text.split()[1:]
    usernames = [p.strip().lstrip("@").lower() for p in parts if p.strip().lstrip("@")]

    with db_lock:
        conn = sqlite3.connect("battle_judge.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM fighters_activity WHERE chat_id = ?", (message.chat.id,))
        c.execute("DELETE FROM registered_fighters WHERE chat_id = ?", (message.chat.id,))
        c.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (message.chat.id,))

        if usernames:
            c.execute("UPDATE chat_settings SET strict_mode = 1 WHERE chat_id = ?", (message.chat.id,))
            for u in usernames:
                c.execute("INSERT OR IGNORE INTO registered_fighters (chat_id, username) VALUES (?, ?)", (message.chat.id, u))
        else:
            c.execute("UPDATE chat_settings SET strict_mode = 0 WHERE chat_id = ?", (message.chat.id,))

        conn.commit()
        conn.close()

    settings = get_settings(message.chat.id)
    mins = int(settings["ignore_sec"] // 60)
    if usernames:
        fighters_list = "\n".join([f"• @{escape_md(u)}" for u in usernames])
        bot.send_message(
            message.chat.id,
            f"🚨 БАТТЛ ОБЪЯВЛЕН ОТКРЫТЫМ! 🚨\n\n"
            f"🎯 Режим строгого отбора: судья следит ТОЛЬКО за этими бойцами (наблюдателей не трогаю):\n"
            f"{fighters_list}\n\n"
            f"⏳ Лимит на ответ: {mins} мин.\n"
            f"💀 За игнор — реплай, карточка-пруф и позорный закреп!"
        )
    else:
        bot.send_message(
            message.chat.id,
            f"🚨 ОБЩИЙ БАТТЛ ОТКРЫТ! 🚨\n\n"
            f"Слежу за всеми, кто напишет в этот чат.\n"
            f"⏳ Лимит на ответ: {mins} мин."
        )


@bot.message_handler(commands=["addfighter"])
def cmd_addfighter(message):
    if message.chat.type == "private" or not is_admin(message):
        return
    parts = message.text.split()[1:]
    usernames = [p.strip().lstrip("@").lower() for p in parts if p.strip().lstrip("@")]
    if not usernames:
        bot.reply_to(message, "Укажите юзернейм(ы). Пример: /addfighter @titan_bot")
        return
    with db_lock:
        conn = sqlite3.connect("battle_judge.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (message.chat.id,))
        c.execute("UPDATE chat_settings SET strict_mode = 1 WHERE chat_id = ?", (message.chat.id,))
        for u in usernames:
            c.execute("INSERT OR IGNORE INTO registered_fighters (chat_id, username) VALUES (?, ?)", (message.chat.id, u))
        conn.commit()
        conn.close()
    added_str = ", ".join([f"@{escape_md(u)}" for u in usernames])
    bot.reply_to(message, f"⚔️ В сетку баттла добавлены: {added_str}")


@bot.message_handler(commands=["delfighter"])
def cmd_delfighter(message):
    if message.chat.type == "private" or not is_admin(message):
        return
    parts = message.text.split()[1:]
    usernames = [p.strip().lstrip("@").lower() for p in parts if p.strip().lstrip("@")]
    if not usernames:
        bot.reply_to(message, "Укажите юзернейм. Пример: /delfighter @user2")
        return
    with db_lock:
        conn = sqlite3.connect("battle_judge.db", check_same_thread=False)
        c = conn.cursor()
        for u in usernames:
            c.execute("DELETE FROM registered_fighters WHERE chat_id = ? AND username = ?", (message.chat.id, u))
            c.execute("DELETE FROM fighters_activity WHERE chat_id = ? AND LOWER(username) = ?", (message.chat.id, u))
        conn.commit()
        conn.close()
    del_str = ", ".join([f"@{escape_md(u)}" for u in usernames])
    bot.reply_to(message, f"🗑 Сняты с дистанции (переведены в зрители): {del_str}")


@bot.message_handler(commands=["fighters"])
def cmd_fighters(message):
    if message.chat.type == "private" or not is_admin(message):
        return
    settings = get_settings(message.chat.id)
    if not settings["strict_mode"]:
        bot.reply_to(message, "🌐 Сейчас включён общий режим — под наблюдением все, кто пишет в чат.")
        return
    fighters = get_registered_fighters(message.chat.id)
    if not fighters:
        bot.reply_to(message, "📭 Список бойцов пуст. Запустите баттл командой /battle @user1 @user2")
        return
    lines = ["⚔️ Зарегистрированные бойцы текущего баттла:\n"]
    for u in sorted(fighters):
        lines.append(f"• @{escape_md(u)}")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["timers", "status"])
def cmd_timers(message):
    if message.chat.type == "private" or not is_admin(message):
        return
    chat_id = message.chat.id
    settings = get_settings(chat_id)
    reg_fighters = get_registered_fighters(chat_id)
    now_ts = time.time()
    with db_lock:
        conn = sqlite3.connect("battle_judge.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT full_name, username, last_msg_ts, first_seen_ts, pinged FROM fighters_activity WHERE chat_id = ?", (chat_id,))
        rows = c.fetchall()
        conn.close()
    active_map = {}
    lines = []
    for full_name, username, last_ts, first_ts, pinged in rows:
        diff = now_ts - last_ts
        if diff > settings["window_sec"]:
            continue
        if username:
            active_map[username.lower()] = True
        name = f"@{escape_md(username)}" if username else escape_md(full_name)
        uptime = format_duration(last_ts - first_ts)
        if pinged:
            lines.append(f"💀 {name} — СЛИЛСЯ (в закрепе)")
        else:
            left_min = max(1, int((settings["ignore_sec"] - diff) / 60))
            lines.append(f"⚔️ {name} — держится {uptime} | до слива: {left_min} мин.")
    if settings["strict_mode"]:
        for u in sorted(reg_fighters):
            if u not in active_map:
                lines.append(f"⏳ @{escape_md(u)} — ждём первого сообщения в баттле")
    if not lines:
        bot.reply_to(message, "😴 На арене пока тихо.")
    else:
        bot.reply_to(message, "📊 Сводка по бойцам:\n\n" + "\n".join(lines))


@bot.message_handler(commands=["top"])
def cmd_top(message):
    if message.chat.type == "private" or not is_admin(message):
        return
    with db_lock:
        conn = sqlite3.connect("battle_judge.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT full_name, losses FROM battle_stats WHERE chat_id = ? ORDER BY losses DESC LIMIT 10", (message.chat.id,))
        rows = c.fetchall()
        conn.close()
    if not rows:
        bot.reply_to(message, "🏆 Пока ни одного слива не зафиксировано!")
        return
    medals = ["💀", "🥈", "🥉"]
    lines = ["📉 Рейтинг сливов (кто чаще проёбывает час игнора):\n"]
    for idx, (name, losses) in enumerate(rows):
        prefix = medals[idx] if idx < 3 else f"{idx + 1}."
        lines.append(f"{prefix} {escape_md(name)} — {losses} слив(ов)")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["settime"])
def cmd_settime(message):
    if message.chat.type == "private" or not is_admin(message):
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Пример: /settime 60 (время в минутах от 1 до 115)")
        return
    mins = max(1, min(115, int(parts[1])))
    with db_lock:
        conn = sqlite3.connect("battle_judge.db", check_same_thread=False)
        conn.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (message.chat.id,))
        conn.execute("UPDATE chat_settings SET ignore_sec = ? WHERE chat_id = ?", (mins * 60, message.chat.id))
        conn.commit()
        conn.close()
    bot.reply_to(message, f"⏱ Таймер слива установлен на {mins} мин.")


@bot.message_handler(commands=["unpin"])
def cmd_unpin(message):
    if message.chat.type == "private" or not is_admin(message):
        return
    settings = get_settings(message.chat.id)
    new_val = 0 if settings["auto_unpin"] else 1
    with db_lock:
        conn = sqlite3.connect("battle_judge.db", check_same_thread=False)
        conn.execute("UPDATE chat_settings SET auto_unpin = ? WHERE chat_id = ?", (new_val, message.chat.id))
        conn.commit()
        conn.close()
    bot.reply_to(message, f"📌 Автооткрепление при возвращении бойца {'включено' if new_val else 'выключено'}.")


# --- ПАРСИНГ СООБЩЕНИЙ НА АРЕНЕ ---

@bot.message_handler(
    func=lambda m: True,
    content_types=["text", "photo", "video", "voice", "video_note", "sticker", "document", "audio", "animation"]
)
def track_arena(message):
    if message.chat.type == "private" or message.from_user.is_bot:
        return
    # Игнорируем любые неизвестные команды, начинающиеся с /
    if message.text and message.text.startswith("/"):
        return
    chat_id = message.chat.id
    user = message.from_user
    settings = get_settings(chat_id)
    username = (user.username or "").strip()
    username_low = username.lower()
    if settings["strict_mode"]:
        reg_fighters = get_registered_fighters(chat_id)
        if not username_low or username_low not in reg_fighters:
            return
    now_ts = time.time()
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "Боец"
    preview = message.text or message.caption or f"[{message.content_type.upper()}]"
    with db_lock:
        conn = sqlite3.connect("battle_judge.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT pinged, pinned_msg_id, first_seen_ts FROM fighters_activity WHERE chat_id = ? AND user_id = ?", (chat_id, user.id))
        prev = c.fetchone()
        first_seen = prev[2] if (prev and not prev[0]) else now_ts
        if prev and prev[0] == 1 and prev[1] and settings["auto_unpin"]:
            try:
                bot.unpin_chat_message(chat_id, prev[1])
            except Exception:
                pass
        c.execute("""
            INSERT INTO fighters_activity (chat_id, user_id, username, full_name, last_msg_text, last_msg_id, last_msg_ts, first_seen_ts, pinged, pinned_msg_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                last_msg_text = excluded.last_msg_text,
                last_msg_id = excluded.last_msg_id,
                last_msg_ts = excluded.last_msg_ts,
                first_seen_ts = ?,
                pinged = 0,
                pinned_msg_id = NULL
        """, (chat_id, user.id, username, full_name, preview, message.id, now_ts, first_seen, first_seen))
        conn.commit()
        conn.close()


# --- ФОНОВЫЙ СУДЬЯ ---

def judge_loop():
    while True:
        time.sleep(15)
        now_ts = time.time()
        with db_lock:
            conn = sqlite3.connect("battle_judge.db", check_same_thread=False)
            c = conn.cursor()
            c.execute("SELECT chat_id, user_id, username, full_name, last_msg_text, last_msg_id, last_msg_ts, first_seen_ts, pinged FROM fighters_activity")
            rows = c.fetchall()
            conn.close()
        for chat_id, user_id, username, full_name, last_text, last_msg_id, last_ts, first_ts, pinged in rows:
            settings = get_settings(chat_id)
            silence = now_ts - last_ts
            if silence > settings["window_sec"]:
                with db_lock:
                    conn = sqlite3.connect("battle_judge.db", check_same_thread=False)
                    conn.execute("DELETE FROM fighters_activity WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
                    conn.commit()
                    conn.close()
                continue
            if pinged == 1 or silence < settings["ignore_sec"]:
                continue
            if settings["strict_mode"]:
                reg_fighters = get_registered_fighters(chat_id)
                if not username or username.lower() not in reg_fighters:
                    continue
            try:
                chat_info = bot.get_chat(chat_id)
                chat_title = chat_info.title or "Арена"
                with db_lock:
                    conn = sqlite3.connect("battle_judge.db", check_same_thread=False)
                    c = conn.cursor()
                    c.execute("""
                        INSERT INTO battle_stats (chat_id, user_id, full_name, losses)
                        VALUES (?, ?, ?, 1)
                        ON CONFLICT(chat_id, user_id) DO UPDATE SET
                            full_name = excluded.full_name,
                            losses = losses + 1
                    """, (chat_id, user_id, full_name))
                    conn.commit()
                    c.execute("SELECT losses FROM battle_stats WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
                    losses = c.fetchone()[0]
                    conn.close()
                last_msg_str = datetime.fromtimestamp(last_ts).strftime("%H:%M")
                now_str = datetime.fromtimestamp(now_ts).strftime("%H:%M")
                uptime_str = format_duration(last_ts - first_ts)
                card = generate_battle_card(
                    chat_title=chat_title,
                    full_name=full_name,
                    username=username,
                    msg_text=last_text,
                    msg_time_str=last_msg_str,
                    now_str=now_str,
                    uptime_str=uptime_str,
                    losses=losses
                )
                mention = f"@{escape_md(username)}" if username else f"{escape_md(full_name)}"
                caption = (
                    f"💀 {mention} проебал, час игнора — СЛИВ ЗАСЧИТАН!\n"
                    f"💬 Последний выпад был в {last_msg_str} (сейчас {now_str})\n"
                    f"⏱ Продержался в баттле: {uptime_str}\n"
                    f"📉 Слив №{losses} на этой арене"
                )
                sent = bot.send_photo(
                    chat_id,
                    photo=card,
                    caption=caption,
                    reply_to_message_id=last_msg_id
                )
                pinned_id = None
                try:
                    bot.pin_chat_message(chat_id, sent.message_id, disable_notification=False)
                    pinned_id = sent.message_id
                except Exception:
                    pass
                with db_lock:
                    conn = sqlite3.connect("battle_judge.db", check_same_thread=False)
                    conn.execute(
                        "UPDATE fighters_activity SET pinged = 1, pinned_msg_id = ? WHERE chat_id = ? AND user_id = ?",
                        (pinned_id, chat_id, user_id)
                    )
                    conn.commit()
                    conn.close()
            except Exception as e:
                print(f"Ошибка в чате {chat_id}: {e}", flush=True)


if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_web_server, daemon=True).start()
    threading.Thread(target=judge_loop, daemon=True).start()
    print("⚔️ Бот-Судья запущен и готов к баттлам!", flush=True)
    bot.infinity_polling(skip_pending=True)
