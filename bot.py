import logging
import os
import asyncio
import hmac
import hashlib
import json
from aiohttp import web, ClientSession
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from database import Database

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN      = os.getenv("BOT_TOKEN")
ADMIN_ID       = int(os.getenv("ADMIN_ID"))
NP_API_KEY     = os.getenv("NP_API_KEY")
NP_IPN_SECRET  = os.getenv("NP_IPN_SECRET")
RAILWAY_URL    = os.getenv("RAILWAY_URL")
PORT           = int(os.getenv("PORT", 8080))

db = Database()

PAYMENT_METHODS = {
    "btc":       ("₿  Bitcoin",        "BTC",  "btc"),
    "eth":       ("⬡  Ethereum",       "ETH",  "eth"),
    "ltc":       ("Ł  Litecoin",       "LTC",  "ltc"),
    "usdttrc20": ("◈  USDT  ·  TRC20", "USDT", "usdttrc20"),
    "trx":       ("◉  Tron",           "TRX",  "trx"),
    "xmr":       ("◎  Monero",         "XMR",  "xmr"),
}

IDR_TO_USD = 0.000061


# ─── Pricing ──────────────────────────────────────────────────────────────────

def get_price_tier(quantity: int):
    if quantity >= 2000:
        return 1000, "Rp 1.000 / file", "2.000+ files"
    elif quantity >= 500:
        return 1500, "Rp 1.500 / file", "500 – 1.999 files"
    else:
        return 2000, "Rp 2.000 / file", "1 – 499 files"


# ─── Nowpayments ──────────────────────────────────────────────────────────────

async def create_payment(amount_usd: float, order_id: str, currency: str):
    url = "https://api.nowpayments.io/v1/payment"
    headers = {"x-api-key": NP_API_KEY, "Content-Type": "application/json"}
    payload = {
        "price_amount": round(amount_usd, 4),
        "price_currency": "usd",
        "pay_currency": currency,
        "ipn_callback_url": f"https://{RAILWAY_URL}/webhook",
        "order_id": order_id,
        "order_description": f"PDF Store · Order {order_id}",
    }
    async with ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json()
            logger.info(f"Nowpayments response: {data}")
            return data


def verify_ipn(request_body: bytes, sig: str) -> bool:
    expected = hmac.new(
        NP_IPN_SECRET.encode(), request_body, hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(expected, sig.lower())


# ─── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock = db.get_stock_count()
    keyboard = [
        [InlineKeyboardButton("🛒  Buy Files", callback_data="go_buy")],
        [InlineKeyboardButton("📊  Pricing",   callback_data="go_pricing")],
        [InlineKeyboardButton("🆘  Support",   callback_data="go_support")],
    ]
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "     🗂  *PDF STORE*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦  Stock ready:  *{stock} files*\n\n"
        "⚡  Instant delivery after payment\n"
        "🔒  Unique files — never resold\n"
        "💳  Pay with crypto, receive instantly\n\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "go_buy":
        await _ask_quantity(query.message, context)
    elif query.data == "go_pricing":
        await query.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "      💰  *PRICING*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "  1 – 499 files\n"
            "  └─  *Rp 2.000 / file*\n\n"
            "  500 – 1.999 files\n"
            "  └─  *Rp 1.500 / file*  🔥\n\n"
            "  2.000+ files\n"
            "  └─  *Rp 1.000 / file*  ⚡ Best deal\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Type /buy to order.",
            parse_mode="Markdown"
        )
    elif query.data == "go_support":
        context.user_data["step"] = "awaiting_support"
        await query.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "      🆘  *SUPPORT*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Type your message below.\n"
            "We'll reply as soon as possible.",
            parse_mode="Markdown"
        )


# ─── /buy flow ────────────────────────────────────────────────────────────────

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ask_quantity(update.message, context)


async def _ask_quantity(message, context):
    stock = db.get_stock_count()
    if stock == 0:
        await message.reply_text("😔  Out of stock right now. Check back soon!")
        return
    context.user_data["step"] = "awaiting_quantity"
    await message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "      🛒  *NEW ORDER*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦  Available stock:  *{stock} files*\n\n"
        "How many files do you want?\n"
        "_(Enter a number below)_",
        parse_mode="Markdown"
    )


