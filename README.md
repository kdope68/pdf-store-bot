# PDF Store Telegram Bot — Deployment Guide

## Files
- `bot.py` — main bot logic
- `database.py` — SQLite database handler
- `requirements.txt` — dependencies
- `Procfile` — tells Railway how to run the bot

---

## Step 1 — Upload to GitHub

1. Go to https://github.com and sign in (create account if needed)
2. Click **New repository** → name it `pdf-store-bot` → **Create**
3. Upload all 4 files: `bot.py`, `database.py`, `requirements.txt`, `Procfile`

---

## Step 2 — Deploy on Railway

1. Go to https://railway.app → sign in with GitHub
2. Click **New Project** → **Deploy from GitHub repo**
3. Select `pdf-store-bot`
4. Click **Add Variables** and add:

```
BOT_TOKEN = (your bot token from BotFather)
ADMIN_ID   = 7801675291
```

5. Click **Deploy** — done, bot is live 24/7

---

## Step 3 — Enable Telegram Stars Payments

1. Open Telegram → @BotFather
2. Send `/mybots` → select your bot
3. **Payments** → enable **Telegram Stars**

---

## Step 4 — Add Your PDF Files to the Bot

1. Open your bot in Telegram
2. Send any PDF file directly to the bot
3. Bot replies: ✅ File added! + shows current stock count
4. Repeat for all your PDFs

That's it. The bot automatically saves the file_id and adds it to stock.

---

## Admin Commands

| Command | What it does |
|---|---|
| Send a PDF to bot | Adds it to stock automatically |
| `/stock` | Shows current stock count |
| `/broadcast Your message` | Sends message to all buyers |

## Buyer Commands

| Command | What it does |
|---|---|
| `/start` | Welcome message + pricing |
| `/buy` | Start purchase flow |
| `/support` | Send complaint/question to you |
| `/help` | Show all commands |

---

## Pricing

| Quantity | Stars/file | ~IDR/file |
|---|---|---|
| 1–499 | 11 ⭐ | Rp 2,035 |
| 500–1,999 | 8 ⭐ | Rp 1,480 |
| 2,000+ | 5 ⭐ | Rp 925 |

Stars are purchased by buyers at https://fund.tg

---

## How a Purchase Works

1. Buyer types `/buy`
2. Enters quantity
3. Bot shows order summary + total Stars
4. Buyer confirms → invoice sent
5. Buyer pays with Stars
6. Bot instantly sends exact files (no duplicates, ever)
7. Files marked as sold permanently
8. You get notified instantly on Telegram with full order details
