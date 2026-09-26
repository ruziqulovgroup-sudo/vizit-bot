from pathlib import Path

main_code = r'''"""
VIZIT AI ANALYTICS BOT — Railway / Google Sheets / Telegram / Gemini
====================================================================

Google Sheets ustunlari:
    Время визита | ИД | Рабочая зона | Пользователь | Начала визита |
    Конец визита | Погрешность | Клиент | Фото

Asosiy imkoniyatlar:
    - Google Sheets'dan vizitlarni o'qish
    - GPS, foto va vaqt oralig'i muammolarini aniqlash
    - Agentlar bo'yicha dashboard
    - Oldingi magazin tugashi -> keyingi magazin boshlanishi orasidagi
      vaqtni hisoblash
    - 15/30/60 daqiqadan ko'p vaqt yo'qotilgan holatlarni topish
    - Telegram'da erkin savollarga Gemini orqali javob
    - Savolni oldindan kodga yozish shart emas
    - Agent va menejer uchun turli ma'lumot doirasi
    - Google Sheets cache: har bir savolda qayta-qayta Sheets'ga urilmaydi
    - Telegram 409 Conflict holatini nazoratli qayta ulash
    - Railway environment variables bilan ishlaydi

ISHGA TUSHIRISH:
    python main.py
    python main.py --once

KERAKLI ENV:
    GOOGLE_SHEET_ID
    GOOGLE_CREDENTIALS_JSON
    TELEGRAM_BOT_TOKEN
    MANAGER_CHAT_ID
    GEMINI_API_KEY

Ixtiyoriy ENV:
    GEMINI_MODEL=gemini-3.8-flash
    VIZITLAR_SHEET_NAME=Vizitlar
    MAX_POGRESHNOST_METERS=150
    CHECK_ZERO_PHOTO=true
    MIN_TRAVEL_MINUTES=3
    MAX_IDLE_GAP_MINUTES=15
    CHECK_INTERVAL_SECONDS=1800
    DATA_CACHE_SECONDS=60
    MAX_AI_ROWS=250
"""

import os
import re
import sys
import time
import json
import html
import logging
import threading
from datetime import datetime, timedelta
from collections import defaultdict, Counter

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
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "35"))

VIZITLAR_SHEET_NAME = os.getenv(
    "VIZITLAR_SHEET_NAME", "Vizitlar"
).strip()

MAX_POGRESHNOST_METERS = float(
    os.getenv("MAX_POGRESHNOST_METERS", "150")
)

CHECK_ZERO_PHOTO = (
    os.getenv("CHECK_ZERO_PHOTO", "true").lower() == "true"
)

MIN_TRAVEL_MINUTES = float(
    os.getenv("MIN_TRAVEL_MINUTES", "3")
)

MAX_IDLE_GAP_MINUTES = float(
    os.getenv("MAX_IDLE_GAP_MINUTES", "15")
)

CHECK_INTERVAL_SECONDS = int(
    os.getenv("CHECK_INTERVAL_SECONDS", "1800")
)

DATA_CACHE_SECONDS = int(
    os.getenv("DATA_CACHE_SECONDS", "60")
)

MAX_AI_ROWS = int(
    os.getenv("MAX_AI_ROWS", "250")
)

DATETIME_FORMAT = os.getenv(
    "DATETIME_FORMAT", "%d.%m.%Y %H:%M:%S"
)

# 0 = no limit
MAX_REPORT_ITEMS = int(os.getenv("MAX_REPORT_ITEMS", "80"))

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("visit-bot")


# ============================================================
# GLOBAL CACHE / LOCKS
# ============================================================

_DATA_LOCK = threading.Lock()
_DATA_CACHE = {
    "rows": None,
    "loaded_at": 0.0,
    "sheet_title": None,
}

_CHECK_LOCK = threading.Lock()
_TELEGRAM_LOCK = threading.Lock()


# ============================================================
# BASIC HELPERS
# ============================================================

def esc(value) -> str:
    return html.escape(str(value or ""), quote=False)


def normalize_text(value) -> str:
    value = str(value or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", value).strip()


def normalize_name(value) -> str:
    value = normalize_text(value)
    return re.sub(r"[^a-zа-яё0-9]+", " ", value).strip()


def safe_float(value, default=0.0):
    try:
        return float(str(value).replace(",", ".").strip())
    except Exception:
        return default


def safe_int(value, default=0):
    try:
        return int(float(str(value).replace(",", ".").strip()))
    except Exception:
        return default


def format_minutes(minutes: float) -> str:
    if minutes is None:
        return "—"

    seconds = max(0, int(round(minutes * 60)))
    hours, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)

    if hours:
        return f"{hours} soat {mins} daqiqa"
    if mins:
        return f"{mins} daqiqa {secs} soniya"
    return f"{secs} soniya"


def parse_datetime(value):
    if not value:
        return None

    text = str(value).strip()

    formats = [
        DATETIME_FORMAT,
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass

    # ISO fallback
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except Exception:
        return None


def parse_pogreshnost(value) -> float:
    """
    280 м. -> 280
    1 км. 914 м. -> 1914
    """
    text = str(value or "").strip().lower()

    if not text:
        return 0.0

    km = re.search(r"(\d+(?:[.,]\d+)?)\s*км", text)
    meters = re.search(r"(\d+(?:[.,]\d+)?)\s*м(?!\w)", text)

    result = 0.0

    if km:
        result += safe_float(km.group(1)) * 1000

    if meters:
        result += safe_float(meters.group(1))

    if result == 0:
        number = re.search(r"(\d+(?:[.,]\d+)?)", text)
        if number:
            result = safe_float(number.group(1))

    return result


# ============================================================
# GOOGLE SHEETS
# ============================================================

def get_sheet_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]

    if GOOGLE_CREDENTIALS_JSON:
        try:
            info = json.loads(GOOGLE_CREDENTIALS_JSON)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                "GOOGLE_CREDENTIALS_JSON noto'g'ri JSON. "
                f"JSON xatosi: {e}"
            ) from e

        creds = Credentials.from_service_account_info(
            info,
            scopes=scopes,
        )
    else:
        if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
            raise RuntimeError(
                "Google credentials topilmadi. Railway Variables'da "
                "GOOGLE_CREDENTIALS_JSON ni to'liq JSON ko'rinishida "
                "qo'ying."
            )

        creds = Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE,
            scopes=scopes,
        )

    return gspread.authorize(creds)


def find_worksheet(spreadsheet):
    candidates = [
        VIZITLAR_SHEET_NAME,
        "Vizitlar",
        "Визитлар",
        "Визиты",
        "Визит",
    ]

    seen = set()

    for title in candidates:
        if not title or title in seen:
            continue

        seen.add(title)

        try:
            ws = spreadsheet.worksheet(title)
            return ws
        except gspread.exceptions.WorksheetNotFound:
            continue

    available = [ws.title for ws in spreadsheet.worksheets()]

    raise RuntimeError(
        "Vizitlar varag'i topilmadi. Mavjud varaqlar: "
        + ", ".join(available)
    )


def load_rows_from_sheets():
    client = get_sheet_client()
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    worksheet = find_worksheet(spreadsheet)

    raw = worksheet.get_all_records()

    parsed = []

    for row in raw:
        visit_id = str(row.get("ИД", "")).strip()
        agent = str(row.get("Пользователь", "")).strip()
        client_name = str(row.get("Клиент", "")).strip()
        zone = str(row.get("Рабочая зона", "")).strip()

        start = parse_datetime(row.get("Начала визита"))
        end = parse_datetime(row.get("Конец визита"))

        parsed.append(
            {
                "id": visit_id,
                "agent": agent,
                "client": client_name,
                "zone": zone,
                "visit_time_raw": str(
                    row.get("Время визита", "")
                ).strip(),
                "start": start,
                "end": end,
                "gps_m": parse_pogreshnost(
                    row.get("Погрешность", "")
                ),
                "gps_raw": str(
                    row.get("Погрешность", "")
                ).strip(),
                "photos": safe_int(row.get("Фото", 0)),
            }
        )

    calculate_visit_gaps(parsed)

    log.info(
        "Google Sheets: %s ta vizit yuklandi. Sheet=%s",
        len(parsed),
        worksheet.title,
    )

    return parsed, worksheet.title


def get_rows(force=False):
    now = time.time()

    with _DATA_LOCK:
        if (
            not force
            and _DATA_CACHE["rows"] is not None
            and now - _DATA_CACHE["loaded_at"] < DATA_CACHE_SECONDS
        ):
            return _DATA_CACHE["rows"]

        rows, sheet_title = load_rows_from_sheets()

        _DATA_CACHE["rows"] = rows
        _DATA_CACHE["loaded_at"] = now
        _DATA_CACHE["sheet_title"] = sheet_title

        return rows


# ============================================================
# VISIT ANALYTICS
# ============================================================

def calculate_visit_gaps(rows):
    """
    Har bir agent uchun:
        oldingi vizit tugashi -> keyingi vizit boshlanishi

    Natija curr['gap_minutes'] ichiga yoziladi.
    """

    by_agent = defaultdict(list)

    for row in rows:
        if row.get("start"):
            by_agent[row["agent"]].append(row)

    for agent_rows in by_agent.values():
        agent_rows.sort(key=lambda x: x["start"])

        previous = None

        for current in agent_rows:
            current["gap_minutes"] = None
            current["previous_client"] = ""
            current["previous_end"] = None
            current["gap_problem"] = False
            current["too_short"] = False

            if (
                previous
                and previous.get("end")
                and current.get("start")
                and previous["end"].date() == current["start"].date()
            ):
                gap = (
                    current["start"] - previous["end"]
                ).total_seconds() / 60

                current["gap_minutes"] = round(gap, 2)
                current["previous_client"] = previous["client"]
                current["previous_end"] = previous["end"]

                if gap >= MAX_IDLE_GAP_MINUTES:
                    current["gap_problem"] = True

                if 0 <= gap < MIN_TRAVEL_MINUTES:
                    current["too_short"] = True

            previous = current


def visit_has_problem(row):
    reasons = []

    if row["gps_m"] > MAX_POGRESHNOST_METERS:
        reasons.append("GPS")

    if CHECK_ZERO_PHOTO and row["photos"] == 0:
        reasons.append("Foto")

    if row.get("gap_problem"):
        reasons.append("Vaqt")

    if row.get("too_short"):
        reasons.append("Juda qisqa o'tish")

    if row.get("end") is None:
        reasons.append("Tugamagan vizit")

    return reasons


def get_problem_visits(rows):
    result = []

    for row in rows:
        reasons = visit_has_problem(row)

        if reasons:
            item = dict(row)
            item["reasons"] = reasons
            result.append(item)

    return result


def date_rows(rows, target_date):
    return [
        r for r in rows
        if r.get("start") and r["start"].date() == target_date
    ]


def today_rows(rows):
    return date_rows(rows, datetime.now().date())


def yesterday_rows(rows):
    return date_rows(
        rows,
        (datetime.now() - timedelta(days=1)).date(),
    )


def agent_names(rows):
    return sorted(
        {
            r["agent"].strip()
            for r in rows
            if r.get("agent", "").strip()
        }
    )


def resolve_agent_from_message(message, rows):
    """
    Agentni Telegram first_name/last_name/username orqali topishga
    harakat qiladi.
    """

    chat = message.get("chat") or {}

    profile_parts = [
        chat.get("first_name", ""),
        chat.get("last_name", ""),
        chat.get("username", ""),
    ]

    profile = normalize_name(
        " ".join(str(x) for x in profile_parts if x)
    )

    if not profile:
        return None

    agents = agent_names(rows)

    # 1. exact
    for agent in agents:
        if normalize_name(agent) == profile:
            return agent

    # 2. substring
    exactish = []

    for agent in agents:
        a = normalize_name(agent)

        if a and (a in profile or profile in a):
            exactish.append(agent)

    if len(exactish) == 1:
        return exactish[0]

    # 3. token score
    profile_tokens = set(profile.split())

    scored = []

    for agent in agents:
        tokens = set(normalize_name(agent).split())
        common = len(profile_tokens & tokens)

        if common:
            scored.append((common, agent))

    if scored:
        scored.sort(
            key=lambda x: (x[0], len(x[1])),
            reverse=True,
        )

        if len(scored) == 1:
            return scored[0][1]

        if scored[0][0] > scored[1][0]:
            return scored[0][1]

    return None


# ============================================================
# DASHBOARD DATA
# ============================================================

def agent_stats(rows, agent=None):
    if agent:
        rows = [
            r for r in rows
            if r["agent"] == agent
        ]

    total = len(rows)
    photos_zero = sum(r["photos"] == 0 for r in rows)
    gps_bad = sum(
        r["gps_m"] > MAX_POGRESHNOST_METERS
        for r in rows
    )
    gap_bad = sum(
        bool(r.get("gap_problem"))
        for r in rows
    )
    too_short = sum(
        bool(r.get("too_short"))
        for r in rows
    )
    unfinished = sum(
        r.get("end") is None
        for r in rows
    )

    gaps = [
        r["gap_minutes"]
        for r in rows
        if r.get("gap_minutes") is not None
        and r["gap_minutes"] >= 0
    ]

    total_lost = sum(
        x for x in gaps
        if x >= MAX_IDLE_GAP_MINUTES
    )

    max_gap = max(gaps, default=0)

    return {
        "total": total,
        "photos_zero": photos_zero,
        "gps_bad": gps_bad,
        "gap_bad": gap_bad,
        "too_short": too_short,
        "unfinished": unfinished,
        "total_lost_minutes": round(total_lost, 1),
        "max_gap_minutes": round(max_gap, 1),
    }


def build_agent_ranking(rows):
    result = []

    for agent in agent_names(rows):
        stats = agent_stats(rows, agent)
        result.append(
            {
                "agent": agent,
                **stats,
            }
        )

    return sorted(
        result,
        key=lambda x: x["total"],
        reverse=True,
    )


def build_gap_ranking(rows):
    result = []

    for row in rows:
        if (
            row.get("gap_minutes") is not None
            and row["gap_minutes"] >= MAX_IDLE_GAP_MINUTES
        ):
            result.append(row)

    return sorted(
        result,
        key=lambda x: x["gap_minutes"],
        reverse=True,
    )


def find_by_id(rows, text):
    ids = re.findall(r"\b\d{5,}\b", text)

    if not ids:
        return []

    wanted = set(ids)

    return [
        r for r in rows
        if r.get("id") in wanted
    ]


def find_by_client(rows, question):
    q = normalize_text(question)

    if len(q) < 4:
        return []

    candidates = []

    for row in rows:
        client = normalize_text(row.get("client", ""))

        if client and client in q:
            candidates.append(row)

    return candidates


def detect_date_filter(question):
    q = normalize_text(question)
    today = datetime.now().date()

    if "bugun" in q or "бугун" in q:
        return today

    if "kecha" in q or "кеча" in q:
        return today - timedelta(days=1)

    m = re.search(
        r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b",
        q,
    )

    if m:
        try:
            return datetime(
                int(m.group(3)),
                int(m.group(2)),
                int(m.group(1)),
            ).date()
        except ValueError:
            return None

    return None


# ============================================================
# TELEGRAM DASHBOARD
# ============================================================

def dashboard_header(title, subtitle=None):
    lines = [
        f"📊 <b>{esc(title)}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    if subtitle:
        lines.append(esc(subtitle))

    lines.append("")

    return lines


def format_agent_dashboard(rows, agent=None):
    if agent:
        title = f"{agent} — vizit dashboard"
        stats = agent_stats(rows, agent)
    else:
        title = "Umumiy vizit dashboard"
        stats = agent_stats(rows)

    scoped = [
        r for r in rows
        if not agent or r["agent"] == agent
    ]

    today = date_rows(scoped, datetime.now().date())

    today_stats = agent_stats(today)

    lines = dashboard_header(
        title,
        f"📅 Bugun: {datetime.now():%d.%m.%Y}",
    )

    lines.extend(
        [
            f"👣 <b>Jami vizit:</b> {stats['total']:,}".replace(",", " "),
            f"📅 <b>Bugungi vizit:</b> {today_stats['total']}",
            "",
            "🧩 <b>Muammolar</b>",
            f"📸 Fotosiz: <b>{stats['photos_zero']}</b>",
            f"📍 GPS xatosi: <b>{stats['gps_bad']}</b>",
            f"⏱ Vaqt yo'qotish: <b>{stats['gap_bad']}</b>",
            f"⚡ Juda qisqa o'tish: <b>{stats['too_short']}</b>",
            f"🔴 Tugamagan vizit: <b>{stats['unfinished']}</b>",
            "",
            "⏱ <b>Vaqt</b>",
            f"🔻 Jami yo'qotilgan vaqt: <b>{format_minutes(stats['total_lost_minutes'])}</b>",
            f"🔴 Eng katta tanaffus: <b>{format_minutes(stats['max_gap_minutes'])}</b>",
        ]
    )

    if agent:
        problem_count = sum(
            bool(visit_has_problem(r))
            for r in scoped
        )

        lines.extend(
            [
                "",
                f"🚨 <b>Muammoli vizitlar:</b> {problem_count}",
            ]
        )

    return "\n".join(lines)


def format_top_agents(rows, limit=10):
    ranking = build_agent_ranking(rows)[:limit]

    lines = dashboard_header(
        "ENG KO'P VIZIT QILGAN AGENTLAR",
        f"Top {len(ranking)}",
    )

    if not ranking:
        return "\n".join(
            lines + ["ℹ️ Ma'lumot topilmadi."]
        )

    medals = ["🥇", "🥈", "🥉"]

    for i, item in enumerate(ranking, 1):
        icon = medals[i - 1] if i <= 3 else f"{i}."

        lines.append(
            f"{icon} <b>{esc(item['agent'])}</b>\n"
            f"   👣 {item['total']} vizit"
        )

    return "\n".join(lines)


def format_top_problem_agents(rows, limit=10):
    ranking = []

    for agent in agent_names(rows):
        subset = [
            r for r in rows
            if r["agent"] == agent
        ]

        problems = sum(
            bool(visit_has_problem(r))
            for r in subset
        )

        if problems:
            ranking.append(
                {
                    "agent": agent,
                    "problems": problems,
                    "photos": sum(
                        r["photos"] == 0 for r in subset
                    ),
                    "gps": sum(
                        r["gps_m"] > MAX_POGRESHNOST_METERS
                        for r in subset
                    ),
                    "gaps": sum(
                        bool(r.get("gap_problem"))
                        for r in subset
                    ),
                }
            )

    ranking.sort(
        key=lambda x: x["problems"],
        reverse=True,
    )

    lines = dashboard_header(
        "MUAMMOLI AGENTLAR",
        f"Top {min(limit, len(ranking))}",
    )

    if not ranking:
        lines.append("✅ Muammoli agent topilmadi.")
        return "\n".join(lines)

    for i, item in enumerate(ranking[:limit], 1):
        lines.extend(
            [
                f"<b>{i}. {esc(item['agent'])}</b>",
                f"   🔴 Jami muammo: <b>{item['problems']}</b>",
                f"   📸 Fotosiz: {item['photos']}",
                f"   📍 GPS: {item['gps']}",
                f"   ⏱ Vaqt: {item['gaps']}",
                "",
            ]
        )

    return "\n".join(lines).rstrip()


def format_photo_dashboard(rows, limit=10):
    ranking = []

    for agent in agent_names(rows):
        subset = [
            r for r in rows
            if r["agent"] == agent
        ]

        count = sum(
            r["photos"] == 0
            for r in subset
        )

        if count:
            ranking.append(
                (
                    agent,
                    count,
                    len(subset),
                )
            )

    ranking.sort(
        key=lambda x: x[1],
        reverse=True,
    )

    total = sum(
        r["photos"] == 0
        for r in rows
    )

    lines = dashboard_header(
        "FOTOSIZ VIZITLAR",
        f"Jami: {total} ta",
    )

    if not ranking:
        lines.append("✅ Fotosiz vizit topilmadi.")
        return "\n".join(lines)

    for i, (agent, count, total_agent) in enumerate(
        ranking[:limit], 1
    ):
        percent = (
            count / total_agent * 100
            if total_agent else 0
        )

        lines.append(
            f"<b>{i}. {esc(agent)}</b>\n"
            f"   📸 Fotosiz: <b>{count}</b> / {total_agent} "
            f"({percent:.1f}%)"
        )

    return "\n".join(lines)


def format_gap_dashboard(rows, limit=10):
    gaps = build_gap_ranking(rows)[:limit]

    lines = dashboard_header(
        "VAQT YO'QOTISHLARI",
        f"Chegara: {MAX_IDLE_GAP_MINUTES:.0f} daqiqa",
    )

    if not gaps:
        lines.append("✅ Katta vaqt yo'qotilishi topilmadi.")
        return "\n".join(lines)

    for i, row in enumerate(gaps, 1):
        previous_end = (
            row["previous_end"].strftime("%H:%M:%S")
            if row.get("previous_end")
            else "—"
        )

        start = (
            row["start"].strftime("%H:%M:%S")
            if row.get("start")
            else "—"
        )

        lines.extend(
            [
                f"<b>{i}. {esc(row['agent'])}</b>",
                f"🏪 {esc(row['previous_client'])}",
                f"   ⏹ Tugadi: {previous_end}",
                f"🏪 {esc(row['client'])}",
                f"   ▶️ Boshlandi: {start}",
                f"🔴 Yo'qotilgan vaqt: "
                f"<b>{format_minutes(row['gap_minutes'])}</b>",
                f"🆔 {esc(row['id'])}",
                "",
            ]
        )

    return "\n".join(lines).rstrip()


def format_visit_details(rows, question):
    matches = find_by_id(rows, question)

    if not matches:
        matches = find_by_client(rows, question)

    if not matches:
        return None

    lines = dashboard_header(
        "VIZIT TAFSILOTI",
        f"Topildi: {len(matches)} ta",
    )

    for i, row in enumerate(matches[:10], 1):
        reasons = visit_has_problem(row)

        lines.extend(
            [
                f"<b>{i}. {esc(row['client'])}</b>",
                f"👤 Agent: {esc(row['agent'])}",
                f"🆔 ID: <code>{esc(row['id'])}</code>",
                f"📅 {esc(row['visit_time_raw'])}",
                f"▶️ Start: {row['start'].strftime('%d.%m.%Y %H:%M:%S') if row['start'] else '—'}",
                f"⏹ End: {row['end'].strftime('%d.%m.%Y %H:%M:%S') if row['end'] else '—'}",
                f"📍 GPS: {row['gps_m']:.0f} m",
                f"📸 Foto: {row['photos']}",
            ]
        )

        if row.get("gap_minutes") is not None:
            lines.append(
                f"⏱ Oldingi vizitdan tanaffus: "
                f"{format_minutes(row['gap_minutes'])}"
            )

        if reasons:
            lines.append(
                "⚠️ Muammo: " +
                ", ".join(esc(x) for x in reasons)
            )
        else:
            lines.append("✅ Muammo aniqlanmadi.")

        lines.append("")

    return "\n".join(lines).rstrip()


def format_check_report(rows):
    problems = get_problem_visits(rows)

    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    if not problems:
        return (
            "✅ <b>VIZIT TEKSHIRUVI</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {now}\n\n"
            "🎉 <b>Muammoli vizit topilmadi.</b>"
        )

    by_agent = defaultdict(list)

    for item in problems:
        by_agent[item["agent"] or "Noma'lum"].append(item)

    lines = [
        "🚨 <b>VIZIT TEKSHIRUVI</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🕐 {now}",
        f"🔴 Muammoli vizit: <b>{len(problems)}</b>",
        f"📸 Foto 0: {'Ha' if CHECK_ZERO_PHOTO else 'Yo‘q'}",
        f"📍 GPS chegara: {MAX_POGRESHNOST_METERS:.0f} m",
        f"⏱ Vaqt chegara: {MAX_IDLE_GAP_MINUTES:.0f} daqiqa",
        "",
    ]

    item_counter = 0

    for agent, items in sorted(
        by_agent.items(),
        key=lambda x: len(x[1]),
        reverse=True,
    ):
        lines.append(
            f"👤 <b>{esc(agent)}</b> — {len(items)} ta"
        )

        for item in items:
            if MAX_REPORT_ITEMS and item_counter >= MAX_REPORT_ITEMS:
                break

            item_counter += 1

            lines.extend(
                [
                    f"🏪 {esc(item['client'])}",
                    f"🆔 {esc(item['id'])}",
                    f"🕐 {esc(item['visit_time_raw'])}",
                    "⚠️ " + ", ".join(
                        esc(x) for x in item["reasons"]
                    ),
                    "",
                ]
            )

        if MAX_REPORT_ITEMS and item_counter >= MAX_REPORT_ITEMS:
            break

    if MAX_REPORT_ITEMS and len(problems) > MAX_REPORT_ITEMS:
        lines.append(
            f"ℹ️ Yana {len(problems) - MAX_REPORT_ITEMS} ta "
            "muammo qisqartirildi."
        )

    return "\n".join(lines)


# ============================================================
# AI DATA PACK
# ============================================================

def rows_for_ai(rows, agent=None, question=""):
    scoped = rows

    if agent:
        scoped = [
            r for r in rows
            if r["agent"] == agent
        ]

    date_filter = detect_date_filter(question)

    if date_filter:
        scoped = date_rows(scoped, date_filter)

    # ID bo'yicha savol bo'lsa faqat mos yozuvlar.
    id_matches = find_by_id(scoped, question)

    if id_matches:
        scoped = id_matches

    # Client nomi aniq kelsa.
    client_matches = find_by_client(scoped, question)

    if client_matches:
        scoped = client_matches

    scoped = sorted(
        scoped,
        key=lambda x: x["start"] or datetime.min,
        reverse=True,
    )

    # Savol umumiy bo'lsa, so'nggi yozuvlar yetarli bo'lmasligi
    # mumkin. Shu sababli aggregate + muammoli + recent yozuvlarni
    # birga beramiz.
    selected = []

    if id_matches or client_matches or date_filter:
        selected = scoped[:MAX_AI_ROWS]
    else:
        # Muammoli vizitlar
        problems = [
            r for r in scoped
            if visit_has_problem(r)
        ]

        # Eng katta gaplar
        gaps = [
            r for r in scoped
            if r.get("gap_minutes") is not None
            and r["gap_minutes"] >= MAX_IDLE_GAP_MINUTES
        ]

        gaps.sort(
            key=lambda x: x["gap_minutes"],
            reverse=True,
        )

        # Recent
        recent = scoped[:80]

        seen = set()

        for r in problems + gaps + recent:
            key = r.get("id") or id(r)

            if key not in seen:
                selected.append(r)
                seen.add(key)

            if len(selected) >= MAX_AI_ROWS:
                break

    return [
        {
            "id": r["id"],
            "agent": r["agent"],
            "client": r["client"],
            "zone": r["zone"],
            "visit_time": r["visit_time_raw"],
            "start": (
                r["start"].strftime(DATETIME_FORMAT)
                if r["start"] else ""
            ),
            "end": (
                r["end"].strftime(DATETIME_FORMAT)
                if r["end"] else ""
            ),
            "gps_m": round(r["gps_m"], 1),
            "photos": r["photos"],
            "gap_minutes": r.get("gap_minutes"),
            "previous_client": r.get("previous_client", ""),
            "previous_end": (
                r["previous_end"].strftime(DATETIME_FORMAT)
                if r.get("previous_end")
                else ""
            ),
            "gap_problem": bool(r.get("gap_problem")),
            "too_short": bool(r.get("too_short")),
            "problems": visit_has_problem(r),
        }
        for r in selected
    ]


def build_ai_summary(rows, agent=None):
    scoped = [
        r for r in rows
        if not agent or r["agent"] == agent
    ]

    stats = agent_stats(scoped)

    today = date_rows(scoped, datetime.now().date())

    ranking = build_agent_ranking(rows)[:15]
    gap_ranking = build_gap_ranking(scoped)[:20]

    return {
        "scope_agent": agent or "MANAGER_ALL_AGENTS",
        "total_rows": len(scoped),
        "today": agent_stats(today),
        "overall": stats,
        "agents_count": len(
            {
                r["agent"]
                for r in scoped
                if r["agent"]
            }
        ),
        "agent_ranking": ranking,
        "largest_time_losses": [
            {
                "agent": r["agent"],
                "id": r["id"],
                "previous_client": r["previous_client"],
                "previous_end": (
                    r["previous_end"].strftime(DATETIME_FORMAT)
                    if r.get("previous_end")
                    else ""
                ),
                "next_client": r["client"],
                "next_start": (
                    r["start"].strftime(DATETIME_FORMAT)
                    if r.get("start")
                    else ""
                ),
                "gap_minutes": r["gap_minutes"],
            }
            for r in gap_ranking
        ],
    }


# ============================================================
# GEMINI
# ============================================================

def gemini_request(system_text, user_text):
    if not GEMINI_API_KEY:
        return None, "GEMINI_API_KEY topilmadi."

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )

    payload = {
        "systemInstruction": {
            "parts": [
                {"text": system_text}
            ]
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": user_text}
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.15,
            "maxOutputTokens": 1800,
        },
    }

    try:
        response = requests.post(
            url,
            headers={
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=GEMINI_TIMEOUT,
        )

        if response.status_code != 200:
            log.error(
                "Gemini %s: %s",
                response.status_code,
                response.text[:1200],
            )
            return None, (
                f"Gemini API xatosi: {response.status_code}"
            )

        body = response.json()

        candidates = body.get("candidates") or []

        if not candidates:
            return None, "Gemini javob qaytarmadi."

        parts = (
            candidates[0]
            .get("content", {})
            .get("parts", [])
        )

        text = "\n".join(
            p.get("text", "")
            for p in parts
            if p.get("text")
        ).strip()

        if not text:
            return None, "Gemini bo'sh javob qaytardi."

        return text, None

    except requests.RequestException as e:
        log.exception("Gemini network error")
        return None, f"Gemini bilan ulanish xatosi: {e}"


def clean_ai_answer(text):
    if not text:
        return text

    # Gemini ba'zida Markdown yuboradi. Telegram HTML bilan
    # aralashmasligi uchun oddiy markdown belgilarini yumshatamiz.
    text = text.replace("```html", "")
    text = text.replace("```", "")
    text = text.strip()

    # Agar AI <...> ishlatsa, Telegram HTML bo'lishi mumkin.
    # Lekin noma'lum HTML teglarini buzmaslik uchun faqat xavfsiz
    # teglarni qoldiramiz.
    allowed = {
        "b", "strong", "i", "em", "u", "s",
        "code", "pre", "blockquote",
    }

    def replace_tag(match):
        slash = match.group(1) or ""
        tag = match.group(2).lower()

        if tag in allowed:
            return f"<{slash}{tag}>"

        return ""

    text = re.sub(
        r"<(/?)([a-zA-Z0-9]+)[^>]*>",
        replace_tag,
        text,
    )

    return text.strip()


def ask_gemini_about_visits(
    question,
    rows,
    agent=None,
):
    summary = build_ai_summary(rows, agent=agent)
    data = rows_for_ai(
        rows,
        agent=agent,
        question=question,
    )

    scope_text = (
        f"Bu savolni {agent} agent nomidan berilgan deb qabul qil. "
        "Faqat shu agent ma'lumotidan foydalan."
        if agent
        else
        "Foydalanuvchi menejer. Barcha agentlar bo'yicha ma'lumotdan foydalan."
    )

    system = f"""
Sen professional Telegram BI / Vizit Analytics yordamchisisan.

{scope_text}

Bugungi sana:
{datetime.now():%d.%m.%Y}

Sistemadagi ustunlar:
- ИД = vizit ID
- Пользователь = agent
- Клиент = magazin/mijoz
- Рабочая зона = zona
- Начала визита = vizit boshlanishi
- Конец визита = vizit tugashi
- Погрешность = GPS xatosi
- Фото = foto soni

Muhim hisoblash qoidalari:
- GPS muammo: {MAX_POGRESHNOST_METERS} metrdan katta.
- Fotosiz muammo: Фото = 0.
- Vaqt yo'qotish: oldingi vizitning Konец визита -> keyingi vizitning Начала визита.
- Vaqt yo'qotish muammosi: {MAX_IDLE_GAP_MINUTES} daqiqadan katta yoki teng.
- Juda qisqa o'tish: {MIN_TRAVEL_MINUTES} daqiqadan kichik.
- Tugamagan vizit: Конец визита yo'q.

QAT'IY QOIDALAR:
1. Faqat berilgan ma'lumotlardan foydalan.
2. Ma'lumotda yo'q narsani o'ylab topma.
3. Sonlarni o'zingcha taxmin qilma.
4. "Bugun", "kecha", "eng ko'p", "eng kam", "qaysi agent",
   "qaysi magazin", "qancha vaqt" kabi savollarni tushun.
5. Savol oldindan kodga yozilmagan bo'lsa ham, tabiiy tilda tushun.
6. Agar savol noaniq bo'lsa, mavjud ma'lumot asosida eng yaqin javobni ber,
   kerak bo'lsa bitta qisqa aniqlashtiruvchi savol so'ra.
7. Javob o'zbek tilida bo'lsin.
8. Agent/magazin nomlarini Sheets'dagi kabi saqla.
9. Telegram uchun mini-dashboard uslubida yoz.
10. Javobni qisqa, ammo mazmunli qil.
11. Kerak bo'lsa quyidagi bloklardan foydalan:
   📊 Sarlavha
   ━━━━━━━━━━━━━━━━━━━
   👤 Agent
   👣 Vizitlar
   📸 Foto
   📍 GPS
   ⏱ Vaqt
   🔴 Muammo
   ✅ Xulosa
12. Vaqt yo'qotish haqida savol bo'lsa:
   oldingi magazin + tugash vaqti + keyingi magazin +
   boshlanish vaqti + yo'qotilgan vaqtni ko'rsat.
13. ID bo'yicha savolda IDni ko'rsat.
14. Agar savol "eng faol agent", "eng ko'p vizit" bo'lsa rankingdan foydalan.
15. "eng muammoli agent" deyilganda muammo soni bo'yicha tushuntir,
   "eng yaxshi" yoki "eng yomon" degan bahoni o'zingcha bermagin.
16. Agar ma'lumot yetarli bo'lmasa, "ma'lumot yetarli emas" deb ayt.

AGGREGATE DASHBOARD:
{json.dumps(summary, ensure_ascii=False, default=str)}

SAVOLGA MOS TANLANGAN VIZITLAR:
{json.dumps(data, ensure_ascii=False, default=str)}
"""

    answer, error = gemini_request(
        system,
        question,
    )

    if error:
        return (
            "⚠️ <b>AI javobida muammo</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"{esc(error)}"
        )

    return clean_ai_answer(answer)


# ============================================================
# COMMAND HANDLERS
# ============================================================

def is_manager(chat_id):
    return (
        bool(MANAGER_CHAT_ID)
        and str(chat_id) == str(MANAGER_CHAT_ID)
    )


def start_message():
    return (
        "👋 <b>Assalomu alaykum!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 <b>Vizit AI Analytics</b> ishlayapti.\n\n"
        "Men Google Sheets'dagi vizitlarni tahlil qilaman.\n\n"
        "💬 Istalgan savolni oddiy tilda yozing:\n"
        "• Bugun nechta vizit qildim?\n"
        "• Eng ko'p vaqt qayerda yo'qoldi?\n"
        "• Fotosiz vizitlarim nechta?\n"
        "• ID 2040272965 bo'yicha nima bo'lgan?\n"
        "• Eng ko'p vizit qilgan agentlar?\n\n"
        "📌 <b>Buyruqlar:</b>\n"
        "/dashboard — umumiy dashboard\n"
        "/my — o'zingiz bo'yicha dashboard\n"
        "/photos — fotosiz vizitlar\n"
        "/gaps — vaqt yo'qotishlari\n"
        "/top — eng ko'p vizit qilganlar\n"
        "/problems — muammoli agentlar\n"
        "/status — bot holati\n"
        "/check — tekshiruv (menejer)"
    )


def status_message():
    cache_age = (
        time.time() - _DATA_CACHE["loaded_at"]
        if _DATA_CACHE["rows"] is not None
        else None
    )

    cache_text = (
        f"{cache_age:.0f} soniya"
        if cache_age is not None
        else "hali yuklanmagan"
    )

    return (
        "🟢 <b>BOT STATUS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Telegram: faol\n"
        f"🧠 Gemini: {'ulangan' if GEMINI_API_KEY else 'ulanmagan'}\n"
        f"📊 Google Sheets: "
        f"{'ulangan' if GOOGLE_SHEET_ID else 'ID yo‘q'}\n"
        f"💾 Cache yoshi: {cache_text}\n"
        f"⏱ Avtomatik tekshiruv: "
        f"{CHECK_INTERVAL_SECONDS // 60} daqiqa"
    )


def resolve_scope(message, rows):
    chat_id = (message.get("chat") or {}).get("id")

    if is_manager(chat_id):
        return None, True

    agent = resolve_agent_from_message(
        message,
        rows,
    )

    return agent, False


def handle_command(message, text, rows):
    command = normalize_text(text)

    if command.startswith("/start"):
        return start_message()

    if command in {"/status", "status"}:
        return status_message()

    if command in {"/dashboard", "dashboard"}:
        agent, manager = resolve_scope(message, rows)

        if not manager and not agent:
            return (
                "⚠️ <b>Agent aniqlanmadi.</b>\n"
                "Telegram profilingizdagi ism/familiya "
                "Google Sheets'dagi Пользователь bilan mos kelmadi."
            )

        return format_agent_dashboard(
            rows,
            agent=None if manager else agent,
        )

    if command in {"/my", "my"}:
        agent, manager = resolve_scope(message, rows)

        if manager:
            return format_agent_dashboard(rows)

        if not agent:
            return (
                "⚠️ Agentni aniqlay olmadim.\n"
                "Telegram profilingizdagi ism/familiyani "
                "Google Sheets'dagi Пользователь bilan moslang."
            )

        return format_agent_dashboard(
            rows,
            agent=agent,
        )

    if command in {"/photos", "photos"}:
        agent, manager = resolve_scope(message, rows)

        if not manager and not agent:
            return "⚠️ Agent aniqlanmadi."

        scoped = (
            rows if manager
            else [r for r in rows if r["agent"] == agent]
        )

        return format_photo_dashboard(scoped)

    if command in {"/gaps", "gaps"}:
        agent, manager = resolve_scope(message, rows)

        if not manager and not agent:
            return "⚠️ Agent aniqlanmadi."

        scoped = (
            rows if manager
            else [r for r in rows if r["agent"] == agent]
        )

        return format_gap_dashboard(scoped)

    if command in {"/top", "top"}:
        if not is_manager(
            (message.get("chat") or {}).get("id")
        ):
            return (
                "⛔ Bu dashboard faqat menejer uchun."
            )

        return format_top_agents(rows)

    if command in {"/problems", "problems"}:
        if not is_manager(
            (message.get("chat") or {}).get("id")
        ):
            return (
                "⛔ Bu dashboard faqat menejer uchun."
            )

        return format_top_problem_agents(rows)

    if command in {"/check", "check", "tekshir"}:
        chat_id = (message.get("chat") or {}).get("id")

        if not is_manager(chat_id):
            return "⛔ Bu buyruq faqat menejer uchun."

        if not _CHECK_LOCK.acquire(blocking=False):
            return "⏳ Tekshiruv allaqachon bajarilmoqda."

        try:
            fresh_rows = get_rows(force=True)
            return format_check_report(fresh_rows)
        except Exception as e:
            log.exception("Manual check error")
            return f"🚨 Tekshiruv xatosi: {esc(e)}"
        finally:
            _CHECK_LOCK.release()

    return None


# ============================================================
# TELEGRAM API
# ============================================================

def telegram_url(method):
    return (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/{method}"
    )


def telegram_send(chat_id, text):
    if not text:
        return

    # Telegram limit ~4096. 3800 xavfsizroq.
    max_len = 3800

    parts = []

    while len(text) > max_len:
        cut = text.rfind("\n", 0, max_len)

        if cut < 1000:
            cut = max_len

        parts.append(text[:cut])
        text = text[cut:].lstrip()

    parts.append(text)

    for part in parts:
        try:
            response = requests.post(
                telegram_url("sendMessage"),
                data={
                    "chat_id": chat_id,
                    "text": part,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
                timeout=15,
            )

            if response.status_code != 200:
                log.error(
                    "Telegram sendMessage xatosi: %s",
                    response.text[:1000],
                )

        except requests.RequestException:
            log.exception(
                "Telegram sendMessage network error"
            )


def telegram_typing(chat_id):
    try:
        requests.post(
            telegram_url("sendChatAction"),
            data={
                "chat_id": chat_id,
                "action": "typing",
            },
            timeout=5,
        )
    except Exception:
        pass


# ============================================================
# TELEGRAM POLLING
# ============================================================

def telegram_polling():
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN topilmadi.")
        return

    offset = 0

    # Eski update'larni o'tkazib yuborish.
    try:
        response = requests.get(
            telegram_url("getUpdates"),
            params={
                "offset": -1,
                "timeout": 1,
                "allowed_updates": json.dumps(["message"]),
            },
            timeout=5,
        )

        data = response.json()

        if data.get("ok") and data.get("result"):
            offset = (
                data["result"][-1]["update_id"] + 1
            )

    except Exception as e:
        log.warning(
            "Telegram initial update error: %s",
            e,
        )

    log.info("Telegram polling ishga tushdi.")

    while True:
        try:
            response = requests.get(
                telegram_url("getUpdates"),
                params={
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": json.dumps(
                        ["message"]
                    ),
                },
                timeout=40,
            )

            if response.status_code == 409:
                log.error(
                    "Telegram 409 Conflict: boshqa bot instance "
                    "getUpdates ishlatyapti. 20 soniya kutaman."
                )
                time.sleep(20)
                continue

            if response.status_code != 200:
                log.error(
                    "Telegram getUpdates %s: %s",
                    response.status_code,
                    response.text[:1000],
                )
                time.sleep(5)
                continue

            data = response.json()

            if not data.get("ok"):
                log.error(
                    "Telegram API error: %s",
                    data,
                )
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = (
                    update["update_id"] + 1
                )

                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                text = (
                    message.get("text") or ""
                ).strip()

                if not chat_id or not text:
                    continue

                try:
                    rows = get_rows()

                    reply = handle_command(
                        message,
                        text,
                        rows,
                    )

                    if reply is None:
                        # Oddiy savol -> Gemini.
                        agent, manager = resolve_scope(
                            message,
                            rows,
                        )

                        if not manager and not agent:
                            reply = (
                                "⚠️ <b>Agentni aniqlay olmadim.</b>\n"
                                "━━━━━━━━━━━━━━━━━━━━\n"
                                "Telegram profilingizdagi ism/familiya "
                                "Google Sheets'dagi Пользователь bilan "
                                "mos kelmadi.\n\n"
                                "Menejer sifatida barcha agentlar "
                                "bo'yicha savol berish uchun MANAGER_CHAT_ID "
                                "to'g'ri qo'yilganini tekshiring."
                            )
                        else:
                            telegram_typing(chat_id)

                            reply = ask_gemini_about_visits(
                                text,
                                rows,
                                agent=None if manager else agent,
                            )

                    telegram_send(
                        chat_id,
                        reply,
                    )

                except Exception as e:
                    log.exception(
                        "Update qayta ishlashda xato"
                    )

                    telegram_send(
                        chat_id,
                        "🚨 <b>Xatolik</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"{esc(e)}",
                    )

        except requests.RequestException as e:
            log.warning(
                "Telegram polling network error: %s",
                e,
            )
            time.sleep(5)

        except Exception:
            log.exception(
                "Telegram polling unexpected error"
            )
            time.sleep(5)


# ============================================================
# AUTOMATIC CHECK
# ============================================================

def run_once(send_report=True):
    log.info("Tekshiruv boshlandi...")

    try:
        rows = get_rows(force=True)
        report = format_check_report(rows)

        if send_report:
            telegram_send(
                MANAGER_CHAT_ID,
                report,
            )

        problems = len(
            get_problem_visits(rows)
        )

        log.info(
            "Tekshiruv tugadi. Muammoli vizitlar: %s",
            problems,
        )

        return report

    except Exception as e:
        log.exception(
            "Tekshiruv paytida xato"
        )

        if send_report and MANAGER_CHAT_ID:
            telegram_send(
                MANAGER_CHAT_ID,
                "🚨 <b>BOT XATOSI</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"{esc(e)}",
            )

        return None


def run_forever():
    log.info(
        "Bot doimiy rejimda. Interval=%s soniya.",
        CHECK_INTERVAL_SECONDS,
    )

    telegram_thread = threading.Thread(
        target=telegram_polling,
        daemon=True,
        name="telegram-polling",
    )

    telegram_thread.start()

    while True:
        try:
            if _CHECK_LOCK.acquire(blocking=False):
                try:
                    run_once(send_report=True)
                finally:
                    _CHECK_LOCK.release()
            else:
                log.info(
                    "Oldingi tekshiruv hali tugamagan."
                )

        except Exception:
            log.exception(
                "Main loop error"
            )

        time.sleep(CHECK_INTERVAL_SECONDS)


# ============================================================
# STARTUP VALIDATION
# ============================================================

def validate_config():
    missing = []

    if not GOOGLE_SHEET_ID:
        missing.append("GOOGLE_SHEET_ID")

    if not GOOGLE_CREDENTIALS_JSON and not os.path.exists(
        GOOGLE_CREDENTIALS_FILE
    ):
        missing.append(
            "GOOGLE_CREDENTIALS_JSON"
        )

    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")

    if not MANAGER_CHAT_ID:
        missing.append("MANAGER_CHAT_ID")

    if missing:
        log.warning(
            "Yetishmayotgan ENV: %s",
            ", ".join(missing),
        )

    if GEMINI_API_KEY:
        log.info(
            "Gemini: %s",
            GEMINI_MODEL,
        )
    else:
        log.warning(
            "GEMINI_API_KEY yo'q. Savol-javob AI ishlamaydi."
        )


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    validate_config()

    if "--once" in sys.argv:
        run_once(send_report=True)
    else:
        run_forever()
'''

requirements = """gspread
google-auth
requests
python-dotenv
"""

out_dir = Path("/mnt/data/visit_bot_rebuild")
out_dir.mkdir(parents=True, exist_ok=True)

main_path = out_dir / "main.py"
req_path = out_dir / "requirements.txt"

main_path.write_text(main_code, encoding="utf-8")
req_path.write_text(requirements, encoding="utf-8")

# Syntax check before giving the files to the user.
import py_compile
py_compile.compile(str(main_path), doraise=True)

print(f"Created: {main_path}")
print(f"Created: {req_path}")
print("Python syntax check: OK")
