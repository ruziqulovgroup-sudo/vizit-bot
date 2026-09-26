"""
VIZIT TEKSHIRUV BOTI (v3)
==========================
Sizning tizimingizdagi "Визиты" jadvali quyidagi ustunlarga ega:
    Время визита | ИД | Рабочая зона | Пользователь | Начала визита |
    Конец визита | Погрешность | Клиент | Фото

Bu ma'lumotni Google Sheets'ga joylaysiz, bot esa har bir vizitni tekshiradi:

    1) Погрешность (GPS xatosi) MAX_POGRESHNOST_METERS dan katta bo'lsa -> XATO
    2) Фото soni 0 bo'lsa (rasm biriktirilmagan) -> XATO (agar yoqilgan bo'lsa)
    3) YANGI: Bitta do'kondan keyingi do'konga o'tish vaqti juda qisqa bo'lsa
       (MIN_TRAVEL_MINUTES dan kam) -> XATO — fizik jihatdan bunday tez
       borib bo'lmaydi, demak vizit soxta yoki oldindan bosilgan bo'lishi mumkin

Har bir xato uchun: qaysi agent (Пользователь), qaysi mijoz (Клиент),
qachon (Время визита), qancha metr/daqiqa xato ekani - Telegram orqali
menejerga hisobot qilib yuboriladi.

ISHGA TUSHIRISH:
    python main.py --once      # bir martalik tekshirish (sinov uchun)
    python main.py             # doimiy rejim (har CHECK_INTERVAL_SECONDS'da)
"""

import os
import re
import time
import logging
from datetime import datetime
from collections import defaultdict
import html

import json
import gspread
from google.oauth2.service_account import Credentials
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# SOZLAMALAR (.env fayldan o'qiladi)
# ---------------------------------------------------------------------------
load_dotenv()

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
# Ikki xil usulda berish mumkin:
#   1) GOOGLE_CREDENTIALS_FILE - kompyuterda/serverda faylga yo'l (masalan credentials.json)
#   2) GOOGLE_CREDENTIALS_JSON - Railway kabi joylarda, faylning ICHIDAGI matnni
#      to'g'ridan-to'g'ri shu o'zgaruvchiga joylashtirasiz (fayl shart emas)
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_CREDENTIALS")
VIZITLAR_SHEET_NAME = os.getenv("VIZITLAR_SHEET_NAME", "Визитлар")

# Railway compatibility:
# If credentials are supplied through an environment variable, also create
# credentials.json at runtime. This keeps both env-based and file-based
# credential code compatible.
if GOOGLE_CREDENTIALS_JSON and not os.path.exists(GOOGLE_CREDENTIALS_FILE):
    try:
        _credentials_info = json.loads(GOOGLE_CREDENTIALS_JSON)
        with open(GOOGLE_CREDENTIALS_FILE, "w", encoding="utf-8") as _f:
            json.dump(_credentials_info, _f)
        log_message = "Google credentials: Railway variable loaded."
    except Exception as _e:
        log_message = f"Google credentials variable invalid: {_e}"
else:
    log_message = "Google credentials: file mode."

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID")

# Gemini AI (ixtiyoriy, savollarga tabiiy tilda javob berish uchun)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "30"))

# Погрешность shu metrdan katta bo'lsa - xato
MAX_POGRESHNOST_METERS = float(os.getenv("MAX_POGRESHNOST_METERS", 150))

# Фото soni 0 bo'lsa ham xato deb hisoblansinmi? (true/false)
CHECK_ZERO_PHOTO = os.getenv("CHECK_ZERO_PHOTO", "true").lower() == "true"

# Bitta do'kondan keyingisiga o'tish uchun kamida shuncha daqiqa kerak
# (undan kam bo'lsa - "juda tez, real emas" deb hisoblanadi)
MIN_TRAVEL_MINUTES = float(os.getenv("MIN_TRAVEL_MINUTES", 3))

# Agentning bir magazindan chiqib, keyingi magazinga yetib borishida
# shuncha daqiqadan KO'P vaqt bo'sh qolsa, vaqt yo'qotilishi sifatida ko'rsatiladi.
MAX_IDLE_GAP_MINUTES = float(os.getenv("MAX_IDLE_GAP_MINUTES", 15))

