"""
VIZIT BOT — Google Sheets + Telegram + OpenAI GPT-5 mini
---------------------------------------------
Funksiyalar:
1) Google Sheets'dagi Vizitlar varag'ini to'liq o'qiydi.
2) Bo'sh kataklar sabab qatorlarni yo'qotmaydi.
3) GPS, foto va vaqt muammolarini tekshiradi.
4) Agentlar orasidagi / agentning ketma-ket vizitlari orasidagi vaqt yo'qotilishini topadi.
5) Telegram orqali tabiiy tilda savollarga javob beradi.
6) Oddiy/statistik savollarga AI'siz javob berishga harakat qiladi.
7) Murakkab va erkin savollar uchun OpenAI GPT-5 mini ishlatiladi.
8) OpenAI 429 bo'lsa foydalanuvchiga aniq quota/rate-limit xabari beradi.

Railway Variables:
GOOGLE_SHEET_ID
GOOGLE_CREDENTIALS_JSON
TELEGRAM_BOT_TOKEN
MANAGER_CHAT_ID
OPENAI_API_KEY
OPENAI_MODEL                 (default: gpt-5-mini)

VIZITLAR_SHEET_NAME          (default: Vizitlar)
MAX_POGRESHNOST_METERS       (default: 150)
CHECK_ZERO_PHOTO             (default: true)
MIN_TRAVEL_MINUTES           (default: 3)
MAX_IDLE_GAP_MINUTES         (default: 20)
CHECK_INTERVAL_SECONDS       (default: 1800)
OPENAI_TIMEOUT               (default: 60)
"""

import os
import re
import time
import json
import logging
import threading
from datetime import datetime
from collections import defaultdict

import gspread
from google.oauth2.service_account import Credentials
import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ============================================================
# SETTINGS
# ============================================================

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_CREDENTIALS_JSON = (
    os.getenv("GOOGLE_CREDENTIALS_JSON")
    or os.getenv("GOOGLE_CREDENTIALS")
)

