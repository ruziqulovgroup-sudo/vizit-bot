# Vizit Tekshiruv Boti

Sizning tizimingizdagi "Визиты" jadvalini (skrinshotdagi ko'rinish) Google
Sheets'ga joylaysiz, bot esa har bir vizitni tekshiradi:

1. **Погрешность** (GPS xatosi) belgilangan chegaradan katta bo'lsa
2. **Фото** soni 0 bo'lsa (rasm biriktirilmagan)
3. **Bitta do'kondan keyingi do'konga o'tish vaqti** juda qisqa bo'lsa (masalan
   3 daqiqadan kam) — fizik jihatdan bunday tez borib bo'lmaydi, demak
   vizit soxta yoki oldindan tayyorlab bosilgan bo'lishi mumkin. Bot buni
   agentning barcha vizitlarini vaqt bo'yicha tartiblab, ketma-ket ikkita
   vizit orasidagi bo'shliqni hisoblash orqali aniqlaydi (oldingi vizitning
   "Конец визита" bilan keyingisining "Начала визита" orasidagi farq)

Muammoli vizitlar haqida (qaysi agent, qaysi mijoz, qachon, nima sabab)
Telegram orqali menejerga hisobot yuboriladi.

## 1-qadam: Ma'lumotni Google Sheets'ga joylash

Google Sheets faylida "Vizitlar" nomli varaq (list) yarating va tizimdagi
jadvalni AYNAN shu ustun nomlari bilan joylang (birinchi qator - sarlavha):

| Время визита | ИД | Рабочая зона | Пользователь | Начала визита | Конец визита | Погрешность | Клиент | Фото |
|---|---|---|---|---|---|---|---|---|
| 26.09.2026 12:14:18 | 204560243 | ТП 9 Сайдуллаева Садокат (NCF-CNF) | ТП 9 Сайдуллаева Садокат (NCF-CNF) | 26.09.2026 12:12:53 | 26.09.2026 12:14:16 | 280 м. | YTT Mansurov G'aybullo | 0 |

> Eslatma: agar tizimingizda "Excelga eksport" yoki "CSV yuklab olish" tugmasi
> bo'lsa, o'sha faylni to'g'ridan-to'g'ri Google Sheets'ga import qiling
> (File → Import). Ustun nomlari mos kelmasa, bot ularni tanimaydi — shu
> sababli sarlavhalarni o'zgartirmasdan qoldiring, yoki main.py faylidagi
> ustun nomlarini o'zingizning jadvalingizga moslab tahrirlang.

## 2-qadam: Google Service Account yaratish (bepul, bir martalik)

Bot Sheets'ni o'qishi uchun ruxsat kerak:

1. https://console.cloud.google.com ga kiring, yangi loyiha yarating
2. "APIs & Services" → "Library" → "Google Sheets API" ni yoqing
3. "APIs & Services" → "Credentials" → "Create Credentials" → "Service Account"
4. Yaratilgan service account ichiga kiring → "Keys" → "Add Key" → "JSON"
5. Yuklab olingan faylni `credentials.json` deb nomlab, loyiha papkasiga qo'ying
6. JSON faylda `client_email` maydonini toping (masalan `xxx@xxx.iam.gserviceaccount.com`)
7. Google Sheets faylingizni oching → "Share" → shu emailni qo'shing (Viewer yetarli)

## 3-qadam: Telegram bot yaratish (bepul)

1. Telegram'da @BotFather ga yozing → `/newbot` → nom bering → token oling
2. Menejer bilan botni bir marta suhbatlashtirib qo'ying (masalan `/start` yozsin)
3. Menejerning chat_id'sini bilish uchun @userinfobot ga yozing

## 4-qadam: Sozlash

```bash
cd vizit_bot
pip install -r requirements.txt
cp .env.example .env
```

`.env` faylini oching va to'ldiring:
- `GOOGLE_SHEET_ID` — Sheets linkidagi ID
- `TELEGRAM_BOT_TOKEN` — BotFather'dan olingan token
- `MANAGER_CHAT_ID` — menejerning chat_id'si
- `MAX_POGRESHNOST_METERS` — nechta metrdan katta bo'lsa xato (tavsiya: 150)
- `CHECK_ZERO_PHOTO` — rasm yo'qligini ham xato deb hisoblasinmi (true/false)
- `MIN_TRAVEL_MINUTES` — do'kondan-do'konga o'tish uchun kamida nechta daqiqa
  kerak (tavsiya: 3 — do'konlar bir-biriga yaqin bo'lsa kamaytirishingiz mumkin)

## 5-qadam: Ishga tushirish

Bir martalik tekshirish (sinov uchun):
```bash
python main.py --once
```

Doimiy rejim (har 30 daqiqada avtomatik tekshiradi):
```bash
python main.py
```

## 6-qadam: Serverga joylashtirish (~$5/oy)

- **Railway.app** — bepul limit bilan boshlaydi, keyin ~$5/oy
- **PythonAnywhere** — bepul tarifda "scheduled task" qo'yish mumkin
- **Hetzner / oddiy VPS** — ~$4-5/oy

VPS'da doimiy ishlashi uchun `systemd` xizmat fayli:

```ini
[Unit]
Description=Vizit Tekshiruv Boti
After=network.target

[Service]
WorkingDirectory=/home/USER/vizit_bot
ExecStart=/usr/bin/python3 main.py
Restart=always
EnvironmentFile=/home/USER/vizit_bot/.env

[Install]
WantedBy=multi-user.target
```

## Misol hisobot (Telegramda shunday ko'rinadi)

```
📋 Vizit tekshiruvi hisoboti (2026-09-26 13:00)
Jami 3 ta muammoli vizit topildi:

👤 ТП 9 Сайдуллаева Садокат (NCF-CNF) — 1 ta muammo
   • ИД 204560243 | 26.09.2026 12:14:18 | Мижоз: YTT Mansurov G'aybullo
     ⚠️ GPS xatosi katta: 280 м. (chegara: 150 m)

👤 ТП 5 Равшанова Фарангиз (IN) — 1 ta muammo
   • ИД 204552193 | 26.09.2026 11:56:18 | Мижоз: MCHJ SILK ROAD GLOBAL GROUP
     ⚠️ GPS xatosi katta: 1 км. 914 м. (chegara: 150 m)

👤 ТП 10 Исматуллаева Севара (NCF-CNF) — 1 ta muammo
   • ИД 204552108 | 26.09.2026 11:56:06 | Мижоз: SHERZOD SHERALI SHAXRIZODA MCHJ
     ⚠️ O'tish vaqti juda qisqa: 'Sevinchoy 2018' dan 'SHERZOD SHERALI SHAXRIZODA MCHJ'gacha atigi 0.2 daqiqa (chegara: 3 daqiqa)
```

## Kengaytirish g'oyalari (kerak bo'lsa aytsangiz qo'shib beraman)

- Vizit davomiyligi juda qisqa bo'lsa ham xato deb belgilash (Начала/Конец
  vaqtlari orasidagi farq asosida)
- Do'konlar orasidagi haqiqiy masofani ham hisobga olish (masalan, do'konlar
  bir-biridan 10 km uzoqda bo'lsa, 3 daqiqa emas, kamida 15-20 daqiqa kutish)
- Har bir agent uchun kunlik/oylik statistika (nechta xato, nechta to'g'ri)
- Faqat yangi (oldin tekshirilmagan) qatorlarni tekshirish, takroriy xabar
  yubormaslik
- Xatoni agentning o'ziga ham to'g'ridan-to'g'ri yuborish