# Sana/vaqt formati (tizimingizdagi format: "26.09.2026 12:14:18")
DATETIME_FORMAT = os.getenv("DATETIME_FORMAT", "%d.%m.%Y %H:%M:%S")

# Doimiy rejimda necha soniyada bir marta tekshirish
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", 1800))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)
log.info(log_message)

if not GOOGLE_CREDENTIALS_JSON and not os.path.exists(GOOGLE_CREDENTIALS_FILE):
    log.error(
        "Google credentials topilmadi. Railway Variables'da "
        "GOOGLE_CREDENTIALS yoki GOOGLE_CREDENTIALS_JSON bo'lishi kerak."
    )


# ---------------------------------------------------------------------------
# YORDAMCHI FUNKSIYALAR
# ---------------------------------------------------------------------------
def parse_pogreshnost(text) -> float:
    """
    '280 м.'        -> 280
    '3 м.'          -> 3
    '1 км. 914 м.'  -> 1914
    """
    if text is None:
        return 0.0
    text = str(text).strip().lower()
    if not text:
        return 0.0

    km_match = re.search(r"(\d+(?:[.,]\d+)?)\s*км", text)
    m_match = re.search(r"(\d+(?:[.,]\d+)?)\s*м(?!\w)", text)

    total_meters = 0.0
    if km_match:
        total_meters += float(km_match.group(1).replace(",", ".")) * 1000
    if m_match:
        total_meters += float(m_match.group(1).replace(",", "."))

    if not km_match and not m_match:
        num_match = re.search(r"(\d+(?:[.,]\d+)?)", text)
        if num_match:
            total_meters = float(num_match.group(1).replace(",", "."))

    return total_meters


def parse_datetime(text):
    """'26.09.2026 12:14:18' -> datetime object. Xato bo'lsa None qaytaradi."""
    if not text:
        return None
    text = str(text).strip()
    try:
        return datetime.strptime(text, DATETIME_FORMAT)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# GOOGLE SHEETS BILAN ULANISH
# ---------------------------------------------------------------------------
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
                "GOOGLE_CREDENTIALS JSON noto'g'ri formatda. "
                f"JSON xatosi: {e}"
            ) from e
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
            raise RuntimeError(
                "credentials.json topilmadi. Railway Variables'ga "
                "GOOGLE_CREDENTIALS nomli variable qo'shing va unga "
                "credentials.json ichidagi to'liq JSON matnini joylang."
            )
        creds = Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE,
            scopes=scopes
        )

    return gspread.authorize(creds)


def load_vizitlar(client):
    """Vizitlar varag'ini topadi. Railway variable orqali nom berish mumkin.
    Agar nom berilmagan bo'lsa, o'zbek/ruscha variantlarni navbat bilan tekshiradi.
    """
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    candidates = [
        VIZITLAR_SHEET_NAME,
        "Визитлар",
        "Vizitlar",
        "Визиты",
        "Визит",
    ]
    seen = set()
    for title in candidates:
        if not title or title in seen:
            continue
        seen.add(title)
        try:
            return spreadsheet.worksheet(title).get_all_records()
        except gspread.exceptions.WorksheetNotFound:
            continue
    mavjud = [ws.title for ws in spreadsheet.worksheets()]
    raise RuntimeError(
        "Vizitlar varag'i topilmadi. Mavjud varaqlar: " + ", ".join(mavjud)
    )


# ---------------------------------------------------------------------------
# TEKSHIRISH MANTIG'I
# ---------------------------------------------------------------------------
def load_and_parse_rows(client):
    """Barcha qatorlarni o'qiydi va kerakli maydonlarni ajratib beradi."""
    raw_rows = load_vizitlar(client)
    parsed = []
    for row in raw_rows:
        vizit_id = str(row.get("ИД", "")).strip()
        agent = str(row.get("Пользователь", "")).strip()
        mijoz = str(row.get("Клиент", "")).strip()
        zona = str(row.get("Рабочая зона", "")).strip()
        pogreshnost_raw = row.get("Погрешность", "")
        foto_raw = row.get("Фото", 0)

        start_dt = parse_datetime(row.get("Начала визита"))
        end_dt = parse_datetime(row.get("Конец визита"))
        vaqt_raw = str(row.get("Время визита", "")).strip()

        try:
            foto_soni = int(foto_raw)
        except (ValueError, TypeError):
            foto_soni = 0

        parsed.append({
            "id": vizit_id,
            "agent": agent,
            "mijoz": mijoz,
            "zona": zona,
            "vaqt_raw": vaqt_raw,
            "start_dt": start_dt,
            "end_dt": end_dt,
            "pogreshnost_m": parse_pogreshnost(pogreshnost_raw),
            "pogreshnost_raw": pogreshnost_raw,
            "foto_soni": foto_soni,
        })
    return parsed


