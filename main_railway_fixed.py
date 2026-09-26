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
VIZITLAR_SHEET_NAME = os.getenv("VIZITLAR_SHEET_NAME", "Vizitlar")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID")

# Погрешность shu metrdan katta bo'lsa - xato
MAX_POGRESHNOST_METERS = float(os.getenv("MAX_POGRESHNOST_METERS", 150))

# Фото soni 0 bo'lsa ham xato deb hisoblansinmi? (true/false)
CHECK_ZERO_PHOTO = os.getenv("CHECK_ZERO_PHOTO", "true").lower() == "true"

# Bitta do'kondan keyingisiga o'tish uchun kamida shuncha daqiqa kerak
# (undan kam bo'lsa - "juda tez, real emas" deb hisoblanadi)
MIN_TRAVEL_MINUTES = float(os.getenv("MIN_TRAVEL_MINUTES", 3))

# Sana/vaqt formati (tizimingizdagi format: "26.09.2026 12:14:18")
DATETIME_FORMAT = os.getenv("DATETIME_FORMAT", "%d.%m.%Y %H:%M:%S")

# Doimiy rejimda necha soniyada bir marta tekshirish
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", 1800))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


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
        # Railway kabi joylarda: matn shaklidagi JSON'ni o'qiymiz (fayl kerak emas)
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        # Oddiy kompyuter/server: haqiqiy faylni o'qiymiz
        creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=scopes)
    return gspread.authorize(creds)


def load_vizitlar(client):
    sheet = client.open_by_key(GOOGLE_SHEET_ID).worksheet(VIZITLAR_SHEET_NAME)
    return sheet.get_all_records()


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

    # --- YANGI: do'kondan-do'konga o'tish vaqtini tekshirish ---
    by_agent = defaultdict(list)
    for r in rows:
        if r["start_dt"] is not None:
            by_agent[r["agent"]].append(r)

    for agent, agent_rows in by_agent.items():
        agent_rows.sort(key=lambda r: r["start_dt"])
        for prev, curr in zip(agent_rows, agent_rows[1:]):
            if prev["end_dt"] is None or curr["start_dt"] is None:
                continue
            gap_minutes = (curr["start_dt"] - prev["end_dt"]).total_seconds() / 60.0

            if gap_minutes < 0:
                muammolar[curr["id"]].append(
                    f"Vaqt ziddiyati: '{curr['mijoz']}' vizit oldingi vizit "
                    f"('{prev['mijoz']}') tugashidan OLDIN boshlangan"
                )
            elif gap_minutes < MIN_TRAVEL_MINUTES:
                muammolar[curr["id"]].append(
                    f"O'tish vaqti juda qisqa: '{prev['mijoz']}' dan '{curr['mijoz']}'gacha "
                    f"atigi {gap_minutes:.1f} daqiqa (chegara: {MIN_TRAVEL_MINUTES:.0f} daqiqa)"
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
    hozir = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not xatolar:
        return f"✅ <b>Vizit tekshiruvi</b> ({hozir})\nHech qanday xato topilmadi."

    agent_guruh = defaultdict(list)
    for x in xatolar:
        agent_guruh[x["agent"] or "Noma'lum"].append(x)

    lines = [f"📋 <b>Vizit tekshiruvi hisoboti</b> ({hozir})",
             f"Jami {len(xatolar)} ta muammoli vizit topildi:\n"]

    for agent, items in agent_guruh.items():
        lines.append(f"👤 <b>{agent}</b> — {len(items)} ta muammo")
        for x in items:
            sabab_matn = "\n     ⚠️ ".join(x["sabablar"])
            lines.append(
                f"   • ИД {x['id']} | {x['vaqt']} | Мижоз: {x['mijoz']}\n"
                f"     ⚠️ {sabab_matn}"
            )
        lines.append("")

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
                    reply = (
                        "🤖 Xabaringizni oldim.\n"
                        "Vizitlarni tekshirish uchun /check, "
                        "holatni ko'rish uchun /status yuboring."
                    )

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
    import sys
    if "--once" in sys.argv:
        run_once()
    else:
        run_forever()
