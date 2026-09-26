from pathlib import Path
import py_compile, zipfile, textwrap

base = Path("/mnt/data/visit_bot_v2")
base.mkdir(exist_ok=True)

main_py = r'''# -*- coding: utf-8 -*-
"""
VIZIT BOT v2
Telegram + Google Sheets + Gemini

Muhim:
- Telegram xabarlari PLAIN TEXT yuboriladi.
- HTML/Markdown parser ishlatilmaydi.
- Shuning uchun \\1\\1\\1 kabi regex xatolari chiqmaydi.
- Dashboard va asosiy hisobotlarni Python o'zi hisoblaydi.
- Erkin savollarni Gemini tushunadi.
"""

import os
import re
import json
import time
import logging
import threading
from datetime import datetime
from collections import defaultdict

import requests
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDENTIALS_JSON = (
    os.getenv("GOOGLE_CREDENTIALS_JSON")
    or os.getenv("GOOGLE_CREDENTIALS")
    or ""
).strip()

GOOGLE_CREDENTIALS_FILE = os.getenv(
    "GOOGLE_CREDENTIALS_FILE", "credentials.json"
).strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID", "").strip()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()

VIZITLAR_SHEET_NAME = os.getenv(
    "VIZITLAR_SHEET_NAME", "Vizitlar"
).strip()

CHECK_INTERVAL_SECONDS = int(
    os.getenv("CHECK_INTERVAL_SECONDS", "1800")
)
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "60"))

MAX_GPS_METERS = float(
    os.getenv("MAX_POGRESHNOST_METERS", "150")
)
MAX_GAP_MINUTES = float(
    os.getenv("MAX_IDLE_GAP_MINUTES", "15")
)
MIN_GAP_MINUTES = float(
    os.getenv("MIN_TRAVEL_MINUTES", "3")
)

# Telegram ID -> agent name.
# Masalan:
# AGENT_CHAT_MAP={"123456789":"TP 7 Буранова Гулмира (NCF-CNF)"}
AGENT_CHAT_MAP_RAW = os.getenv("AGENT_CHAT_MAP", "{}").strip()

try:
    AGENT_CHAT_MAP = json.loads(AGENT_CHAT_MAP_RAW)
except Exception:
    AGENT_CHAT_MAP = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("visit-bot-v2")

DATA_LOCK = threading.Lock()
DATA_CACHE = {"rows": None, "loaded": 0.0}


# ============================================================
# BASIC HELPERS
# ============================================================

def clean(value):
    return str(value or "").strip()


def norm(value):
    value = clean(value).lower().replace("ё", "е")
    return re.sub(r"\s+", " ", value).strip()


def number(value, default=0.0):
    try:
        return float(str(value).replace(",", ".").strip())
    except Exception:
        return default


def integer(value, default=0):
    try:
        return int(float(str(value).replace(",", ".").strip()))
    except Exception:
        return default


def parse_dt(value):
    value = clean(value)
    if not value:
        return None

    formats = [
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).replace(tzinfo=None)
    except Exception:
        return None


def format_dt(value):
    return value.strftime("%d.%m.%Y %H:%M:%S") if value else "—"


def duration_text(minutes):
    if minutes is None:
        return "—"

    total = max(0, int(round(minutes * 60)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)

    if h:
        return f"{h} soat {m} daqiqa"
    if m:
        return f"{m} daqiqa"
    return f"{s} soniya"


def parse_gps(value):
    text = clean(value).lower()

    if not text:
        return 0.0

    result = 0.0

    km = re.search(r"(\d+(?:[.,]\d+)?)\s*км", text)
    met = re.search(r"(\d+(?:[.,]\d+)?)\s*м(?!\w)", text)

    if km:
        result += number(km.group(1)) * 1000
    if met:
        result += number(met.group(1))

    if result == 0:
        m = re.search(r"\d+(?:[.,]\d+)?", text)
        if m:
            result = number(m.group(0))

    return result


def pct(a, b):
    return round(a * 100 / b, 1) if b else 0.0


# ============================================================
# GOOGLE SHEETS
# ============================================================

def google_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]

    if GOOGLE_CREDENTIALS_JSON:
        try:
            info = json.loads(GOOGLE_CREDENTIALS_JSON)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "GOOGLE_CREDENTIALS_JSON noto'g'ri formatda: "
                + str(exc)
            )

        creds = Credentials.from_service_account_info(
            info, scopes=scopes
        )
    else:
        if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
            raise RuntimeError(
                "GOOGLE_CREDENTIALS_JSON yoki credentials.json topilmadi."
            )

        creds = Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE,
            scopes=scopes,
        )

    return gspread.authorize(creds)


def find_sheet(book):
    names = [
        VIZITLAR_SHEET_NAME,
        "Vizitlar",
        "Визитлар",
        "Визиты",
        "Визит",
    ]

    checked = set()

    for name in names:
        if not name or name in checked:
            continue

        checked.add(name)

        try:
            return book.worksheet(name)
        except gspread.exceptions.WorksheetNotFound:
            pass

    available = [x.title for x in book.worksheets()]
    raise RuntimeError(
        "Vizitlar sheet topilmadi. Mavjud sheetlar: "
        + ", ".join(available)
    )


def load_rows():
    client = google_client()
    book = client.open_by_key(GOOGLE_SHEET_ID)
    sheet = find_sheet(book)

    records = sheet.get_all_records()
    rows = []

    for raw in records:
        start = parse_dt(raw.get("Начала визита"))
        end = parse_dt(raw.get("Конец визита"))

        row = {
            "id": clean(raw.get("ИД")),
            "agent": clean(raw.get("Пользователь")),
            "zone": clean(raw.get("Рабочая зона")),
            "client": clean(raw.get("Клиент")),
            "visit_time": clean(raw.get("Время визита")),
            "start": start,
            "end": end,
            "gps_raw": clean(raw.get("Погрешность")),
            "gps": parse_gps(raw.get("Погрешность")),
            "photos": integer(raw.get("Фото")),
        }
        rows.append(row)

    # Agent bo'yicha vaqt tartibida
    groups = defaultdict(list)
    for row in rows:
        if row["start"]:
            groups[row["agent"]].append(row)

    for agent_rows in groups.values():
        agent_rows.sort(key=lambda x: x["start"])

        previous = None

        for row in agent_rows:
            row["gap_minutes"] = None
            row["previous_client"] = ""
            row["previous_end"] = None

            if (
                previous
                and previous["end"]
                and row["start"]
                and previous["end"].date() == row["start"].date()
            ):
                gap = (
                    row["start"] - previous["end"]
                ).total_seconds() / 60

                row["gap_minutes"] = round(gap, 1)
                row["previous_client"] = previous["client"]
                row["previous_end"] = previous["end"]

            previous = row

    log.info("Google Sheets: %s ta vizit yuklandi", len(rows))
    return rows


def get_rows(force=False):
    now = time.time()

    with DATA_LOCK:
        if (
            not force
            and DATA_CACHE["rows"] is not None
            and now - DATA_CACHE["loaded"] < CACHE_SECONDS
        ):
            return DATA_CACHE["rows"]

        rows = load_rows()
        DATA_CACHE["rows"] = rows
        DATA_CACHE["loaded"] = now
        return rows


# ============================================================
# ANALYTICS
# ============================================================

def rows_today(rows):
    today = datetime.now().date()
    return [r for r in rows if r["start"] and r["start"].date() == today]


def agent_names(rows):
    return sorted({
        r["agent"] for r in rows if r["agent"]
    })


def find_agent(rows, query):
    q = norm(query)

    if not q:
        return None

    # 1. Telegram mapping
    if q.isdigit() and q in AGENT_CHAT_MAP:
        mapped = norm(AGENT_CHAT_MAP[q])
        for name in agent_names(rows):
            if norm(name) == mapped:
                return name

    # 2. Exact
    for name in agent_names(rows):
        if norm(name) == q:
            return name

    # 3. Contains
    matches = [
        name for name in agent_names(rows)
        if q in norm(name) or norm(name) in q
    ]

    if len(matches) == 1:
        return matches[0]

    # 4. Token match
    qt = set(q.split())
    scored = []

    for name in agent_names(rows):
        nt = set(norm(name).split())
        score = len(qt & nt)

        if score:
            scored.append((score, name))

    if scored:
        scored.sort(reverse=True)
        if len(scored) == 1 or scored[0][0] > scored[1][0]:
            return scored[0][1]

    return None


def today_agent_stats(rows, agent=None):
    data = rows_today(rows)

    if agent:
        data = [r for r in data if norm(r["agent"]) == norm(agent)]

    total = len(data)
    photos_zero = sum(r["photos"] == 0 for r in data)
    gps_bad = sum(r["gps"] > MAX_GPS_METERS for r in data)
    long_gaps = [
        r for r in data
        if r["gap_minutes"] is not None
        and r["gap_minutes"] >= MAX_GAP_MINUTES
    ]

    max_gap = max(
        [r["gap_minutes"] for r in long_gaps],
        default=0,
    )

    return {
        "rows": data,
        "total": total,
        "photos_zero": photos_zero,
        "gps_bad": gps_bad,
        "long_gaps": long_gaps,
        "max_gap": max_gap,
    }


def all_agent_summary(rows):
    data = rows_today(rows)
    groups = defaultdict(list)

    for row in data:
        groups[row["agent"]].append(row)

    result = []

    for agent, items in groups.items():
        gaps = [
            x["gap_minutes"]
            for x in items
            if x["gap_minutes"] is not None
            and x["gap_minutes"] >= MAX_GAP_MINUTES
        ]

        result.append({
            "agent": agent,
            "visits": len(items),
            "photos_zero": sum(x["photos"] == 0 for x in items),
            "gps_bad": sum(x["gps"] > MAX_GPS_METERS for x in items),
            "long_gaps": len(gaps),
            "max_gap": max(gaps, default=0),
        })

    return sorted(
        result,
        key=lambda x: (
            -x["long_gaps"],
            -x["max_gap"],
            -x["photos_zero"],
        ),
    )


# ============================================================
# TELEGRAM DASHBOARD TEXT
# ============================================================

def header(title):
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 {title}\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


def dashboard(rows, agent=None):
    stats = today_agent_stats(rows, agent)

    title = "AGENT DASHBOARD" if agent else "BUGUNGI VIZIT DASHBOARD"

    lines = [
        header(title),
        f"📅 {datetime.now():%d.%m.%Y}",
    ]

    if agent:
        lines.append(f"👤 Agent: {agent}")

    lines += [
        "",
        f"🏪 Jami vizitlar: {stats['total']}",
        (
            f"📷 Fotosiz: {stats['photos_zero']} "
            f"({pct(stats['photos_zero'], stats['total'])}%)"
        ),
        f"📍 GPS muammosi: {stats['gps_bad']}",
        f"⏱ Uzun tanaffuslar: {len(stats['long_gaps'])}",
        f"🔴 Eng katta tanaffus: {duration_text(stats['max_gap'])}",
    ]

    if stats["long_gaps"]:
        lines += ["", "🔴 ENG KATTA VAQT YO'QOTISHLARI"]

        top = sorted(
            stats["long_gaps"],
            key=lambda x: x["gap_minutes"] or 0,
            reverse=True,
        )[:8]

        for i, row in enumerate(top, 1):
            lines.append(
                f"{i}. {duration_text(row['gap_minutes'])}\n"
                f"   🏪 {row['previous_client'] or 'Oldingi magazin'}\n"
                f"   ➜ {row['client'] or 'Keyingi magazin'}\n"
                f"   ⏰ {format_dt(row['previous_end'])} → "
                f"{format_dt(row['start'])}"
            )

    if stats["photos_zero"]:
        lines += [
            "",
            "📷 FOTO MUAMMOSI",
            f"Fotosiz vizitlar: {stats['photos_zero']} ta",
        ]

    if stats["gps_bad"]:
        lines += [
            "",
            "📍 GPS MUAMMOSI",
            f"{stats['gps_bad']} ta vizitda GPS xatosi "
            f"{MAX_GPS_METERS:g} metrdan katta.",
        ]

    return "\n".join(lines)


def top_agents_report(rows):
    data = all_agent_summary(rows)

    lines = [
        header("AGENTLAR BO'YICHA HISOBOT"),
        f"📅 {datetime.now():%d.%m.%Y}",
        "",
    ]

    if not data:
        return "\n".join(lines + ["Ma'lumot topilmadi."])

    for i, item in enumerate(data[:15], 1):
        lines.append(
            f"{i}. 👤 {item['agent']}\n"
            f"   🏪 Vizit: {item['visits']} | "
            f"📷 Fotosiz: {item['photos_zero']}\n"
            f"   ⏱ Uzun tanaffus: {item['long_gaps']} | "
            f"🔴 Max: {duration_text(item['max_gap'])}"
        )

    return "\n".join(lines)


def photos_report(rows):
    data = rows_today(rows)
    groups = defaultdict(list)

    for row in data:
        if row["photos"] == 0:
            groups[row["agent"]].append(row)

    total = sum(len(x) for x in groups.values())

    lines = [
        header("FOTOSIZ VIZITLAR"),
        f"📅 {datetime.now():%d.%m.%Y}",
        f"📷 Jami fotosiz: {total} ta",
        "",
    ]

    if not groups:
        return "\n".join(lines + ["✅ Fotosiz vizit yo'q."])

    for agent, items in sorted(
        groups.items(),
        key=lambda x: len(x[1]),
        reverse=True,
    ):
        lines.append(
            f"👤 {agent} — {len(items)} ta"
        )

        for row in items[:8]:
            lines.append(
                f"   • ID: {row['id']} | {row['client']}"
            )

        if len(items) > 8:
            lines.append(
                f"   ... yana {len(items) - 8} ta"
            )

        lines.append("")

    return "\n".join(lines)


def gaps_report(rows):
    data = [
        r for r in rows_today(rows)
        if r["gap_minutes"] is not None
        and r["gap_minutes"] >= MAX_GAP_MINUTES
    ]

    data.sort(
        key=lambda x: x["gap_minutes"] or 0,
        reverse=True,
    )

    lines = [
        header("VAQT YO'QOTISHLARI"),
        f"📅 {datetime.now():%d.%m.%Y}",
        f"🔴 {len(data)} ta muammoli tanaffus",
        "",
    ]

    if not data:
        return "\n".join(
            lines + ["✅ Belgilangan chegaradan katta tanaffus topilmadi."]
        )

    for i, row in enumerate(data[:20], 1):
        lines.append(
            f"{i}. 👤 {row['agent']}\n"
            f"   ⏱ Yo'qotish: {duration_text(row['gap_minutes'])}\n"
            f"   🏪 {row['previous_client'] or '—'}\n"
            f"   ➜ {row['client'] or '—'}\n"
            f"   ⏰ {format_dt(row['previous_end'])} → "
            f"{format_dt(row['start'])}"
        )
        lines.append("")

    return "\n".join(lines)


def problems_report(rows):
    data = rows_today(rows)
    problem = []

    for row in data:
        reasons = []

        if row["photos"] == 0:
            reasons.append("📷 foto")
        if row["gps"] > MAX_GPS_METERS:
            reasons.append("📍 GPS")
        if (
            row["gap_minutes"] is not None
            and row["gap_minutes"] >= MAX_GAP_MINUTES
        ):
            reasons.append("⏱ vaqt")

        if reasons:
            problem.append((row, reasons))

    lines = [
        header("MUAMMOLI VIZITLAR"),
        f"📅 {datetime.now():%d.%m.%Y}",
        f"🚨 Jami: {len(problem)} ta",
        "",
    ]

    if not problem:
        return "\n".join(lines + ["✅ Hozircha muammoli vizit yo'q."])

    for row, reasons in problem[:30]:
        lines.append(
            f"👤 {row['agent']}\n"
            f"🏪 {row['client']}\n"
            f"🆔 {row['id']}\n"
            f"⚠️ {', '.join(reasons)}"
        )
        lines.append("")

    return "\n".join(lines)


# ============================================================
# GEMINI
# ============================================================

def compact_data_for_ai(rows):
    today = rows_today(rows)

    result = []

    for row in today:
        result.append({
            "id": row["id"],
            "agent": row["agent"],
            "client": row["client"],
            "zone": row["zone"],
            "start": format_dt(row["start"]),
            "end": format_dt(row["end"]),
            "photos": row["photos"],
            "gps_m": row["gps"],
            "gap_minutes": row["gap_minutes"],
            "previous_client": row["previous_client"],
        })

    # Juda katta prompt bo'lib ketmasligi uchun oxirgi 500 ta.
    return result[-500:]


def gemini_answer(question, rows, user_scope_agent=None):
    if not GEMINI_API_KEY:
        return (
            "⚠️ GEMINI_API_KEY sozlanmagan.\n"
            "Railway → Variables bo'limiga GEMINI_API_KEY qo'shing."
        )

    data_rows = rows

    if user_scope_agent:
        data_rows = [
            r for r in rows
            if norm(r["agent"]) == norm(user_scope_agent)
        ]

    payload_data = compact_data_for_ai(data_rows)

    system = """
Sen Visit Bot uchun professional data analystsan.

Vazifa:
- Google Sheets'dagi vizit ma'lumotlari asosida savolga javob ber.
- Faqat berilgan ma'lumotlardan foydalan.
- Raqamni o'zingdan to'qima.
- Agent nomini aniq yoz.
- Agar savolga javob uchun ma'lumot yetarli bo'lmasa, buni ayt.
- Vaqt yo'qotishini hisoblash kerak bo'lsa:
  oldingi magazin tugagan vaqtdan keyingi magazin boshlangan vaqtgacha hisobla.
- Natijani Telegram uchun juda tushunarli plain text ko'rinishida yoz.
- HTML tag ishlatma.
- Markdown ishlatma.
- ** belgilarini ishlatma.
- Hech qachon \\1, \\2 yoki regex replacement belgilarini yozma.
- Uzun gap emas, qisqa bloklar va punktlardan foydalan.
- Eng muhim raqamlarni birinchi ko'rsat.
"""

    prompt = (
        system
        + "\n\nBUGUNGI MA'LUMOT:\n"
        + json.dumps(payload_data, ensure_ascii=False)
        + "\n\nFOYDALANUVCHI SAVOLI:\n"
        + question
    )

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )

    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 1800,
        },
    }

    try:
        response = requests.post(
            url,
            params={"key": GEMINI_API_KEY},
            json=body,
            timeout=35,
        )

        if response.status_code != 200:
            log.error(
                "Gemini HTTP %s: %s",
                response.status_code,
                response.text[:500],
            )
            return (
                "⚠️ Gemini javob bera olmadi.\n"
                f"HTTP: {response.status_code}"
            )

        result = response.json()

        text = (
            result.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            .strip()
        )

        if not text:
            return "⚠️ Gemini bo'sh javob qaytardi."

        # Fallback: model tasodifan Markdown/HTML yuborsa ham
        # Telegram parser ishlatilmagani uchun xabar buzilmaydi.
        text = text.replace("```", "")
        text = text.replace("**", "")
        text = text.replace("__", "")

        return text

    except Exception as exc:
        log.exception("Gemini xatosi")
        return f"⚠️ AI xatosi: {exc}"


# ============================================================
# TELEGRAM
# ============================================================

TG_URL = (
    "https://api.telegram.org/bot"
    + TELEGRAM_BOT_TOKEN
)


def telegram_call(method, payload=None):
    response = requests.post(
        f"{TG_URL}/{method}",
        json=payload or {},
        timeout=40,
    )
    return response.json()


def send_message(chat_id, text):
    # Telegram 4096 limit.
    text = str(text or "").strip()

    if not text:
        return

    chunks = []

    while len(text) > 3900:
        cut = text.rfind("\n", 0, 3900)
        if cut < 1000:
            cut = 3900

        chunks.append(text[:cut])
        text = text[cut:].lstrip()

    chunks.append(text)

    for chunk in chunks:
        telegram_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
                # parse_mode YO'Q.
                # Bu juda muhim: HTML/Markdown sababli format buzilmaydi.
                "disable_web_page_preview": True,
            },
        )


def user_agent_scope(chat):
    chat_id = str(chat.get("id", ""))

    # Manager barcha ma'lumotni ko'radi.
    if MANAGER_CHAT_ID and chat_id == MANAGER_CHAT_ID:
        return None

    if chat_id in AGENT_CHAT_MAP:
        return AGENT_CHAT_MAP[chat_id]

    # Agar mapping bo'lmasa, Telegram ismi bo'yicha topishga harakat qilamiz.
    full_name = " ".join(
        x for x in [
            chat.get("first_name", ""),
            chat.get("last_name", ""),
        ]
        if x
    )

    return full_name.strip() or None


# ============================================================
# COMMANDS
# ============================================================

def command_help():
    return """━━━━━━━━━━━━━━━━━━━━
🤖 VIZIT BOT
━━━━━━━━━━━━━━━━━━━━

📊 /dashboard
Bugungi umumiy dashboard

👤 /my
Sizga tegishli agent hisoboti

📷 /photos
Fotosiz vizitlar

⏱ /gaps
Vizitlar orasidagi vaqt yo'qotishlari

🚨 /problems
Barcha muammoli vizitlar

🏆 /top
Agentlar bo'yicha umumiy jadval

🔄 /refresh
Google Sheets ma'lumotini yangilash

ℹ️ /status
Bot holati

💬 Oddiy savol ham berishingiz mumkin.

Masalan:
"Bugun eng ko'p vaqt yo'qotgan agent kim?"

"Xoshimov Shaxriyorning fotosiz vizitlari nechta?"

"Qaysi agentda 30 daqiqadan katta tanaffus bor?"

"Bugun nechta muammoli vizit bor?"
"""


def status_report(rows):
    return f"""━━━━━━━━━━━━━━━━━━━━
🤖 BOT STATUS
━━━━━━━━━━━━━━━━━━━━

🟢 Bot: ishlayapti
📊 Yuklangan vizitlar: {len(rows)}
📅 Bugungi vizitlar: {len(rows_today(rows))}
⏱ Tekshiruv intervali: {CHECK_INTERVAL_SECONDS // 60} daqiqa
🧠 Gemini: {"ulangan" if GEMINI_API_KEY else "ulanmagan"}
📄 Google Sheets: ulangan
"""


# ============================================================
# TELEGRAM UPDATE LOOP
# ============================================================

def handle_update(update):
    message = update.get("message")

    if not message:
        return

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = clean(message.get("text"))

    if not chat_id or not text:
        return

    try:
        rows = get_rows()
    except Exception as exc:
        log.exception("Sheets xatosi")
        send_message(
            chat_id,
            "❌ Google Sheets ma'lumotini olishda xato:\n"
            + str(exc),
        )
        return

    if text.startswith("/start"):
        send_message(chat_id, command_help())
        return

    if text.startswith("/help"):
        send_message(chat_id, command_help())
        return

    if text.startswith("/refresh"):
        try:
            rows = get_rows(force=True)
            send_message(
                chat_id,
                f"🔄 Yangilandi.\n\n"
                f"📊 Jami vizit: {len(rows)}\n"
                f"📅 Bugun: {len(rows_today(rows))}",
            )
        except Exception as exc:
            send_message(chat_id, f"❌ Yangilash xatosi:\n{exc}")
        return

    if text.startswith("/status"):
        send_message(chat_id, status_report(rows))
        return

    scope = user_agent_scope(chat)

    if text.startswith("/dashboard"):
        send_message(
            chat_id,
            dashboard(rows, scope),
        )
        return

    if text.startswith("/my"):
        if not scope:
            send_message(
                chat_id,
                "👤 Sizga agent bog'lanmagan.\n\n"
                "Manager bo'lsangiz /dashboard dan foydalaning."
            )
        else:
            agent = find_agent(rows, scope)

            if not agent:
                send_message(
                    chat_id,
                    f"⚠️ Agent topilmadi:\n{scope}"
                )
            else:
                send_message(
                    chat_id,
                    dashboard(rows, agent),
                )
        return

    if text.startswith("/photos"):
        send_message(chat_id, photos_report(rows))
        return

    if text.startswith("/gaps"):
        send_message(chat_id, gaps_report(rows))
        return

    if text.startswith("/problems"):
        send_message(chat_id, problems_report(rows))
        return

    if text.startswith("/top"):
        send_message(chat_id, top_agents_report(rows))
        return

    # --------------------------------------------------------
    # ERKIN SAVOL
    # --------------------------------------------------------

    question = text

    # Agent oddiy savol bersa, faqat o'z ma'lumoti yuboriladi.
    scope_agent = None

    if scope:
        scope_agent = find_agent(rows, scope)

    answer = gemini_answer(
        question,
        rows,
        user_scope_agent=scope_agent,
    )

    if scope_agent:
        prefix = (
            f"👤 {scope_agent}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
        )
        answer = prefix + answer

    send_message(chat_id, answer)


def telegram_loop():
    log.info("Telegram polling ishga tushdi")

    offset = None

    while True:
        try:
            params = {
                "timeout": 50,
                "allowed_updates": ["message"],
            }

            if offset is not None:
                params["offset"] = offset

            response = requests.get(
                f"{TG_URL}/getUpdates",
                params=params,
                timeout=65,
            )

            data = response.json()

            if not data.get("ok"):
                description = data.get(
                    "description", "Telegram xatosi"
                )

                if "409" in str(data) or "Conflict" in description:
                    log.error(
                        "Telegram 409: boshqa bot instance ishlayapti."
                    )
                    time.sleep(5)
                    continue

                log.error("Telegram xatosi: %s", data)
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1

                try:
                    handle_update(update)
                except Exception:
                    log.exception("Update handling xatosi")

        except Exception:
            log.exception("Telegram polling xatosi")
            time.sleep(5)


# ============================================================
# PERIODIC CHECK
# ============================================================

LAST_PROBLEM_SIGNATURE = set()


def periodic_check():
    global LAST_PROBLEM_SIGNATURE

    while True:
        try:
            rows = get_rows(force=True)
            today = rows_today(rows)

            problems = []

            for row in today:
                reasons = []

                if row["photos"] == 0:
                    reasons.append("foto")

                if row["gps"] > MAX_GPS_METERS:
                    reasons.append("gps")

                if (
                    row["gap_minutes"] is not None
                    and row["gap_minutes"] >= MAX_GAP_MINUTES
                ):
                    reasons.append("vaqt")

                if reasons:
                    problems.append((row, reasons))

            signature = {
                (
                    x[0]["id"],
                    tuple(x[1]),
                )
                for x in problems
            }

            new_problems = signature - LAST_PROBLEM_SIGNATURE

            if new_problems:
                log.info(
                    "Yangi muammoli vizitlar: %s",
                    len(new_problems),
                )

            LAST_PROBLEM_SIGNATURE = signature

        except Exception:
            log.exception("Avtomatik tekshiruv xatosi")

        time.sleep(CHECK_INTERVAL_SECONDS)


# ============================================================
# MAIN
# ============================================================

def validate_config():
    missing = []

    if not GOOGLE_SHEET_ID:
        missing.append("GOOGLE_SHEET_ID")

    if not GOOGLE_CREDENTIALS_JSON and not os.path.exists(
        GOOGLE_CREDENTIALS_FILE
    ):
        missing.append(
            "GOOGLE_CREDENTIALS_JSON / credentials.json"
        )

    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")

    if missing:
        raise RuntimeError(
            "Quyidagi sozlamalar yetishmayapti: "
            + ", ".join(missing)
        )


def main():
    validate_config()

    log.info("Google credentials: Railway variable loaded.")
    log.info(
        "Bot ishga tushdi. Tekshiruv intervali: %s soniya",
        CHECK_INTERVAL_SECONDS,
    )

    # Google Sheets test
    rows = get_rows(force=True)

    log.info(
        "Boshlang'ich ma'lumot: %s ta vizit, bugun %s ta",
        len(rows),
        len(rows_today(rows)),
    )

    threading.Thread(
        target=periodic_check,
        daemon=True,
    ).start()

    telegram_loop()


if __name__ == "__main__":
    main()
'''