def check_visits():
    client = get_sheet_client()
    rows = load_and_parse_rows(client)

    # id -> muammolar ro'yxati (bir vizitda bir nechta muammo bo'lishi mumkin)
    muammolar = defaultdict(list)
    row_by_id = {}

    for r in rows:
        row_by_id[r["id"]] = r

        if r["pogreshnost_m"] > MAX_POGRESHNOST_METERS:
            muammolar[r["id"]].append(
                f"GPS xatosi katta: {r['pogreshnost_raw']} (chegara: {int(MAX_POGRESHNOST_METERS)} m)"
            )

        if CHECK_ZERO_PHOTO and r["foto_soni"] == 0:
            muammolar[r["id"]].append("Rasm biriktirilmagan (Фото: 0)")

    # --- DO'KONDAN-DO'KONGA O'TISH VAQTINI TEKSHIRISH ---
    # Har bir agentning ketma-ket vizitlari orasidagi bo'sh vaqtni hisoblaymiz:
    # oldingi magazin tugagan vaqt -> keyingi magazin boshlangan vaqt.
    # Bir kun ichidagi vizitlarni solishtiramiz, tun bo'yicha katta gapni hisoblamaymiz.
    by_agent = defaultdict(list)
    for r in rows:
        if r["start_dt"] is not None:
            by_agent[r["agent"]].append(r)

    for agent, agent_rows in by_agent.items():
        agent_rows.sort(key=lambda r: r["start_dt"])
        for prev, curr in zip(agent_rows, agent_rows[1:]):
            if prev["end_dt"] is None or curr["start_dt"] is None:
                continue

            # Turli kunlarni solishtirmaymiz.
            if prev["end_dt"].date() != curr["start_dt"].date():
                continue

            gap_minutes = (curr["start_dt"] - prev["end_dt"]).total_seconds() / 60.0
            gap_seconds = max(0, int(round(gap_minutes * 60)))
            gap_h = gap_seconds // 3600
            gap_m = (gap_seconds % 3600) // 60
            gap_s = gap_seconds % 60
            if gap_h:
                gap_text = f"{gap_h} soat {gap_m} daqiqa {gap_s} soniya"
            elif gap_m:
                gap_text = f"{gap_m} daqiqa {gap_s} soniya"
            else:
                gap_text = f"{gap_s} soniya"

            if gap_minutes < 0:
                muammolar[curr["id"]].append(
                    f"Vaqt ziddiyati: '{curr['mijoz']}' vizit oldingi vizit "
                    f"('{prev['mijoz']}') tugashidan OLDIN boshlangan"
                )
            elif gap_minutes < MIN_TRAVEL_MINUTES:
                muammolar[curr["id"]].append(
                    f"O'tish vaqti juda qisqa: '{prev['mijoz']}' dan '{curr['mijoz']}'gacha "
                    f"atigi {gap_text} (chegara: {MIN_TRAVEL_MINUTES:.0f} daqiqa)"
                )
            elif gap_minutes >= MAX_IDLE_GAP_MINUTES:
                # Vaqt yo'qotilishi alohida muammo sifatida qayd qilinadi.
                muammolar[curr["id"]].append(
                    f"Vaqt yo'qotilishi: '{prev['mijoz']}' viziti {prev['end_dt'].strftime('%H:%M:%S')} da tugagan, "
                    f"'{curr['mijoz']}' viziti {curr['start_dt'].strftime('%H:%M:%S')} da boshlangan — "
                    f"oradagi bo'sh vaqt {gap_text} (chegara: {MAX_IDLE_GAP_MINUTES:.0f} daqiqa)"
                )

    # Natijani ro'yxat shakliga o'tkazish
    xatolar = []
    for vizit_id, sabablar in muammolar.items():
        r = row_by_id[vizit_id]
        xatolar.append({
            "id": r["id"],
            "vaqt": r["vaqt_raw"],
            "agent": r["agent"],
            "mijoz": r["mijoz"],
            "zona": r["zona"],
            "sabablar": sabablar,
        })

    return xatolar