VIZITLAR_SHEET_NAME = os.getenv("VIZITLAR_SHEET_NAME", "Vizitlar")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "60"))

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
    os.getenv("MAX_IDLE_GAP_MINUTES", "20")
)
CHECK_INTERVAL_SECONDS = int(
    os.getenv("CHECK_INTERVAL_SECONDS", "1800")
)
DATETIME_FORMAT = os.getenv(
    "DATETIME_FORMAT",
    "%d.%m.%Y %H:%M:%S"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ============================================================
# HELPERS
# ============================================================

def normalize_header(value):
    """Ustun nomini qidirish uchun normallashtiradi."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip()).lower()


def get_value(row, *names, default=""):
    """
    Dict ichidan ustun nomini bir nechta variant bilan qidiradi.
    Masalan: ИД / ID / id.
    """
    normalized = {
        normalize_header(k): v
        for k, v in row.items()
    }

    for name in names:
        key = normalize_header(name)
        if key in normalized:
            return normalized[key]

    return default


def parse_pogreshnost(text) -> float:
    """
    '280 м.' -> 280
    '3 м.' -> 3
    '1 км. 914 м.' -> 1914
    """
    if text is None:
        return 0.0

    text = str(text).strip().lower()
    if not text:
        return 0.0

    km_match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*км",
        text
    )
    m_match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*м(?!\w)",
        text
    )

    total_meters = 0.0

    if km_match:
        total_meters += (
            float(km_match.group(1).replace(",", "."))
            * 1000
        )

    if m_match:
        total_meters += float(
            m_match.group(1).replace(",", ".")
        )

    if not km_match and not m_match:
        num_match = re.search(
            r"(\d+(?:[.,]\d+)?)",
            text
        )
        if num_match:
            total_meters = float(
                num_match.group(1).replace(",", ".")
            )

    return total_meters


def parse_number(value, default=0.0):
    if value is None:
        return default

    text = str(value).strip().replace(",", ".")

    if not text:
        return default

    try:
        return float(text)
    except ValueError:
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if match:
            try:
                return float(match.group())
            except ValueError:
                pass

    return default


def parse_datetime(text):
    """
    26.09.2026 12:14:18 -> datetime
    """
    if text is None:
        return None

    text = str(text).strip()
    if not text:
        return None

    formats = [
        DATETIME_FORMAT,
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    return None


def format_minutes(minutes):
    """12.5 -> 12 daqiqa 30 soniya"""
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


def safe_int(value, default=0):
    try:
        return int(float(str(value).replace(",", ".")))
    except (ValueError, TypeError):
        return default


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
                f"GOOGLE_CREDENTIALS_JSON noto'g'ri JSON: {e}"
            ) from e

        creds = Credentials.from_service_account_info(
            info,
            scopes=scopes
        )
    else:
        if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
            raise RuntimeError(
                "Google credentials topilmadi. Railway Variables'da "
                "GOOGLE_CREDENTIALS_JSON bo'lishi kerak."
            )

        creds = Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE,
            scopes=scopes
        )

    return gspread.authorize(creds)


def find_vizitlar_sheet(spreadsheet):
    """
    Varaq nomi ozgina farq qilsa ham topishga harakat qiladi.
    """
    candidates = [
        VIZITLAR_SHEET_NAME,
        "Vizitlar",
        "Визитлар",
        "Визиты",
        "Визит",
    ]

    seen = set()

    for title in candidates:
        if not title:
            continue

        title = str(title).strip()

        if title in seen:
            continue

        seen.add(title)

        try:
            return spreadsheet.worksheet(title)
        except gspread.exceptions.WorksheetNotFound:
            pass

    # Exact topilmadi — katta-kichik harf/bo'shliqni hisobga olmasdan
    target = normalize_header(VIZITLAR_SHEET_NAME)

    for ws in spreadsheet.worksheets():
        if normalize_header(ws.title) == target:
            return ws

    mavjud = [ws.title for ws in spreadsheet.worksheets()]

    raise RuntimeError(
        "Vizitlar varag'i topilmadi. "
        f"Qidirildi: {', '.join(candidates)}. "
        f"Mavjud varaqlar: {', '.join(mavjud)}"
    )


def load_vizitlar(client):
    """
    MUHIM:
    get_all_records() o'rniga get_all_values() ishlatiladi.

    Sababi: Google Sheets'da ba'zi qatorlarda bo'sh kataklar bo'lishi
    mumkin. get_all_values() barcha ishlatilgan qator/ustunlarni olib,
    header bilan o'zimiz dict qilamiz.

    Bu savol-javob uchun kerak bo'lgan barcha ustunlarni saqlaydi.
    """
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    sheet = find_vizitlar_sheet(spreadsheet)

    # Formula natijalari ham olinadi.
    values = sheet.get_all_values(
        value_render_option="FORMATTED_VALUE"
    )

    if not values:
        return []

    # Birinchi qator — header
    headers = [
        str(h).strip()
        for h in values[0]
    ]

    if not any(headers):
        raise RuntimeError(
            f"'{sheet.title}' varag'ining 1-qatori header emas."
        )

    rows = []

    for raw in values[1:]:
        # Qator uzunligi headerdan kam bo'lsa, oxirini bo'sh bilan to'ldiramiz.
        if len(raw) < len(headers):
            raw = list(raw) + [""] * (len(headers) - len(raw))

        # Agar Sheets qatori butunlay bo'sh bo'lsa, o'tkazib yuboramiz.
        if not any(str(x).strip() for x in raw):
            continue

        row = {}

        for index, header in enumerate(headers):
            if not header:
                continue
            row[header] = raw[index] if index < len(raw) else ""

        rows.append(row)

    log.info(
        "Google Sheets: '%s' -> %s ta qator, %s ta ustun",
        sheet.title,
        len(rows),
        len(headers),
    )

    return rows


# ============================================================
# PARSING
# ============================================================

def load_and_parse_rows(client):
    raw_rows = load_vizitlar(client)
    parsed = []

    for row_number, row in enumerate(raw_rows, start=2):
        vizit_id = str(
            get_value(row, "ИД", "ID", "Id", "id")
        ).strip()

        agent = str(
            get_value(
                row,
                "Пользователь",
                "Пользователь ",
                "Agent",
                "Агент"
            )
        ).strip()

        mijoz = str(
            get_value(
                row,
                "Клиент",
                "Клиент ",
                "Mijoz",
                "Client"
            )
        ).strip()

        zona = str(
            get_value(
                row,
                "Рабочая зона",
                "Рабочая зона ",
                "Zona"
            )
        ).strip()

        vaqt_raw = str(
            get_value(
                row,
                "Время визита",
                "Время визита ",
                "Дата визита",
                "Дата"
            )
        ).strip()

        start_raw = get_value(
            row,
            "Начала визита",
            "Начало визита",
            "Boshlanish vaqti"
        )

        end_raw = get_value(
            row,
            "Конец визита",
            "Tugash vaqti"
        )

        pogreshnost_raw = get_value(
            row,
            "Погрешность",
            "Погрешность ",
            "GPS",
            "GPS xatosi"
        )

        foto_raw = get_value(
            row,
            "Фото",
            "Фото ",
            "Photos",
            "photo"
        )

        start_dt = parse_datetime(start_raw)
        end_dt = parse_datetime(end_raw)

        foto_soni = safe_int(foto_raw, 0)

        parsed.append({
            "row_number": row_number,
            "id": vizit_id,
            "agent": agent,
            "mijoz": mijoz,
            "zona": zona,
            "vaqt_raw": vaqt_raw,
            "start_dt": start_dt,
            "end_dt": end_dt,
            "start_raw": str(start_raw or ""),
            "end_raw": str(end_raw or ""),
            "pogreshnost_m": parse_pogreshnost(
                pogreshnost_raw
            ),
            "pogreshnost_raw": str(
                pogreshnost_raw or ""
            ),
            "foto_soni": foto_soni,
            # AI uchun original barcha ustunlar
            "raw": row,
        })

    return parsed


# ============================================================
# ANALYTICS
# ============================================================

def calculate_idle_gaps(rows):
    """
    Har bir agentning ketma-ket vizitlari:
    oldingi vizit tugashi -> keyingi vizit boshlanishi.

    Masalan:
    10:30 tugadi
    11:17 boshlandi
    = 47 daqiqa yo'qolgan.

    MAX_IDLE_GAP_MINUTES dan katta bo'lsa muammo.
    """
    by_agent = defaultdict(list)

    for row in rows:
        if row["start_dt"] is not None:
            by_agent[row["agent"] or "Noma'lum"].append(row)

    gaps = []

    for agent, agent_rows in by_agent.items():
        agent_rows.sort(
            key=lambda x: x["start_dt"]
        )

        for prev, curr in zip(
            agent_rows,
            agent_rows[1:]
        ):
            if not prev["end_dt"] or not curr["start_dt"]:
                continue

            gap_minutes = (
                curr["start_dt"] - prev["end_dt"]
            ).total_seconds() / 60.0

            gaps.append({
                "agent": agent,
                "previous": prev,
                "current": curr,
                "gap_minutes": gap_minutes,
                "is_conflict": gap_minutes < 0,
                "is_short": (
                    0 <= gap_minutes < MIN_TRAVEL_MINUTES
                ),
                "is_long": (
                    gap_minutes >= MAX_IDLE_GAP_MINUTES
                ),
            })

    return gaps


def check_visits(rows=None):
    if rows is None:
        client = get_sheet_client()
        rows = load_and_parse_rows(client)

    muammolar = []
    gaps = calculate_idle_gaps(rows)

    for row in rows:
        sabablar = []

        if row["pogreshnost_m"] > MAX_POGRESHNOST_METERS:
            sabablar.append(
                f"GPS xatosi {row['pogreshnost_raw']} "
                f"(chegara {int(MAX_POGRESHNOST_METERS)} m)"
            )

        if CHECK_ZERO_PHOTO and row["foto_soni"] == 0:
            sabablar.append("Foto yo'q (Фото: 0)")

        if sabablar:
            muammolar.append({
                "id": row["id"],
                "agent": row["agent"],
                "mijoz": row["mijoz"],
                "vaqt": row["vaqt_raw"],
                "zona": row["zona"],
                "sabablar": sabablar,
            })

    # Vaqt muammolari
    for gap in gaps:
        prev = gap["previous"]
        curr = gap["current"]

        if gap["is_conflict"]:
            muammolar.append({
                "id": curr["id"],
                "agent": curr["agent"],
                "mijoz": curr["mijoz"],
                "vaqt": curr["vaqt_raw"],
                "zona": curr["zona"],
                "sabablar": [
                    f"Vaqt ziddiyati: oldingi vizit "
                    f"({prev['mijoz']}) tugashidan oldin "
                    f"keyingi vizit boshlangan"
                ],
            })

        elif gap["is_short"]:
            muammolar.append({
                "id": curr["id"],
                "agent": curr["agent"],
                "mijoz": curr["mijoz"],
                "vaqt": curr["vaqt_raw"],
                "zona": curr["zona"],
                "sabablar": [
                    f"O'tish vaqti juda qisqa: "
                    f"{prev['mijoz']} → {curr['mijoz']} "
                    f"({format_minutes(gap['gap_minutes'])})"
                ],
            })

    return muammolar, gaps


# ============================================================
# TELEGRAM REPORT
# ============================================================

def split_telegram_text(text, max_len=3800):
    if len(text) <= max_len:
        return [text]

    parts = []
    current = ""

    for line in text.splitlines(True):
        if len(current) + len(line) > max_len:
            if current:
                parts.append(current)
                current = ""
        current += line

    if current:
        parts.append(current)

    return parts


def send_to_chat(chat_id, text, parse_mode="HTML"):
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN yo'q.")
        return

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    for part in split_telegram_text(text):
        try:
            response = requests.post(
                url,
                data={
                    "chat_id": chat_id,
                    "text": part,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": "true",
                },
                timeout=15,
            )

            if response.status_code != 200:
                log.error(
                    "Telegram xatosi: %s",
                    response.text[:1000]
                )

        except requests.RequestException:
            log.exception("Telegramga yuborishda xato")


def build_report(muammolar, gaps):
    now = datetime.now().strftime(
        "%d.%m.%Y %H:%M"
    )

    if not muammolar:
        return (
            f"✅ <b>VIZIT TEKSHIRUVI</b>\n"
            f"🕒 {now}\n\n"
            f"Muammoli vizitlar topilmadi."
        )

    grouped = defaultdict(list)

    for item in muammolar:
        grouped[
            item["agent"] or "Noma'lum agent"
        ].append(item)

    long_gaps = [
        g for g in gaps
        if g["is_long"]
    ]

    lines = [
        "🚨 <b>VIZITLAR NAZORATI</b>",
        f"🕒 {now}",
        "",
        f"🔴 Muammoli vizitlar: <b>{len(muammolar)}</b>",
        f"⏱ Uzoq vaqt yo'qotishlari: "
        f"<b>{len(long_gaps)}</b>",
        "",
    ]

    for agent, items in grouped.items():
        lines.append(
            f"👤 <b>{agent}</b> — {len(items)} ta"
        )

        for item in items[:20]:
            lines.append(
                f"  • ID: <code>{item['id']}</code>"
            )
            lines.append(
                f"    🏪 {item['mijoz'] or 'Nomaʼlum'}"
            )
            if item["vaqt"]:
                lines.append(
                    f"    🕒 {item['vaqt']}"
                )

            for reason in item["sabablar"]:
                lines.append(
                    f"    ⚠️ {reason}"
                )

            lines.append("")

    if long_gaps:
        lines.append(
            "⏳ <b>ENG KATTA VAQT YO'QOTISHLARI</b>"
        )

        sorted_gaps = sorted(
            long_gaps,
            key=lambda x: x["gap_minutes"],
            reverse=True,
        )

        for gap in sorted_gaps[:15]:
            prev = gap["previous"]
            curr = gap["current"]

            lines.append(
                f"👤 <b>{gap['agent']}</b>\n"
                f"🏪 {prev['mijoz']} → {curr['mijoz']}\n"
                f"🕒 {prev['end_raw']} → {curr['start_raw']}\n"
                f"⏱ <b>{format_minutes(gap['gap_minutes'])}</b>"
            )
            lines.append("")

    return "\n".join(lines)


# ============================================================
# DATA SNAPSHOT FOR QUESTIONS
# ============================================================

def make_summary(rows, gaps):
    if not rows:
        return {
            "jami_vizit": 0,
            "agentlar": 0,
            "muammoli_gps": 0,
            "fotosiz": 0,
            "uzoq_vaqt_oraliqlari": 0,
        }

    agents = {
        r["agent"]
        for r in rows
        if r["agent"]
    }

    gps_bad = sum(
        1 for r in rows
        if r["pogreshnost_m"] > MAX_POGRESHNOST_METERS
    )

    no_photo = sum(
        1 for r in rows
        if r["foto_soni"] == 0
    )

    long_gaps = [
        g for g in gaps
        if g["is_long"]
    ]

    return {
        "jami_vizit": len(rows),
        "agentlar": len(agents),
        "muammoli_gps": gps_bad,
        "fotosiz": no_photo,
        "uzoq_vaqt_oraliqlari": len(long_gaps),
    }


def find_agent(rows, query):
    q = query.lower().strip()

    exact = []
    partial = []

    for row in rows:
        agent = row["agent"].strip()

        if not agent:
            continue

        a = agent.lower()

        if q == a:
            exact.append(agent)
        elif q in a:
            partial.append(agent)

    return list(dict.fromkeys(exact or partial))


def local_answer(question, rows, gaps):
    """
    Ko'p uchraydigan savollarni Gemini'siz javoblaydi.
    Bu 429 limitni kamaytiradi.
    """
    q = question.lower().strip()

    summary = make_summary(rows, gaps)

    # Jami vizit
    if (
        ("jami" in q or "umumiy" in q)
        and "vizit" in q
        and not any(x in q for x in ["agent", "kim", "qaysi"])
    ):
        return (
            "📊 <b>Umumiy vizitlar</b>\n\n"
            f"🔢 Jami: <b>{summary['jami_vizit']}</b> ta\n"
            f"👥 Agentlar: <b>{summary['agentlar']}</b> ta"
        )

    # Fotosiz
    if (
        ("foto" in q or "fotosiz" in q)
        and any(x in q for x in ["nechta", "soni", "qancha"])
    ):
        return (
            "📷 <b>Fotosiz vizitlar</b>\n\n"
            f"🔴 Foto yo'q: <b>{summary['fotosiz']}</b> ta"
        )

    # GPS
    if (
        "gps" in q
        and any(x in q for x in ["nechta", "soni", "qancha"])
    ):
        return (
            "📍 <b>GPS muammolari</b>\n\n"
            f"🔴 Chegaradan oshgan: "
            f"<b>{summary['muammoli_gps']}</b> ta\n"
            f"📏 Chegara: {int(MAX_POGRESHNOST_METERS)} m"
        )

    # Uzoq vaqt
    if (
        any(x in q for x in [
            "vaqt yo'qot",
            "vaqt yoqot",
            "orasida qancha vaqt",
            "orasidagi vaqt",
            "uzoq vaqt",
        ])
    ):
        long_gaps = sorted(
            [
                g for g in gaps
                if g["is_long"]
            ],
            key=lambda x: x["gap_minutes"],
            reverse=True,
        )

        if not long_gaps:
            return (
                "⏱ <b>Vaqt yo'qotilishi</b>\n\n"
                "Katta vaqt yo'qotilishi topilmadi."
            )

        lines = [
            "⏱ <b>ENG KATTA VAQT YO'QOTISHLARI</b>",
            "",
        ]

        for gap in long_gaps[:15]:
            lines.extend([
                f"👤 <b>{gap['agent']}</b>",
                f"🏪 {gap['previous']['mijoz']}",
                f"➡️ {gap['current']['mijoz']}",
                f"🕒 {gap['previous']['end_raw']} → "
                f"{gap['current']['start_raw']}",
                f"⏱ <b>{format_minutes(gap['gap_minutes'])}</b>",
                "",
            ])

        return "\n".join(lines)

    # Agent bo'yicha vizitlar
    if "agent" in q and "vizit" in q:
        # "agentlar bo'yicha" — barcha agentlar
        if "bo'yicha" in q or "buyicha" in q:
            counts = defaultdict(int)

            for row in rows:
                if row["agent"]:
                    counts[row["agent"]] += 1

            if counts:
                sorted_counts = sorted(
                    counts.items(),
                    key=lambda x: x[1],
                    reverse=True,
                )

                lines = [
                    "👥 <b>AGENTLAR BO'YICHA VIZITLAR</b>",
                    "",
                ]

                for agent, count in sorted_counts[:30]:
                    lines.append(
                        f"• {agent}: <b>{count}</b>"
                    )

                return "\n".join(lines)

    return None


# ============================================================
# OPENAI GPT-5 MINI
# ============================================================

def call_openai(question, rows, gaps):
    """Murakkab savollarni OpenAI GPT-5 mini orqali javoblaydi."""
    if not OPENAI_API_KEY:
        return (
            "⚠️ <b>OpenAI ulanmagan.</b>\n\n"
            "Railway Variables'da "
            "<code>OPENAI_API_KEY</code> variable qo'ying."
        )

    ai_data = compact_data_for_ai(rows, gaps)

    system = f"""
Sen Vizit nazorat tizimining AI yordamchisisan.

Javob tili: o'zbek tili.
Telegram uchun chiroyli, qisqa va tushunarli javob ber.
Kerak bo'lsa ruscha agent/mijoz nomlarini o'zgartirma.

MUHIM QOIDALAR:
1. Faqat berilgan Google Sheets ma'lumotlariga asoslan.
2. Ma'lumotda yo'q narsani o'ylab topma.
3. Savol Google Sheets ma'lumotiga tegishli bo'lsa, aniq hisobla.
4. Agent, mijoz, ID, sana, vaqt, foto va GPS qiymatlarini saqla.
5. "ID bo'yicha" so'ralsa ID ni ko'rsat.
6. "Vaqt yo'qotilishi" so'ralsa oldingi vizitning tugash vaqti bilan
   keyingi vizitning boshlanish vaqtini solishtir.
7. Katta vaqt yo'qotilishi chegarasi: {MAX_IDLE_GAP_MINUTES} daqiqa.
8. GPS muammo chegarasi: {MAX_POGRESHNOST_METERS} metr.
9. Foto 0 bo'lsa fotosiz deb hisobla.
10. Natijani bo'limlar, emoji va punktlar bilan ber.
11. Telegramda ulkan JSON yoki xom ma'lumot chiqarmasdan, natijani
    inson o'qishi uchun tushunarli shaklda ber.
12. Oxirida 1-2 qatorlik xulosa ber.
13. Agar savol ma'lumotda mavjud bo'lmagan mavzu haqida bo'lsa,
    buni ochiq ayt.
"""

    prompt = (
        f"Foydalanuvchi savoli:\n{question}\n\n"
        f"Google Sheets ma'lumotlari:\n"
        f"{json.dumps(ai_data, ensure_ascii=False, default=str)}"
    )

    try:
        client = OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT)
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=system,
            input=prompt,
        )

        answer = (response.output_text or "").strip()
        if answer:
            return answer

        return "⚠️ OpenAI bo'sh javob qaytardi. Savolni qisqaroq yozib ko'ring."

    except Exception as e:
        status = getattr(e, "status_code", None)
        log.exception("OpenAI API xatosi")

        if status == 429:
            return (
                "⏳ <b>OpenAI API limiti vaqtincha to'ldi.</b>\n\n"
                "Birozdan keyin qayta urinib ko'ring.\n"
                "Oddiy statistik savollar esa AI'siz ishlaydi."
            )

        if status == 401:
            return (
                "🔑 <b>OPENAI_API_KEY noto'g'ri.</b>\n\n"
                "Railway → Variables bo'limidagi API keyni tekshiring."
            )

        if status == 404:
            return (
                f"⚠️ <b>OpenAI model topilmadi.</b>\n\n"
                f"Model: <code>{OPENAI_MODEL}</code>\n"
                "OPENAI_MODEL qiymatini tekshiring."
            )

        return (
            "⚠️ <b>OpenAI API xatosi.</b>\n\n"
            f"Kod: <code>{status or 'unknown'}</code>\n"
            f"<i>{str(e)[:500]}</i>"
        )


# ============================================================
# QUESTION ENGINE
# ============================================================

def answer_question(question):
    client = get_sheet_client()
    rows = load_and_parse_rows(client)
    gaps = calculate_idle_gaps(rows)

    if not rows:
        return (
            "⚠️ Google Sheets'da Vizitlar ma'lumoti topilmadi."
        )

    # Avval Gemini'siz tez javob.
    local = local_answer(
        question,
        rows,
        gaps
    )

    if local:
        return local

    # Keyin OpenAI GPT-5 mini.
    return call_openai(
        question,
        rows,
        gaps
    )


# ============================================================
# TELEGRAM
# ============================================================

def telegram_api(method, params=None, timeout=40):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/{method}"
    )

    return requests.get(
        url,
        params=params or {},
        timeout=timeout,
    )


def telegram_send(chat_id, text):
    send_to_chat(
        chat_id,
        text,
        parse_mode="HTML"
    )


def telegram_polling():
    """
    Telegram polling.

    Eslatma:
    Railway'da ikki bir xil bot instance ishlasa:
    409 Conflict chiqadi.
    Bitta production instance qoldirilishi kerak.
    """
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN topilmadi.")
        return

    api_url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}"
    )

    # Eski update'larni tashlab ketamiz.
    offset = 0

    try:
        response = requests.get(
            f"{api_url}/getUpdates",
            params={
                "offset": -1,
                "timeout": 1,
            },
            timeout=5,
        )

        if response.status_code == 200:
            data = response.json()

            if data.get("ok") and data.get("result"):
                offset = (
                    data["result"][-1]["update_id"]
                    + 1
                )

    except Exception as e:
        log.warning(
            "Telegram initial update xatosi: %s",
            e
        )

    log.info(
        "Telegram polling ishga tushdi."
    )

    while True:
        try:
            response = requests.get(
                f"{api_url}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": '["message"]',
                },
                timeout=40,
            )

            if response.status_code != 200:
                log.error(
                    "Telegram getUpdates xatosi: %s",
                    response.text[:1000]
                )
                time.sleep(5)
                continue

            data = response.json()

            if not data.get("ok"):
                description = data.get(
                    "description",
                    str(data)
                )

                # 409 — boshqa instance polling qilmoqda.
                if "409" in str(
                    response.status_code
                ) or "Conflict" in description:
                    log.error(
                        "Telegram 409 Conflict: "
                        "boshqa bot instance ishlayapti."
                    )
                    time.sleep(10)
                    continue

                log.error(
                    "Telegram API: %s",
                    data
                )
                time.sleep(3)
                continue

            for update in data.get(
                "result",
                []
            ):
                offset = (
                    update["update_id"]
                    + 1
                )

                message = (
                    update.get("message")
                    or {}
                )

                chat = (
                    message.get("chat")
                    or {}
                )

                chat_id = chat.get("id")

                text = (
                    message.get("text")
                    or ""
                ).strip()

                if not chat_id or not text:
                    continue

                lower = text.lower()

                # ----------------------------------------
                # START
                # ----------------------------------------
                if lower.startswith("/start"):
                    reply = (
                        "👋 <b>Assalomu alaykum!</b>\n\n"
                        "🤖 <b>Vizit AI nazorat boti</b>\n\n"
                        "Men Google Sheets'dagi vizitlar "
                        "bo'yicha savollarga javob bera olaman.\n\n"
                        "Masalan:\n"
                        "• Bugun jami nechta vizit?\n"
                        "• Agentlar bo'yicha vizitlar nechta?\n"
                        "• Eng ko'p vaqt kimda yo'qolgan?\n"
                        "• TP 7 bo'yicha nechta vizit?\n"
                        "• ID 204027965 bo'yicha ma'lumot ber\n"
                        "• Fotosiz vizitlar nechta?\n"
                        "• Qaysi agentda GPS muammosi ko'p?\n\n"
                        "💡 Istalgan savolni oddiy tilda yozing."
                    )

                # ----------------------------------------
                # STATUS
                # ----------------------------------------
                elif lower in {
                    "/status",
                    "status",
                }:
                    try:
                        client = get_sheet_client()
                        rows = load_and_parse_rows(client)
                        gaps = calculate_idle_gaps(rows)

                        reply = (
                            "🟢 <b>BOT HOLATI</b>\n\n"
                            f"📊 Google Sheets qatorlari: "
                            f"<b>{len(rows)}</b>\n"
                            f"👥 Agentlar: "
                            f"<b>{make_summary(rows, gaps)['agentlar']}</b>\n"
                            f"⏱ Tekshiruv: "
                            f"har {CHECK_INTERVAL_SECONDS // 60} daqiqa\n"
                            f"🤖 OpenAI GPT-5 mini: "
                            f"<b>{'Ulangan' if OPENAI_API_KEY else 'Ulanmagan'}</b>"
                        )

                    except Exception as e:
                        reply = (
                            "🔴 <b>STATUS XATOSI</b>\n\n"
                            f"<code>{str(e)[:1000]}</code>"
                        )

                # ----------------------------------------
                # CHECK
                # ----------------------------------------
                elif lower in {
                    "/check",
                    "check",
                    "tekshir",
                }:
                    if (
                        MANAGER_CHAT_ID
                        and str(chat_id)
                        == str(MANAGER_CHAT_ID)
                    ):
                        telegram_send(
                            chat_id,
                            "🔎 Vizit tekshiruvi boshlandi..."
                        )

                        try:
                            client = get_sheet_client()
                            rows = load_and_parse_rows(
                                client
                            )

                            muammolar, gaps = check_visits(
                                rows
                            )

                            report = build_report(
                                muammolar,
                                gaps
                            )

                            telegram_send(
                                chat_id,
                                report
                            )

                        except Exception as e:
                            log.exception(
                                "Manual check xatosi"
                            )

                            telegram_send(
                                chat_id,
                                "🚨 <b>Tekshiruv xatosi</b>\n\n"
                                f"<code>{str(e)[:1500]}</code>"
                            )

                        continue

                    reply = (
                        "⛔ Bu buyruq faqat menejer uchun."
                    )

                # ----------------------------------------
                # HELP
                # ----------------------------------------
                elif lower in {
                    "/help",
                    "help",
                    "yordam",
                }:
                    reply = (
                        "🤖 <b>AI SAVOL-JAVOB</b>\n\n"
                        "Google Sheets ma'lumotlari bo'yicha "
                        "istalgan savolni yozing.\n\n"
                        "Misollar:\n"
                        "🔹 Bugun jami nechta vizit?\n"
                        "🔹 Eng ko'p vaqt kimda yo'qolgan?\n"
                        "🔹 TP 7 bo'yicha hisobot ber\n"
                        "🔹 ID 204027965 ni tekshir\n"
                        "🔹 Fotosiz vizitlar ro'yxati\n"
                        "🔹 GPS xatosi katta agentlar\n"
                    )

                # ----------------------------------------
                # ANY QUESTION
                # ----------------------------------------
                else:
                    # Oddiy savol: faqat savolga javob beramiz.
                    # Ortiqcha "tekshiryapman..." xabari yuborilmaydi.
                    try:
                        reply = answer_question(
                            text
                        )

                    except Exception as e:
                        log.exception(
                            "Savol-javob xatosi"
                        )

                        reply = (
                            "🚨 <b>Savolga javob berishda xato</b>\n\n"
                            f"<code>{str(e)[:1500]}</code>"
                        )

                telegram_send(
                    chat_id,
                    reply
                )

        except requests.RequestException as e:
            log.warning(
                "Telegram polling network xatosi: %s",
                e
            )
            time.sleep(5)

        except Exception:
            log.exception(
                "Telegram polling paytida xato"
            )
            time.sleep(5)


# ============================================================
# AUTOMATIC CHECK
# ============================================================

def run_once():
    log.info("Tekshiruv boshlandi...")

    try:
        client = get_sheet_client()
        rows = load_and_parse_rows(client)

        muammolar, gaps = check_visits(
            rows
        )

        report = build_report(
            muammolar,
            gaps
        )

        if MANAGER_CHAT_ID:
            telegram_send(
                MANAGER_CHAT_ID,
                report
            )

        log.info(
            "Tekshiruv tugadi. "
            "Qatorlar=%s, muammolar=%s, uzoq gaplar=%s",
            len(rows),
            len(muammolar),
            len([
                g for g in gaps
                if g["is_long"]
            ]),
        )

    except Exception as e:
        log.exception(
            "Tekshiruv paytida xato"
        )

        if MANAGER_CHAT_ID:
            telegram_send(
                MANAGER_CHAT_ID,
                "🚨 <b>BOT XATOSI</b>\n\n"
                f"<code>{str(e)[:1500]}</code>"
            )


def run_forever():
    """
    Telegram bot faqat foydalanuvchi savol/buyruq yuborganda ishlaydi.

    MUHIM:
    - Google Sheets avtomatik ravishda har 30 daqiqada tekshirilmaydi.
    - Bot o'zidan-o'zi hisobot yubormaydi.
    - /check buyrug'i yuborilgandagina to'liq vizit tekshiruvi ishlaydi.
    - Oddiy savol yuborilgandagina answer_question() ishlaydi.
    """
    log.info(
        "Bot ishga tushdi. Avtomatik hisobot o'chirilgan. "
        "Bot faqat Telegramdagi savol yoki buyruqqa javob beradi."
    )

    telegram_polling()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    import sys

    if "--once" in sys.argv:
        run_once()
    else:
        run_forever()