requirements = """requests>=2.32.0
gspread>=6.1.0
google-auth>=2.35.0
python-dotenv>=1.0.1
"""

readme = """# Visit Bot v2

Railway Variables:

GOOGLE_SHEET_ID
GOOGLE_CREDENTIALS_JSON
TELEGRAM_BOT_TOKEN
MANAGER_CHAT_ID
GEMINI_API_KEY

Optional:

GEMINI_MODEL=gemini-3.8-flash
VIZITLAR_SHEET_NAME=Vizitlar
MAX_POGRESHNOST_METERS=150
MAX_IDLE_GAP_MINUTES=15
CHECK_INTERVAL_SECONDS=1800
CACHE_SECONDS=60

Agent Telegram ID mapping example:

AGENT_CHAT_MAP={"123456789":"TP 7 Буранова Гулмира (NCF-CNF)"}

Telegram formatting intentionally uses plain text only.
No parse_mode is used.
"""

(base / "main.py").write_text(main_py, encoding="utf-8")
(base / "requirements.txt").write_text(requirements, encoding="utf-8")
(base / "README.txt").write_text(readme, encoding="utf-8")

py_compile.compile(str(base / "main.py"), doraise=True)

zip_path = Path("/mnt/data/visit_bot_v2.zip")
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(base / "main.py", "main.py")
    z.write(base / "requirements.txt", "requirements.txt")
    z.write(base / "README.txt", "README.txt")

print("Tayyor:", zip_path)
print("Syntax: OK")
