#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram-бот для администрирования каналов и групп.

Команды:
  /createlink <N>        — пригласительная ссылка с лимитом N участников;
  /id <@username|ID>     — узнать Account ID по юзернейму (или наоборот);
  /ban <@username|ID>    — забанить пользователя во всех чатах, где есть бот;
  /unban <@username|ID>  — снять бан во всех чатах;
  /chats                 — список чатов, известных боту.

Каждый пользователь, зашедший в чат (или подавший заявку), автоматически
попадает в лист «Пользователи» с username и Account ID.
"""

import os
import sys
import threading
import time
from datetime import datetime, timedelta

import gspread
import telebot
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound
from telebot.apihelper import ApiTelegramException
from telebot.types import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup

# ---------------------------------------------------------------------------
# Настройки (переопределяются переменными окружения)
# ---------------------------------------------------------------------------
# Токен задаётся только переменной окружения BOT_TOKEN — в коде его быть не должно.
TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    sys.exit("Не задан BOT_TOKEN. Добавьте Environment=BOT_TOKEN=... в unit-файл сервиса.")
GOOGLE_SHEETS_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "/root/bot/231.json")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1rum6ijKOn145YMmFuI67DDrkkI2kYej8ndM4Eq7gYAE")

# Telegram ID администраторов, которым разрешён глобальный бан (через запятую).
# Если пусто — глобальный бан разрешён админам/создателю чата, где вызвана команда.
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",")
    if x.lstrip("-").isdigit()
}

INVITE_LIFETIME_DAYS = 5
BAN_CONFIRM_TTL = 600  # секунд на подтверждение бана

# Мут новых участников до нажатия кнопки «Подтвердить участие».
# CONFIRM_ON_JOIN=0 полностью отключает проверку.
CONFIRM_ON_JOIN = os.getenv("CONFIRM_ON_JOIN", "1").lower() not in ("0", "false", "no", "off")
try:
    MUTE_HOURS = max(1, int(os.getenv("MUTE_HOURS", "24")))
except ValueError:
    MUTE_HOURS = 24  # страховка: Telegram сам снимет мут, если человек не подтвердил

bot = telebot.TeleBot(TOKEN)
BOT_ID = None

# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------
USERS_SHEET_NAME = "Пользователи"
CHATS_SHEET_NAME = "Чаты"
BANS_SHEET_NAME = "Баны"

USERS_HEADERS = ["Account ID", "Username", "Имя", "Первый вход", "Последний вход", "Откуда", "Статус"]
CHATS_HEADERS = ["Chat ID", "Название", "Тип", "Username", "Добавлен"]
BANS_HEADERS = ["Время", "Кто забанил", "Account ID", "Username", "Забанен в чатах", "Не удалось", "Причина"]

try:
    credentials = Credentials.from_service_account_file(
        GOOGLE_SHEETS_CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(credentials)
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)
except Exception as e:
    sys.exit(f"Не удалось подключиться к Google Sheets: {e}\n"
             f"Проверьте файл ключа {GOOGLE_SHEETS_CREDENTIALS_FILE} и доступ к таблице {SPREADSHEET_ID}.")

links_sheet = spreadsheet.sheet1  # первый лист — журнал ссылок (как было раньше)


def ensure_worksheet(title, headers, rows=3000):
    """Возвращает лист по названию, создавая его с шапкой при необходимости."""
    try:
        return spreadsheet.worksheet(title)
    except WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=rows, cols=len(headers))
        ws.append_row(headers)
        return ws


try:
    users_sheet = ensure_worksheet(USERS_SHEET_NAME, USERS_HEADERS)
    chats_sheet = ensure_worksheet(CHATS_SHEET_NAME, CHATS_HEADERS)
    bans_sheet = ensure_worksheet(BANS_SHEET_NAME, BANS_HEADERS)
except Exception as e:
    sys.exit(f"Не удалось открыть листы таблицы: {e}\n"
             f"У сервисного аккаунта должны быть права редактора на таблицу.")

# ---------------------------------------------------------------------------
# Кэши в памяти (чтобы не дёргать Sheets на каждое сообщение)
# ---------------------------------------------------------------------------
LOCK = threading.RLock()

USERS = {}            # "123456" -> {row, username, name, first, last, place, status, _ts}
USERNAME_INDEX = {}   # "@name" (в нижнем регистре) -> "123456"
CHATS = {}            # "-100123456" -> {row, title, type, username}
USERS_NEXT_ROW = 2
CHATS_NEXT_ROW = 2
PENDING_BANS = {}     # (chat_id, message_id) -> {user_id, reason, actor_id, ts}
MUTED = {}            # (chat_id, user_id) -> {since, name} — новички до подтверждения
_LAST_RELOAD = 0.0


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _pad(row, size):
    row = list(row)
    return row + [""] * (size - len(row))


def load_caches(force=False):
    """Читает листы «Пользователи» и «Чаты» в память (при старте и при промахе поиска)."""
    global USERS_NEXT_ROW, CHATS_NEXT_ROW, _LAST_RELOAD
    with LOCK:
        if not force and _LAST_RELOAD and time.time() - _LAST_RELOAD < 60:
            return
        _LAST_RELOAD = time.time()
        USERS.clear()
        USERNAME_INDEX.clear()
        CHATS.clear()

        rows = users_sheet.get_all_values()
        for idx, raw in enumerate(rows[1:], start=2):
            row = _pad(raw, len(USERS_HEADERS))
            uid = row[0].strip()
            if not uid:
                continue
            USERS[uid] = {
                "row": idx, "username": row[1], "name": row[2], "first": row[3],
                "last": row[4], "place": row[5], "status": row[6], "_ts": None,
            }
            if row[1]:
                USERNAME_INDEX[row[1].lower()] = uid
        USERS_NEXT_ROW = max(len(rows) + 1, 2)

        rows = chats_sheet.get_all_values()
        for idx, raw in enumerate(rows[1:], start=2):
            row = _pad(raw, len(CHATS_HEADERS))
            cid = row[0].strip()
            if not cid:
                continue
            CHATS[cid] = {"row": idx, "title": row[1], "type": row[2], "username": row[3]}
        CHATS_NEXT_ROW = max(len(rows) + 1, 2)

        print(f"Загружено: {len(USERS)} пользователей, {len(CHATS)} чатов")


def _place_of(chat):
    if chat is None:
        return ""
    if getattr(chat, "type", "") == "private":
        return "личка с ботом"
    return chat.title or (f"@{chat.username}" if chat.username else str(chat.id))


def upsert_user(user, chat=None, status=None, force=False):
    """Добавляет пользователя в лист «Пользователи» или обновляет его запись."""
    global USERS_NEXT_ROW
    if user is None or getattr(user, "is_bot", False):
        return

    uid = str(user.id)
    uname = f"@{user.username}" if user.username else ""
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    place = _place_of(chat)
    stamp = now_str()

    with LOCK:
        rec = USERS.get(uid)

        if rec is None:
            row = [uid, uname, name, stamp, stamp, place, status or "участник"]
            try:
                users_sheet.append_row(row)
            except Exception as e:
                print(f"Не удалось записать пользователя {uid}: {e}")
                return
            USERS[uid] = {
                "row": USERS_NEXT_ROW, "username": uname, "name": name,
                "first": stamp, "last": stamp, "place": place,
                "status": status or "участник", "_ts": datetime.now(),
            }
            USERS_NEXT_ROW += 1
            if uname:
                USERNAME_INDEX[uname.lower()] = uid
            return

        new_username = uname or rec["username"]
        new_name = name or rec["name"]
        new_place = place or rec["place"]
        new_status = status or rec["status"]

        changed = (new_username != rec["username"] or new_name != rec["name"]
                   or new_place != rec["place"] or new_status != rec["status"])
        stale = rec["_ts"] is None or (datetime.now() - rec["_ts"]).total_seconds() > 1800
        if not force and not changed and not stale:
            return

        values = [uid, new_username, new_name, rec["first"], stamp, new_place, new_status]
        try:
            users_sheet.update([values], f"A{rec['row']}:G{rec['row']}")
        except Exception as e:
            print(f"Не удалось обновить пользователя {uid}: {e}")
            return

        if rec["username"] and rec["username"] != new_username:
            USERNAME_INDEX.pop(rec["username"].lower(), None)
        if new_username:
            USERNAME_INDEX[new_username.lower()] = uid

        rec.update({
            "username": new_username, "name": new_name, "place": new_place,
            "status": new_status, "last": stamp, "_ts": datetime.now(),
        })


def set_user_status(user_id, status):
    """Меняет только колонку «Статус» у известного пользователя."""
    uid = str(user_id)
    with LOCK:
        rec = USERS.get(uid)
        if not rec:
            return
        stamp = now_str()
        values = [uid, rec["username"], rec["name"], rec["first"], stamp, rec["place"], status]
        try:
            users_sheet.update([values], f"A{rec['row']}:G{rec['row']}")
        except Exception as e:
            print(f"Не удалось обновить статус {uid}: {e}")
            return
        rec["status"] = status
        rec["last"] = stamp
        rec["_ts"] = datetime.now()


def remember_chat(chat):
    """Запоминает чат, в котором присутствует бот."""
    global CHATS_NEXT_ROW
    if chat is None:
        return
    cid = str(chat.id)
    title = chat.title or getattr(chat, "first_name", "") or (f"@{chat.username}" if chat.username else cid)
    uname = f"@{chat.username}" if chat.username else ""
    ctype = getattr(chat, "type", "")

    with LOCK:
        rec = CHATS.get(cid)
        if rec is None:
            try:
                chats_sheet.append_row([cid, title, ctype, uname, now_str()])
            except Exception as e:
                print(f"Не удалось записать чат {cid}: {e}")
                return
            CHATS[cid] = {"row": CHATS_NEXT_ROW, "title": title, "type": ctype, "username": uname}
            CHATS_NEXT_ROW += 1
        elif rec["title"] != title:
            try:
                chats_sheet.update([[title]], f"B{rec['row']}:B{rec['row']}")
            except Exception as e:
                print(f"Не удалось обновить чат {cid}: {e}")
                return
            rec["title"] = title


def forget_chat(chat_id):
    with LOCK:
        CHATS.pop(str(chat_id), None)


def is_admin_or_creator(chat_id, user_id):
    try:
        chat_member = bot.get_chat_member(chat_id, user_id)
        return chat_member.status in ["administrator", "creator"]
    except Exception as e:
        print(f"Ошибка при проверке прав пользователя: {e}")
        return False


# ---------------------------------------------------------------------------
# Мут новых участников до нажатия кнопки «Подтвердить»
# ---------------------------------------------------------------------------
def _confirm_markup(user_id):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Подтвердить участие", callback_data=f"joinok_{user_id}"))
    return markup


def _free_permissions(chat_id):
    """Права для снятия мута: по умолчанию чата, иначе «разрешено всё»."""
    try:
        perms = getattr(bot.get_chat(chat_id), "permissions", None)
        if perms is not None and perms.can_send_messages is not None:
            return perms
    except Exception as e:
        print(f"Не удалось прочитать права чата {chat_id}: {e}")
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
    )


def mute_until_confirm(chat, user):
    """Выдаёт новичку мут и просит подтвердить участие кнопкой.

    Мут нужен, чтобы кнопку нельзя было проигнорировать. Снимается по нажатию
    или сам по себе через MUTE_HOURS, если человек так и не подтвердил.
    """
    if not CONFIRM_ON_JOIN or chat is None or user is None:
        return
    if getattr(user, "is_bot", False) or chat.type not in ("group", "supergroup"):
        return
    if is_admin_or_creator(chat.id, user.id):
        return

    # Вход может прийти дважды (chat_member и new_chat_members) — второй раз пропускаем.
    # Проверка и отметка под одним замком, иначе два потока замутят дважды.
    with LOCK:
        if (chat.id, user.id) in MUTED:
            return
        MUTED[(chat.id, user.id)] = {
            "since": datetime.now(), "name": user.first_name or str(user.id),
        }

    until = int((datetime.now() + timedelta(hours=MUTE_HOURS)).timestamp())
    try:
        bot.restrict_chat_member(
            chat.id, user.id,
            until_date=until,
            permissions=ChatPermissions(can_send_messages=False),
            use_independent_chat_permissions=True,
        )
    except ApiTelegramException as e:
        print(f"Не удалось выдать мут {user.id} в {chat.id}: {e.description}")
        MUTED.pop((chat.id, user.id), None)  # иначе повторная попытка будет заблокирована
        return
    except Exception as e:
        print(f"Не удалось выдать мут {user.id} в {chat.id}: {e}")
        MUTED.pop((chat.id, user.id), None)
        return

    try:
        bot.send_message(
            chat.id,
            f"{user.first_name}, добро пожаловать!\n\n"
            f"Чтобы писать в чате, нажмите кнопку ниже. Пока подтверждения нет, "
            f"отправка сообщений закрыта (не более {MUTE_HOURS} ч).",
            reply_markup=_confirm_markup(user.id),
        )
    except Exception as e:
        print(f"Не удалось отправить приветствие в {chat.id}: {e}")


@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("joinok_"))
def handle_join_confirm(call):
    try:
        target = int(call.data.split("_", 1)[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "Некорректная кнопка.")
        return

    # Кнопку должен нажимать сам новичок, а не кто-то другой.
    if call.from_user.id != target:
        bot.answer_callback_query(call.id, "Эта кнопка не для вас.")
        return

    chat_id = call.message.chat.id
    try:
        bot.restrict_chat_member(
            chat_id, target,
            permissions=_free_permissions(chat_id),
            use_independent_chat_permissions=True,
        )
    except ApiTelegramException as e:
        print(f"Не удалось снять мут {target} в {chat_id}: {e.description}")
        bot.answer_callback_query(call.id, "Не удалось снять ограничение, напишите администратору.")
        return
    except Exception as e:
        print(f"Не удалось снять мут {target} в {chat_id}: {e}")
        bot.answer_callback_query(call.id, "Ошибка, напишите администратору.")
        return

    MUTED.pop((chat_id, target), None)
    upsert_user(call.from_user, call.message.chat, status="подтверждён")
    remember_chat(call.message.chat)

    try:
        bot.edit_message_text(
            f"{call.from_user.first_name} подтвердил участие ✅",
            chat_id, call.message.message_id,
        )
    except Exception:
        pass
    bot.answer_callback_query(call.id, "Готово, можете писать.")


def can_global_ban(chat_id, user_id):
    """Глобальный бан: только ADMIN_IDS, либо (если не заданы) админ текущего чата.

    Для постов в канале user_id is None: писать в канал могут только админы,
    поэтому такой запрос считается доверенным.
    """
    if user_id is None:
        return True
    if ADMIN_IDS:
        return user_id in ADMIN_IDS
    return is_admin_or_creator(chat_id, user_id)


def actor_of(message):
    """Автор команды. None для постов в канале."""
    return getattr(message, "from_user", None)


def actor_label(message):
    user = actor_of(message)
    if user is None:
        return f"канал {message.chat.id}"
    return f"{user.id} (@{user.username})" if user.username else str(user.id)


def reply_author(message):
    """Автор сообщения, на которое отвечают (None для постов от имени канала)."""
    if not message.reply_to_message:
        return None
    return getattr(message.reply_to_message, "from_user", None)


def command_args(message):
    parts = (message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def resolve_user(query):
    """Ищет Account ID по ID или по @username. Возвращает (uid, запись) либо (None, None)."""
    q = (query or "").strip().lstrip("+")
    if not q:
        return None, None
    if q.isdigit():
        return q, USERS.get(q)

    key = "@" + q.lstrip("@").lower()
    uid = USERNAME_INDEX.get(key)
    if uid is None:
        load_caches()  # возможно, таблицу правили руками
        uid = USERNAME_INDEX.get(key)
    return (uid, USERS.get(uid)) if uid else (None, None)


def lookup_user(query):
    """Ищет пользователя: сначала в таблице, затем через Telegram API.

    Возвращает (uid, запись, примечание). Любой из элементов может быть None.
    """
    uid, rec = resolve_user(query)
    if uid:
        return uid, rec, None

    q = (query or "").strip().lstrip("+")
    if not q or q.isdigit():
        return None, None, None

    username = "@" + q.lstrip("@")
    try:
        chat = bot.get_chat(username)
    except ApiTelegramException as e:
        desc = (e.description or "").lower()
        if "not found" in desc:
            return None, None, f"Юзернейм {username} не найден в Telegram."
        return None, None, f"Не удалось получить {username}: {e.description}"
    except Exception as e:
        return None, None, f"Не удалось получить {username}: {e}"

    if chat.type != "private":
        return str(chat.id), None, (f"{username} — это {chat.type}, а не пользователь.\n"
                                    f"ID этого чата: {chat.id}")

    # Для приватного чата id совпадает с Account ID пользователя.
    upsert_user(chat, None, status="найден по username")
    return str(chat.id), USERS.get(str(chat.id)), None


def format_user_info(uid, rec):
    if not rec:
        return (f"Account ID: {uid}\n"
                f"Пользователь не найден в таблице «{USERS_SHEET_NAME}».")
    lines = [f"👤 {rec['username'] or 'без username'}", f"Account ID: {uid}"]
    if rec["name"]:
        lines.append(f"Имя: {rec['name']}")
    if rec["first"]:
        lines.append(f"Первый вход: {rec['first']}")
    if rec["last"]:
        lines.append(f"Последний вход: {rec['last']}")
    if rec["place"]:
        lines.append(f"Откуда: {rec['place']}")
    if rec["status"]:
        lines.append(f"Статус: {rec['status']}")
    return "\n".join(lines)


def target_chats():
    """Чаты, где бот присутствует и где вообще можно банить."""
    return {cid: info for cid, info in CHATS.items()
            if info.get("type") in ("group", "supergroup", "channel")}


def _short_list(items, limit=25):
    if not items:
        return ""
    text = "\n".join("  • " + s for s in items[:limit])
    if len(items) > limit:
        text += f"\n  … и ещё {len(items) - limit}"
    return "\n" + text


def ban_everywhere(user_id, reason="", actor=""):
    """Банит пользователя во всех известных чатах. Возвращает текст отчёта."""
    chats = target_chats()
    if not chats:
        return "Бот пока не знает ни одного чата — банить негде."

    done, denied, failed = [], [], []

    for chat_id, info in chats.items():
        title = info.get("title") or chat_id
        try:
            bot.ban_chat_member(chat_id, int(user_id))
            done.append(title)
        except ApiTelegramException as e:
            desc = (e.description or "").lower()
            if "chat not found" in desc or "chat_id is empty" in desc:
                forget_chat(chat_id)
                failed.append(f"{title}: чат недоступен")
            elif e.error_code == 403 or "not enough rights" in desc or "chat_admin_required" in desc:
                denied.append(f"{title}: нет прав")
            elif "user not found" in desc:
                failed.append(f"{title}: пользователь не найден")
            elif "can't remove chat owner" in desc or "user is an administrator" in desc:
                denied.append(f"{title}: пользователь — админ")
            else:
                failed.append(f"{title}: {e.description}")
        except Exception as e:
            failed.append(f"{title}: {e}")

    try:
        bans_sheet.append_row([
            now_str(), actor, user_id,
            USERS.get(str(user_id), {}).get("username", ""),
            len(done), len(denied) + len(failed), reason,
        ])
    except Exception as e:
        print(f"Не удалось записать бан в лист «{BANS_SHEET_NAME}»: {e}")

    set_user_status(user_id, "забанен")

    report = f"🚫 Бан {user_id} завершён.\nЗабанен: {len(done)} из {len(chats)}"
    if denied:
        report += f"\nБез прав ({len(denied)}):{_short_list(denied)}"
    if failed:
        report += f"\nОшибки ({len(failed)}):{_short_list(failed)}"
    return report[:3800]


def unban_everywhere(user_id, actor=""):
    chats = target_chats()
    if not chats:
        return "Бот пока не знает ни одного чата."

    done, failed = [], []
    for chat_id, info in chats.items():
        title = info.get("title") or chat_id
        try:
            bot.unban_chat_member(chat_id, int(user_id), only_if_banned=True)
            done.append(title)
        except ApiTelegramException as e:
            if "chat not found" in (e.description or "").lower():
                forget_chat(chat_id)
            failed.append(f"{title}: {e.description}")
        except Exception as e:
            failed.append(f"{title}: {e}")

    set_user_status(user_id, "разбанен")

    report = f"✅ Разбан {user_id} завершён.\nРазбанен: {len(done)} из {len(chats)}"
    if failed:
        report += f"\nОшибки ({len(failed)}):{_short_list(failed)}"
    return report[:3800]


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    upsert_user(actor_of(message), message.chat)
    remember_chat(message.chat)
    bot.reply_to(message, (
        "Доступные команды:\n\n"
        "/createlink <число> — пригласительная ссылка с лимитом участников\n"
        "/id <@username | ID> — узнать Account ID по юзернейму\n"
        "/id (ответом на сообщение) — узнать ID автора\n"
        "/ban <@username | ID> [причина] — забанить во всех чатах, где есть бот\n"
        "/unban <@username | ID> — снять бан во всех чатах\n"
        "/unmute <@username | ID> — снять ограничение с новичка\n"
        "/chats — чаты, известные боту\n\n"
        "Все, кто заходит в чат, автоматически попадают в лист «Пользователи». "
        "Новички не могут писать, пока не нажмут «Подтвердить участие»."
    ))


@bot.message_handler(commands=["id", "whois"])
@bot.channel_post_handler(commands=["id", "whois"])
def cmd_id(message):
    upsert_user(actor_of(message), message.chat)
    remember_chat(message.chat)

    args = command_args(message)
    reply_user = reply_author(message)

    if not args and reply_user:
        if str(reply_user.id) not in USERS:
            upsert_user(reply_user, message.chat)
        bot.reply_to(message, format_user_info(str(reply_user.id), USERS.get(str(reply_user.id))))
        return

    if not args:
        bot.reply_to(message, "Использование: /id @username или /id 123456789\n"
                              "Либо ответьте командой /id на сообщение нужного пользователя.")
        return

    uid, rec, note = lookup_user(args)
    if not uid:
        bot.reply_to(message, note or f"Пользователь {args} не найден.\n"
                                      f"Он появится в таблице «{USERS_SHEET_NAME}» после входа в чат, где есть бот.")
        return
    bot.reply_to(message, format_user_info(uid, rec) + (f"\n\n{note}" if note else ""))


@bot.message_handler(commands=["ban"])
@bot.channel_post_handler(commands=["ban"])
def cmd_ban(message):
    upsert_user(actor_of(message), message.chat)
    remember_chat(message.chat)

    actor = actor_of(message)
    if not can_global_ban(message.chat.id, actor.id if actor else None):
        bot.reply_to(message, "Глобальный бан доступен только администраторам.")
        return

    args = command_args(message)
    reply_user = reply_author(message)

    if not args and reply_user:
        uid, rec, reason = str(reply_user.id), USERS.get(str(reply_user.id)), ""
    elif args:
        parts = args.split(maxsplit=1)
        uid, rec, _ = lookup_user(parts[0])
        reason = parts[1] if len(parts) > 1 else ""
    else:
        bot.reply_to(message, "Использование: /ban <@username | ID> [причина]\n"
                              "Либо ответьте командой /ban на сообщение нарушителя.")
        return

    if not uid:
        bot.reply_to(message, f"Пользователь {args.split()[0]} не найден. Укажите его Account ID "
                              f"(узнать можно командой /id).")
        return
    if actor and int(uid) == actor.id:
        bot.reply_to(message, "Нельзя забанить самого себя.")
        return
    if int(uid) in ADMIN_IDS:
        bot.reply_to(message, "Этот пользователь — администратор бота, бан невозможен.")
        return

    chats = target_chats()
    who = f"{uid} ({rec['username']})" if rec and rec["username"] else uid
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Да, забанить", callback_data="ban_yes"),
        InlineKeyboardButton("Отмена", callback_data="ban_no"),
    )
    sent = bot.reply_to(
        message,
        f"Забанить {who} во всех чатах, где есть бот?\n"
        f"Чатов: {len(chats)}\n"
        f"Причина: {reason or '—'}",
        reply_markup=markup,
    )
    if len(PENDING_BANS) > 200:
        PENDING_BANS.clear()
    PENDING_BANS[(sent.chat.id, sent.message_id)] = {
        "user_id": uid, "reason": reason,
        "actor_id": actor.id if actor else None, "ts": time.time(),
    }


@bot.message_handler(commands=["unban"])
@bot.channel_post_handler(commands=["unban"])
def cmd_unban(message):
    upsert_user(actor_of(message), message.chat)
    remember_chat(message.chat)

    actor = actor_of(message)
    if not can_global_ban(message.chat.id, actor.id if actor else None):
        bot.reply_to(message, "Разбан доступен только администраторам.")
        return

    args = command_args(message)
    reply_user = reply_author(message)

    if not args and reply_user:
        uid = str(reply_user.id)
    elif args:
        uid, _, _ = lookup_user(args.split()[0])
    else:
        bot.reply_to(message, "Использование: /unban <@username | ID>")
        return

    if not uid:
        bot.reply_to(message, f"Пользователь {args.split()[0]} не найден. Укажите Account ID.")
        return

    bot.reply_to(message, unban_everywhere(uid, actor_label(message)))


@bot.message_handler(commands=["chats"])
@bot.channel_post_handler(commands=["chats"])
def cmd_chats(message):
    remember_chat(message.chat)
    chats = target_chats()
    if not chats:
        bot.reply_to(message, "Бот пока не знает ни одного чата.")
        return
    kinds = {"group": "группа", "supergroup": "супергруппа", "channel": "канал"}
    lines = [f"Чатов у бота: {len(chats)}\n"]
    for cid, info in list(chats.items())[:50]:
        kind = kinds.get(info.get("type"), info.get("type"))
        lines.append(f"• {info.get('title')} ({kind}) — {cid}")
    if len(chats) > 50:
        lines.append(f"… и ещё {len(chats) - 50}")
    bot.reply_to(message, "\n".join(lines)[:3800])


# ---------------------------------------------------------------------------
# Выбор формата для /createlink
# ---------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("format_"))
def handle_button_selection(call):
    try:
        if not is_admin_or_creator(call.message.chat.id, call.from_user.id):
            bot.answer_callback_query(call.id, "Вы не администратор или создатель группы.")
            return

        data = call.data.split("_")
        selected_type = data[1]
        limit = int(data[2])

        format_mapping = {"video": "Видео", "text": "Текстовый", "mixed": "Смешанный"}
        selected_format = format_mapping.get(selected_type, "Неизвестный формат")

        creation_time = now_str()

        invite_link = bot.create_chat_invite_link(
            chat_id=call.message.chat.id,
            member_limit=limit,
            expire_date=int((datetime.now() + timedelta(days=INVITE_LIFETIME_DAYS)).timestamp()),
        )

        user = call.from_user
        user_info = f"{user.first_name} {user.last_name or ''} (@{user.username or 'не указан'})"
        chat_title = call.message.chat.title or "Чат без имени"

        links_sheet.append_row([
            creation_time, user_info, chat_title, limit, selected_format,
            invite_link.invite_link, f"{INVITE_LIFETIME_DAYS} дней",
        ])

        upsert_user(user, call.message.chat)

        bot.send_message(chat_id=call.message.chat.id, text="Ссылка успешно добавлена в Google Таблицу!")
        bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
    except Exception as e:
        bot.send_message(call.message.chat.id, f"Произошла ошибка: {str(e)}")


# ---------------------------------------------------------------------------
# Подтверждение бана
# ---------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data in ("ban_yes", "ban_no"))
def handle_ban_confirm(call):
    key = (call.message.chat.id, call.message.message_id)
    data = PENDING_BANS.pop(key, None)

    if data is None or time.time() - data["ts"] > BAN_CONFIRM_TTL:
        bot.answer_callback_query(call.id, "Запрос устарел, повторите команду.")
        bot.edit_message_text("Запрос устарел.", call.message.chat.id, call.message.message_id)
        return

    if ADMIN_IDS:
        allowed = call.from_user.id in ADMIN_IDS
    else:
        allowed = (call.from_user.id == data["actor_id"]
                   or is_admin_or_creator(call.message.chat.id, call.from_user.id))

    if not allowed:
        bot.answer_callback_query(call.id, "Недостаточно прав.")
        return

    if call.data == "ban_no":
        bot.answer_callback_query(call.id, "Отменено.")
        bot.edit_message_text("Бан отменён.", call.message.chat.id, call.message.message_id)
        return

    bot.answer_callback_query(call.id, "Баню…")
    bot.edit_message_text("Выполняю бан…", call.message.chat.id, call.message.message_id)

    actor = f"{data['actor_id']} (@{call.from_user.username})" if call.from_user.username else str(data["actor_id"])
    report = ban_everywhere(data["user_id"], data["reason"], actor)
    bot.edit_message_text(report, call.message.chat.id, call.message.message_id)


# ---------------------------------------------------------------------------
# Отслеживание входов и выходов
# ---------------------------------------------------------------------------
@bot.chat_member_handler()
def on_chat_member(update):
    try:
        new, old = update.new_chat_member, update.old_chat_member
        user = new.user
        if getattr(user, "is_bot", False):
            return

        if new.status in ("member", "creator", "administrator") and old.status in ("left", "kicked"):
            upsert_user(user, update.chat, status="участник")
            remember_chat(update.chat)
            if new.status == "member":
                mute_until_confirm(update.chat, user)
        elif new.status == "kicked":
            upsert_user(user, update.chat, status="забанен", force=True)
            MUTED.pop((update.chat.id, user.id), None)
        elif new.status == "left":
            upsert_user(user, update.chat, status="вышел")
            MUTED.pop((update.chat.id, user.id), None)
    except Exception as e:
        print(f"chat_member: {e}")


@bot.chat_join_request_handler()
def on_join_request(request):
    try:
        upsert_user(request.from_user, request.chat, status="заявка")
        remember_chat(request.chat)
    except Exception as e:
        print(f"chat_join_request: {e}")


@bot.my_chat_member_handler()
def on_my_chat_member(update):
    try:
        if update.new_chat_member.user.id != BOT_ID:
            return
        if update.new_chat_member.status in ("member", "administrator"):
            remember_chat(update.chat)
    except Exception as e:
        print(f"my_chat_member: {e}")


@bot.message_handler(content_types=["new_chat_members"])
def on_new_chat_members(message):
    try:
        remember_chat(message.chat)
        for user in message.new_chat_members:
            upsert_user(user, message.chat, status="участник")
            mute_until_confirm(message.chat, user)
    except Exception as e:
        print(f"new_chat_members: {e}")


# ---------------------------------------------------------------------------
# /unmute — снять мут с новичка, не подтвердившего участие
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["unmute"])
@bot.channel_post_handler(commands=["unmute"])
def cmd_unmute(message):
    actor = actor_of(message)
    if actor is not None and not is_admin_or_creator(message.chat.id, actor.id):
        bot.reply_to(message, "Команда доступна администраторам и создателю группы.")
        return

    target = reply_author(message)
    args = command_args(message)
    if target is None and args:
        uid, rec, _ = lookup_user(args)
        if not uid:
            bot.reply_to(message, f"Пользователь {args} не найден.")
            return
        target = type("U", (), {"id": int(uid), "first_name": (rec or {}).get("name") or uid})()

    if target is None:
        bot.reply_to(message, "Использование: /unmute (ответом на сообщение) или /unmute <@username|ID>")
        return

    try:
        bot.restrict_chat_member(
            message.chat.id, target.id,
            permissions=_free_permissions(message.chat.id),
            use_independent_chat_permissions=True,
        )
    except ApiTelegramException as e:
        bot.reply_to(message, f"Не удалось снять ограничение: {e.description}")
        return
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")
        return

    MUTED.pop((message.chat.id, target.id), None)
    set_user_status(target.id, "подтверждён")
    bot.reply_to(message, f"Ограничение снято с {target.id}.")


# ---------------------------------------------------------------------------
# /createlink
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["createlink"])
@bot.channel_post_handler(commands=["createlink"])
def create_link(message):
    try:
        actor = actor_of(message)
        # В канале команду может отправить только администратор канала.
        if actor is not None and not is_admin_or_creator(message.chat.id, actor.id):
            bot.reply_to(message, "Эта команда доступна только администраторам и создателю группы.")
            return

        args = message.text.split()
        if len(args) != 2 or not args[1].isdigit():
            bot.reply_to(message, "Использование: /createlink <число>")
            return

        limit = int(args[1])
        remember_chat(message.chat)

        bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)

        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("Видео", callback_data=f"format_video_{limit}"),
            InlineKeyboardButton("Текст", callback_data=f"format_text_{limit}"),
            InlineKeyboardButton("Смешанный", callback_data=f"format_mixed_{limit}"),
        )

        bot.send_message(
            chat_id=message.chat.id,
            text=f"Выберите формат. Лимит: {limit}",
            reply_markup=markup,
        )
    except Exception as e:
        bot.reply_to(message, f"Произошла ошибка: {str(e)}")


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    load_caches()
    me = bot.get_me()
    BOT_ID = me.id
    print(f"Бот запущен: @{me.username} (id {BOT_ID})")
    if not ADMIN_IDS:
        print("ADMIN_IDS не заданы — глобальный бан разрешён админам/создателю чата.")

    bot.infinity_polling(
        allowed_updates=[
            "message", "edited_message", "callback_query",
            "chat_member", "my_chat_member", "chat_join_request",
        ],
        skip_pending=True,
    )