async def handle_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌  Please enter a valid number.")
        return

    quantity = int(text)
    if quantity <= 0:
        await update.message.reply_text("❌  Quantity must be at least 1.")
        return

    stock = db.get_stock_count()
    if quantity > stock:
        await update.message.reply_text(
            f"❌  Not enough stock.\n📦  Only *{stock}* files available.",
            parse_mode="Markdown"
        )
        return

    idr_per_file, rate_label, tier_label = get_price_tier(quantity)
    total_idr = idr_per_file * quantity
    total_usd = round(total_idr * IDR_TO_USD, 4)

    context.user_data.update({
        "quantity":  quantity,
        "total_idr": total_idr,
        "total_usd": total_usd,
        "step":      "awaiting_payment_method"
    })

    # Payment method buttons — 2 per row, clean grid
    keyboard = [
        [
            InlineKeyboardButton("₿  Bitcoin",      callback_data="pay_btc"),
            InlineKeyboardButton("⬡  Ethereum",     callback_data="pay_eth"),
        ],
        [
            InlineKeyboardButton("Ł  Litecoin",     callback_data="pay_ltc"),
            InlineKeyboardButton("◈  USDT TRC20",   callback_data="pay_usdttrc20"),
        ],
        [
            InlineKeyboardButton("◉  Tron",         callback_data="pay_trx"),
            InlineKeyboardButton("◎  Monero",       callback_data="pay_xmr"),
        ],
        [
            InlineKeyboardButton("❌  Cancel",       callback_data="cancel_buy"),
        ],
    ]

    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "    🧾  *ORDER SUMMARY*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  📦  Files       :  *{quantity}*\n"
        f"  🏷  Tier         :  *{tier_label}*\n"
        f"  💲  Rate         :  *{rate_label}*\n"
        f"  🇮🇩  Total IDR  :  *Rp {total_idr:,}*\n"
        f"  💵  Total USD  :  *≈ ${total_usd}*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "      💳  *SELECT PAYMENT*\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_buy":
        context.user_data.clear()
        await query.edit_message_text(
            "❌  Order cancelled.\nType /buy to start a new order."
        )
        return

    if query.data in ("go_buy", "go_pricing", "go_support"):
        await start_callback(update, context)
        return

    if not query.data.startswith("pay_"):
        return

    currency_key = query.data.replace("pay_", "")
    method = PAYMENT_METHODS.get(currency_key)
    if not method:
        await query.edit_message_text("❌  Invalid method. Type /buy again.")
        return

    method_label, ticker, currency_code = method
    quantity  = context.user_data.get("quantity")
    total_usd = context.user_data.get("total_usd")
    total_idr = context.user_data.get("total_idr")
    buyer     = query.from_user

    if not quantity or not total_usd:
        await query.edit_message_text("❌  Session expired. Type /buy again.")
        return

    await query.edit_message_text(
        f"⏳  Generating *{ticker}* payment address...\n\n"
        f"_Please wait a moment._",
        parse_mode="Markdown"
    )

    order_id = f"{buyer.id}_{quantity}_{int(asyncio.get_event_loop().time())}"
    db.create_pending_order(order_id, buyer.id, buyer.username or buyer.first_name, quantity, total_idr, total_usd)

    try:
        payment = await create_payment(total_usd, order_id, currency_code)

        if "code" in payment or "error" in payment:
            raise Exception(f"API error: {payment}")

        pay_address = payment.get("pay_address")
        pay_amount  = payment.get("pay_amount")
        pay_cur     = payment.get("pay_currency", currency_code).upper()
        payment_id  = str(payment.get("payment_id", ""))

        if not pay_address:
            raise Exception(f"No address returned: {payment}")

        db.set_order_payment_id(order_id, payment_id)

        await context.bot.send_message(
            buyer.id,
            "━━━━━━━━━━━━━━━━━━━━\n"
            "    💳  *PAYMENT DETAILS*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"  📦  Files       :  *{quantity}*\n"
            f"  🪙  Currency  :  *{pay_cur}*\n"
            f"  💰  Amount    :  *{pay_amount} {pay_cur}*\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "  📬  *Send to this address:*\n\n"
            f"`{pay_address}`\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️  Send *only {pay_cur}* to this address\n"
            f"✅  Files delivered *automatically* after confirmation\n"
            f"⏱  Payment expires in *60 minutes*\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Payment creation failed: {e}")
        await context.bot.send_message(
            buyer.id,
            "━━━━━━━━━━━━━━━━━━━━\n"
            "      ❌  *PAYMENT ERROR*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Failed to generate address.\n"
            "Please try /buy again or contact /support.",
            parse_mode="Markdown"
        )

    context.user_data.clear()


# ─── Webhook ──────────────────────────────────────────────────────────────────