# ---------------------------------------------------------------------------
# HISOBOT TUZISH VA YUBORISH
# ---------------------------------------------------------------------------
def build_report(xatolar):
    hozir = datetime.now().strftime("%d.%m.%Y %H:%M")

    if not xatolar:
        return (
            f"✅ <b>VIZIT TEKSHIRUVI</b>\n"
            f"🕐 {hozir}\n\n"
            "🎉 <b>Muammoli vizitlar topilmadi.</b>"
        )

    agent_guruh = defaultdict(list)
    for x in xatolar:
        agent_guruh[x["agent"] or "Noma'lum"].append(x)

    lines = [
        "🚨 <b>VIZIT TEKSHIRUVI</b>",
        f"🕐 {hozir}",
        f"🔴 <b>Muammoli vizitlar: {len(xatolar)} ta</b>",
        f"⏱ <b>Vaqt yo'qotish chegarasi:</b> {MAX_IDLE_GAP_MINUTES:.0f} daqiqa",
        "",
    ]

    for agent, items in sorted(agent_guruh.items()):
        lines.append(f"👤 <b>{html.escape(agent)}</b> — {len(items)} ta")
        for i, x in enumerate(items, 1):
            sabablar = "\n".join(f"      ⚠️ {html.escape(str(s))}" for s in x["sabablar"])
            lines.append(
                f"  <b>{i}.</b> 🏪 {html.escape(str(x['mijoz']))}\n"
                f"      🆔 {html.escape(str(x['id']))}\n"
                f"      🕐 {html.escape(str(x['vaqt']))}\n"
                f"      {sabablar}"
            )
        lines.append("")

    lines.append("💡 <i>Vaqt yo'qotilishi — oldingi magazin tugagan va keyingi magazin boshlangan vaqt orasidagi bo'sh vaqt.</i>")
    return "\n".join(lines)

def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    max_len = 3800
    parts = [text[i:i + max_len] for i in range(0, len(text), max_len)] or [text]
    for part in parts:
        resp = requests.post(url, data={
            "chat_id": MANAGER_CHAT_ID,
            "text": part,
            "parse_mode": "HTML",
        })
        if resp.status_code != 200:
            log.error(f"Telegramga yuborishda xato: {resp.text}")


# ---------------------------------------------------------------------------
# AI SAVOL-JAVOB
# ---------------------------------------------------------------------------
def normalize_name(text):
    text = str(text or "").lower().replace("ё", "е")
    return re.sub(r"[^a-zа-яё0-9]+", " ", text).strip()


def resolve_agent_for_user(message, rows):
    """Telegram profilidan agentni ehtiyotkorlik bilan aniqlash."""
    chat = message.get("chat") or {}
    first = normalize_name(chat.get("first_name", ""))
    last = normalize_name(chat.get("last_name", ""))
    username = normalize_name(chat.get("username", ""))
    profile = " ".join(x for x in [first, last, username] if x)
    if not profile:
        return None

    agents = sorted({r.get("agent", "").strip() for r in rows if r.get("agent", "").strip()})
    exact = []
    for agent in agents:
        a = normalize_name(agent)
        if a and (a == profile or a in profile or profile in a):
            exact.append(agent)
    if len(exact) == 1:
        return exact[0]

    profile_tokens = set(profile.split())
    scored = []
    for agent in agents:
        tokens = set(normalize_name(agent).split())
        common = len(profile_tokens & tokens)
        if common:
            scored.append((common, agent))
    if scored:
        scored.sort(reverse=True)
        best = scored[0]
        if len(scored) == 1 or best[0] > scored[1][0]:
            return best[1]
    return None


def compact_rows_for_ai(rows, agent_name=None, max_rows=700):
    if agent_name:
        filtered = [r for r in rows if r.get("agent") == agent_name]
    else:
        filtered = list(rows)

    # Eng yangi yozuvlarni oldinga qo'yamiz.
    filtered.sort(key=lambda r: r.get("start_dt") or datetime.min, reverse=True)
    filtered = filtered[:max_rows]

    # AI uchun ketma-ket vizitlar orasidagi vaqtni Python oldindan hisoblab beradi.
    # Shunda Gemini vaqtni taxmin qilmaydi, tayyor aniq qiymatni tushuntiradi.
    by_agent = defaultdict(list)
    for r in filtered:
        if r.get("start_dt") is not None:
            by_agent[r.get("agent", "")].append(r)

    idle_by_id = {}
    for agent, agent_rows in by_agent.items():
        agent_rows.sort(key=lambda x: x.get("start_dt") or datetime.min)
        for prev, curr in zip(agent_rows, agent_rows[1:]):
            if not prev.get("end_dt") or not curr.get("start_dt"):
                continue
            if prev["end_dt"].date() != curr["start_dt"].date():
                continue
            gap_seconds = int((curr["start_dt"] - prev["end_dt"]).total_seconds())
            if gap_seconds >= 0:
                idle_by_id[curr.get("id", "")] = {
                    "previous_client": prev.get("mijoz", ""),
                    "previous_end": prev["end_dt"].strftime(DATETIME_FORMAT),
                    "next_client": curr.get("mijoz", ""),
                    "next_start": curr["start_dt"].strftime(DATETIME_FORMAT),
                    "gap_minutes": round(gap_seconds / 60, 2),
                    "is_idle_problem": gap_seconds / 60 >= MAX_IDLE_GAP_MINUTES,
                    "is_too_short": gap_seconds / 60 < MIN_TRAVEL_MINUTES,
                }

    result = []
    for r in filtered:
        result.append({
            "id": r.get("id", ""),
            "agent": r.get("agent", ""),
            "client": r.get("mijoz", ""),
            "zone": r.get("zona", ""),
            "visit_time": r.get("vaqt_raw", ""),
            "start": r.get("start_dt").strftime(DATETIME_FORMAT) if r.get("start_dt") else "",
            "end": r.get("end_dt").strftime(DATETIME_FORMAT) if r.get("end_dt") else "",
            "gps_m": r.get("pogreshnost_m", 0),
            "gps_raw": str(r.get("pogreshnost_raw", "")),
            "photos": r.get("foto_soni", 0),
            "previous_visit_gap": idle_by_id.get(r.get("id", ""), {}),
        })
    return result