async def webhook_handler(request: web.Request):
    body = await request.read()
    sig  = request.headers.get("x-nowpayments-sig", "")

    if not verify_ipn(body, sig):
        logger.warning("Invalid IPN signature")
        return web.Response(status=400, text="Invalid signature")

    data           = json.loads(body)
    payment_status = data.get("payment_status")
    order_id       = data.get("order_id")
    logger.info(f"IPN: status={payment_status} order={order_id}")

    if payment_status in ("finished", "confirmed") and order_id:
        order = db.get_order(order_id)
        if not order or order["status"] != "pending":
            return web.Response(status=200, text="OK")

        db.mark_order_paid(order_id)
        buyer_id       = order["buyer_id"]
        quantity       = order["quantity"]
        buyer_username = order["buyer_username"]
        tg_app         = request.app["tg_app"]

        files = db.claim_files(quantity, buyer_id, buyer_username)

        if not files:
            await tg_app.bot.send_message(buyer_id,
                "⚠️  Payment received but stock ran out.\nContact /support immediately. You will be refunded.")
            await tg_app.bot.send_message(ADMIN_ID,
                f"🚨 STOCK ERROR!\n@{buyer_username} ({buyer_id}) paid for {quantity} files — stock empty. REFUND NEEDED.")
            return web.Response(status=200, text="OK")

        await tg_app.bot.send_message(
            buyer_id,
            "━━━━━━━━━━━━━━━━━━━━\n"
            "    ✅  *PAYMENT CONFIRMED*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦  Sending *{quantity}* files now...\n"
            "_This may take a moment._",
            parse_mode="Markdown"
        )

        failed = 0
        for i, file_id in enumerate(files, 1):
            try:
                await tg_app.bot.send_document(
                    chat_id=buyer_id,
                    document=file_id,
                    caption=f"📄  File {i} of {quantity}"
                )
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"Failed to send {file_id}: {e}")
                failed += 1

        success = quantity - failed
        await tg_app.bot.send_message(
            buyer_id,
            "━━━━━━━━━━━━━━━━━━━━\n"
            "    🎉  *DELIVERY COMPLETE*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✅  *{success}/{quantity}* files delivered\n\n"
            "Thank you for your purchase!\n"
            "Need help?  /support",
            parse_mode="Markdown"
        )

        remaining = db.get_stock_count()
        await tg_app.bot.send_message(
            ADMIN_ID,
            "━━━━━━━━━━━━━━━━━━━━\n"
            "    🛒  *NEW SALE*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"  👤  Buyer      :  @{buyer_username}\n"
            f"  🆔  ID           :  `{buyer_id}`\n"
            f"  📦  Files       :  *{quantity}*\n"
            f"  💵  USD         :  *${order['total_usd']}*\n"
            f"  🇮🇩  IDR         :  *Rp {order['total_idr']:,}*\n"
            f"  📊  Remaining :  *{remaining} files*\n\n"
            f"  🔖  Order ID  :  `{order_id}`\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )

    return web.Response(status=200, text="OK")


async def health_check(request):
    return web.Response(text="OK")


# ─── Admin ────────────────────────────────────────────────────────────────────

async def stock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    count = db.get_stock_count()
    await update.message.reply_text(
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"      📦  *STOCK STATUS*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  Available:  *{count} files*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown"
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    doc = update.message.document
    if doc.mime_type == "application/pdf":
        db.add_file(doc.file_id)
        count = db.get_stock_count()
        await update.message.reply_text(
            f"✅  File added to stock!\n"
            f"📦  Total stock:  *{count} files*",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌  Only PDF files accepted.")


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["step"] = "awaiting_support"
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "      🆘  *SUPPORT*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Type your message below.\n"
        "We'll get back to you shortly.",
        parse_mode="Markdown"
    )


async def handle_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buyer = update.effective_user
    await context.bot.send_message(
        ADMIN_ID,
        "━━━━━━━━━━━━━━━━━━━━\n"
        "    📣  *SUPPORT REQUEST*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  👤  @{buyer.username or 'N/A'}\n"
        f"  🆔  `{buyer.id}`\n\n"
        f"  💬  {update.message.text}\n\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown"
    )
    await update.message.reply_text(
        "✅  Message sent to store owner.\nWe'll reply soon!"
    )
    context.user_data.clear()


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast Your message here")
        return
    msg = " ".join(context.args)
    buyers = db.get_all_buyer_ids()
    sent = 0
    for uid in buyers:
        try:
            await context.bot.send_message(uid, f"📢  {msg}")
            sent += 1
            await asyncio.sleep(0.1)
        except Exception:
            pass
    await update.message.reply_text(f"✅  Broadcast sent to {sent} users.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "       📖  *COMMANDS*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "  /start   —  Home\n"
        "  /buy      —  Purchase files\n"
        "  /support —  Contact us\n"
        "  /help     —  This menu\n\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("step")
    if step == "awaiting_quantity":
        await handle_quantity(update, context)
    elif step == "awaiting_support":
        await handle_support_message(update, context)


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    tg_app = Application.builder().token(BOT_TOKEN).build()

    tg_app.add_handler(CommandHandler("start",     start))
    tg_app.add_handler(CommandHandler("buy",       buy))
    tg_app.add_handler(CommandHandler("stock",     stock_cmd))
    tg_app.add_handler(CommandHandler("support",   support))
    tg_app.add_handler(CommandHandler("help",      help_cmd))
    tg_app.add_handler(CommandHandler("broadcast", broadcast))
    tg_app.add_handler(CallbackQueryHandler(confirm_callback))
    tg_app.add_handler(MessageHandler(filters.Document.ALL,          handle_document))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    web_app = web.Application()
    web_app["tg_app"] = tg_app
    web_app.router.add_post("/webhook", webhook_handler)
    web_app.router.add_get("/",         health_check)

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling()

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info(f"Bot running on port {PORT}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