def ask_gemini(question, rows, agent_name=None):
    if not GEMINI_API_KEY:
        return (
            "⚠️ AI savol-javob hali yoqilmagan. Railway Variables'da "
            "GEMINI_API_KEY variable qo'shing."
        )

    data = compact_rows_for_ai(rows, agent_name=agent_name)
    today = datetime.now().strftime("%d.%m.%Y")
    scope = (
        f"Foydalanuvchi agent: {agent_name}. Faqat shu agent ma'lumotlari haqida javob ber."
        if agent_name
        else "Foydalanuvchi menejer. Barcha agentlar ma'lumotidan foydalanish mumkin."
    )

    system = f"""
Sen Telegramdagi Vizitlar analitika yordamchisisan.
Bugungi sana: {today}.
{scope}

Qoidalar:
1. Javobni faqat berilgan Google Sheets ma'lumotlariga tayab ber.
2. Savol oldindan kodga yozilmagan bo'lsa ham, ma'nosini tushunib javob ber.
3. 'bugun', 'kecha', 'shu oy', 'eng ko'p', 'nechta', 'qaysi klient' kabi savollarni ma'lumotdan hisobla.
4. Sonlarni aniq hisobla. Hisoblash imkoni bo'lmasa, buni ochiq ayt.
5. Ma'lumotda yo'q narsani o'ylab topma.
6. Telegram uchun qisqa, tushunarli o'zbek tilida yoz. Kerak bo'lsa ruscha mijoz/agent nomlarini aynan saqla.
7. Hisobot bo'lsa emoji va punktlardan foydalan. Juda uzun jadval chiqarmagin.
8. 'GPS xatosi' uchun gps_m, foto uchun photos ustunidan foydalan.
9. 'muammoli vizit' deganda GPS > MAX_POGRESHNOST_METERS yoki photos=0 yoki tekshiruvdagi vaqt muammolarini hisobga ol.
10. Agar foydalanuvchi "vaqt yo'qotilishi", "orasida qancha vaqt", "qaysi magazinlar orasida vaqt ketgan" deb so'rasa, har bir agentning ketma-ket vizitlarini tartib bilan ko'rib chiq: oldingi vizitning Konец визита va keyingi vizitning Начала визита orasini hisobla. MAX_IDLE_GAP_MINUTES dan katta bo'lsa muammo sifatida ko'rsat. Javobda oldingi magazin, tugagan vaqt, keyingi magazin, boshlangan vaqt va yo'qotilgan vaqtni ko'rsat.
11. Agar "ID bo'yicha" deyilsa, vizitning ИД maydonini ham javobga qo'sh.
12. Javob oxirida qisqa xulosa ber.

Tekshiruv chegaralari:
- GPS: {MAX_POGRESHNOST_METERS} metr
- Foto 0: {'ha' if CHECK_ZERO_PHOTO else 'yo\'q'}
- Minimal o'tish vaqti: {MIN_TRAVEL_MINUTES} daqiqa
- Vaqt yo'qotilishi chegarasi: {MAX_IDLE_GAP_MINUTES} daqiqa

Google Sheets ma'lumotlari JSON:
{json.dumps(data, ensure_ascii=False, default=str)}
"""

    # Gemini API REST: rasmiy generateContent endpoint.
    # API key faqat Railway Environment Variable orqali olinadi.
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": question}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1600,
        },
    }
    try:
        resp = requests.post(
            url,
            headers={
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=GEMINI_TIMEOUT,
        )
        if resp.status_code != 200:
            log.error(f"Gemini API xatosi: {resp.text[:1000]}")
            return f"⚠️ AI javobida xato: {resp.status_code}. Railway logini tekshiring."
        body = resp.json()
        candidates = body.get("candidates", [])
        if not candidates:
            return "⚠️ AI javob qaytarmadi. Savolni boshqacha yozib ko'ring."
        parts = candidates[0].get("content", {}).get("parts", [])
        answer = "\n".join(p.get("text", "") for p in parts if p.get("text"))
        return answer.strip() or "⚠️ AI bo'sh javob qaytardi."
    except requests.RequestException as e:
        log.exception("Gemini API tarmoq xatosi")
        return f"⚠️ AI bilan ulanishda xato: {e}"
    except Exception as e:
        log.exception("AI savol-javob xatosi")
        return f"⚠️ AI javobini tayyorlashda xato: {e}"


def answer_user_question(message, text):
    """Oddiy savolni Google Sheets + Gemini orqali javobga aylantiradi."""
    try:
        client = get_sheet_client()
        rows = load_and_parse_rows(client)
    except Exception as e:
        log.exception("Savol uchun Sheets o'qishda xato")
        return f"🚨 Ma'lumotlarni o'qib bo'lmadi: {e}"

    chat_id = (message.get("chat") or {}).get("id")
    is_manager = bool(MANAGER_CHAT_ID and str(chat_id) == str(MANAGER_CHAT_ID))
    agent_name = None if is_manager else resolve_agent_for_user(message, rows)

    if not is_manager and not agent_name:
        return (
            "⚠️ Telegram profilingizni agent bilan bog'lay olmadim.\n\n"
            "Iltimos, menejerga Telegram profilingizdagi ism/familiyangizni "
            "agent nomi bilan moslab berishni ayting."
        )

    return ask_gemini(text, rows, agent_name=agent_name)


# ---------------------------------------------------------------------------
# TELEGRAM BOT — oddiy javoblar (/start, salom va h.k.)
# ---------------------------------------------------------------------------
def telegram_polling():
    """
    Railway'da webhook'siz ishlaydi.
    Botga /start yoki oddiy 'salom' yozilsa javob beradi.
    """
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN topilmadi.")
        return

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    offset = 0

    # Eski update'larni qayta yubormaslik uchun ularni tashlab ketamiz.
    try:
        r = requests.get(
            f"{api_url}/getUpdates",
            params={"offset": -1, "timeout": 1},
            timeout=5,
        )
        data = r.json()
        if data.get("ok") and data.get("result"):
            offset = data["result"][-1]["update_id"] + 1
    except Exception as e:
        log.warning(f"Telegram boshlang'ich update tekshiruvi: {e}")

    log.info("Telegram polling ishga tushdi.")

    while True:
        try:
            response = requests.get(
                f"{api_url}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=40,
            )

            if response.status_code != 200:
                log.error(f"Telegram getUpdates xatosi: {response.text}")
                time.sleep(3)
                continue

            data = response.json()
            if not data.get("ok"):
                log.error(f"Telegram API xatosi: {data}")
                time.sleep(3)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1

                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                text = (message.get("text") or "").strip()

                if not chat_id:
                    continue

                # /start
                if text.lower().startswith("/start"):
                    reply = (
                        "👋 Assalomu alaykum!\n\n"
                        "🤖 Vizit tekshiruv boti ishlayapti.\n"
                        "Men Google Sheets'dagi vizitlarni tekshiraman.\n\n"
                        "📊 Tekshiruv avtomatik ravishda "
                        f"har {CHECK_INTERVAL_SECONDS // 60} daqiqada bajariladi."
                    )

                # Oddiy salomlashuv
                elif text.lower() in {
                    "salom", "salam", "hello", "hi",
                    "assalomu alaykum", "assalom"
                }:
                    reply = (
                        "👋 Va alaykum assalom!\n"
                        "🤖 Bot ishlayapti. Vizitlar tekshiruvi faol."
                    )

                elif text.lower() in {"/status", "status"}:
                    reply = (
                        "🟢 Bot ishlayapti.\n"
                        f"⏱ Tekshiruv intervali: {CHECK_INTERVAL_SECONDS // 60} daqiqa"
                    )

                elif text.lower() in {"/check", "check", "tekshir"}:
                    # Faqat menejer uchun qo'lda tekshiruv.
                    if MANAGER_CHAT_ID and str(chat_id) == str(MANAGER_CHAT_ID):
                        reply = "🔎 Vizit tekshiruvi boshlandi..."
                        try:
                            xatolar = check_visits()
                            report = build_report(xatolar)
                            send_telegram_message(report)
                            # Alohida status xabari
                            requests.post(
                                f"{api_url}/sendMessage",
                                data={
                                    "chat_id": chat_id,
                                    "text": f"✅ Tekshiruv tugadi. Muammoli vizitlar: {len(xatolar)}",
                                },
                                timeout=10,
                            )
                            continue
                        except Exception as e:
                            reply = f"🚨 Tekshiruv xatosi: {e}"
                    else:
                        reply = "⛔ Bu buyruq faqat menejer uchun."

                else:
                    # Endi kiritilmagan savollar ham AI orqali tahlil qilinadi.
                    reply = "⏳ Savolingizni tahlil qilyapman..."
                    try:
                        # Avval foydalanuvchiga kutish holatini yuboramiz.
                        requests.post(
                            f"{api_url}/sendChatAction",
                            data={"chat_id": chat_id, "action": "typing"},
                            timeout=5,
                        )
                        reply = answer_user_question(message, text)
                    except Exception as e:
                        log.exception("Savolga javob berishda xato")
                        reply = f"🚨 Savolni qayta ishlashda xato: {e}"

                requests.post(
                    f"{api_url}/sendMessage",
                    data={
                        "chat_id": chat_id,
                        "text": reply,
                    },
                    timeout=10,
                )

        except requests.RequestException as e:
            log.warning(f"Telegram polling tarmoq xatosi: {e}")
            time.sleep(3)
        except Exception:
            log.exception("Telegram polling paytida xato")
            time.sleep(3)


# ---------------------------------------------------------------------------
# ASOSIY ISHGA TUSHIRISH
# ---------------------------------------------------------------------------
def run_once():
    log.info("Tekshiruv boshlandi...")
    try:
        xatolar = check_visits()
        report = build_report(xatolar)
        send_telegram_message(report)
        log.info(f"Tekshiruv tugadi. Muammoli vizitlar: {len(xatolar)}")
    except Exception as e:
        log.exception("Tekshiruv paytida xato yuz berdi")
        try:
            send_telegram_message(f"🚨 Bot xatosi: {e}")
        except Exception:
            pass


def run_forever():
    log.info(f"Bot doimiy rejimda ishga tushdi. Har {CHECK_INTERVAL_SECONDS} soniyada tekshiradi.")

    # Telegram polling alohida oqimda ishlaydi.
    import threading
    telegram_thread = threading.Thread(target=telegram_polling, daemon=True)
    telegram_thread.start()

    # Asosiy oqim vizitlarni reja bo'yicha tekshiradi.
    while True:
        run_once()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    telegram_polling()
