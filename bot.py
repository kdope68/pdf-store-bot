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

BOT_TOKEN   = os.getenv("BOT_TOKEN")
ADMIN_ID    = int(os.getenv("ADMIN_ID"))
NP_API_KEY  = os.getenv("NP_API_KEY")
NP_IPN_SECRET = os.getenv("NP_IPN_SECRET")
RAILWAY_URL = os.getenv("RAILWAY_URL")  # e.g. web-production-3422e.up.railway.app
PORT        = int(os.getenv("PORT", 8080))

db = Database()


# ─── Pricing ──────────────────────────────────────────────────────────────────

def get_price_tier(quantity: int):
    """Returns (idr_per_file, label)"""
    if quantity >= 2000:
        return 1000, "2000+ files (Rp 1.000/file)"
    elif quantity >= 500:
        return 1500, "500–1999 files (Rp 1.500/file)"
    else:
        return 2000, "1–499 files (Rp 2.000/file)"

IDR_TO_USD = 0.000061  # 1 IDR = ~0.000061 USD (Rp16,300 per dollar)


# ─── Nowpayments API ──────────────────────────────────────────────────────────

async def create_payment(amount_usd: float, order_id: str, buyer_id: int):
    url = "https://api.nowpayments.io/v1/payment"
    headers = {"x-api-key": NP_API_KEY, "Content-Type": "application/json"}
    payload = {
        "price_amount": round(amount_usd, 4),
        "price_currency": "usd",
        "pay_currency": "usdttrc20",  # USDT
        "ipn_callback_url": f"https://{RAILWAY_URL}/webhook",
        "order_id": order_id,
        "order_description": f"PDF Store - Order {order_id}",
    }
    async with ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            return await resp.json()


def verify_ipn(request_body: bytes, sig: str) -> bool:
    expected = hmac.new(
        NP_IPN_SECRET.encode(), request_body, hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(expected, sig.lower())


# ─── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock = db.get_stock_count()
    text = (
        "🛒 *Welcome to our PDF Store!*\n\n"
        f"📦 *Stock available:* {stock} files\n\n"
        "💡 *How to buy:*\n"
        "Type /buy and follow the steps.\n\n"
        "💰 *Pricing:*\n"
        "• 1–499 files → Rp 2.000/file\n"
        "• 500–1.999 files → Rp 1.500/file\n"
        "• 2.000+ files → Rp 1.000/file\n\n"
        "💳 *Payment:* USDT (TON Network)\n\n"
        "❓ Need help? Type /support"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ─── /buy ─────────────────────────────────────────────────────────────────────

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock = db.get_stock_count()
    if stock == 0:
        await update.message.reply_text("😔 Out of stock right now. Check back soon!")
        return
    context.user_data["step"] = "awaiting_quantity"
    await update.message.reply_text(
        f"📦 *Stock available:* {stock} files\n\n"
        "How many files do you want?\n_(Type a number, e.g. 100)_",
        parse_mode="Markdown"
    )


async def handle_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Please enter a valid number.")
        return

    quantity = int(text)
    if quantity <= 0:
        await update.message.reply_text("❌ Quantity must be at least 1.")
        return

    stock = db.get_stock_count()
    if quantity > stock:
        await update.message.reply_text(
            f"❌ Not enough stock. Only *{stock}* files available.",
            parse_mode="Markdown"
        )
        return

    idr_per_file, tier_label = get_price_tier(quantity)
    total_idr = idr_per_file * quantity
    total_usd = round(total_idr * IDR_TO_USD, 4)

    context.user_data["quantity"] = quantity
    context.user_data["total_idr"] = total_idr
    context.user_data["total_usd"] = total_usd
    context.user_data["step"] = "awaiting_confirm"

    keyboard = [
        [InlineKeyboardButton("✅ Confirm & Pay", callback_data="confirm_buy")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_buy")]
    ]

    await update.message.reply_text(
        f"🧾 *Order Summary*\n"
        f"──────────────────\n"
        f"📦 Files: *{quantity}*\n"
        f"💰 Rate: *{tier_label}*\n"
        f"🇮🇩 Total IDR: *Rp {total_idr:,}*\n"
        f"💵 Total USDT: *${total_usd}*\n"
        f"──────────────────\n"
        f"Payment via USDT (TON Network)",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_buy":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelled. Type /buy to start again.")
        return

    quantity = context.user_data.get("quantity")
    total_usd = context.user_data.get("total_usd")
    total_idr = context.user_data.get("total_idr")
    buyer = query.from_user

    if not quantity or not total_usd:
        await query.edit_message_text("❌ Something went wrong. Type /buy again.")
        return

    await query.edit_message_text("⏳ Generating your payment address...")

    order_id = f"{buyer.id}_{quantity}_{int(asyncio.get_event_loop().time())}"
    db.create_pending_order(order_id, buyer.id, buyer.username or buyer.first_name, quantity, total_idr, total_usd)

    try:
        payment = await create_payment(total_usd, order_id, buyer.id)
        pay_address = payment.get("pay_address")
        pay_amount = payment.get("pay_amount")
        pay_currency = payment.get("pay_currency", "USDT").upper()
        payment_id = payment.get("payment_id")

        if not pay_address:
            raise Exception(f"No address returned: {payment}")

        db.set_order_payment_id(order_id, str(payment_id))

        await context.bot.send_message(
            buyer.id,
            f"💳 *Payment Instructions*\n"
            f"──────────────────\n"
            f"📦 Files: *{quantity}*\n"
            f"💵 Amount: *{pay_amount} {pay_currency}*\n"
            f"🔗 Network: *TON*\n"
            f"──────────────────\n"
            f"Send *exactly* this amount to:\n\n"
            f"`{pay_address}`\n\n"
            f"_(Tap address to copy)_\n\n"
            f"⚠️ Send *only* USDT on TON network.\n"
            f"✅ Files sent automatically after confirmation.\n"
            f"⏱ Payment expires in 60 minutes.",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Payment creation failed: {e}")
        await context.bot.send_message(
            buyer.id,
            "❌ Failed to generate payment. Please try /buy again or contact /support."
        )

    context.user_data.clear()


# ─── Webhook (Nowpayments IPN) ────────────────────────────────────────────────

async def webhook_handler(request: web.Request):
    body = await request.read()
    sig = request.headers.get("x-nowpayments-sig", "")

    if not verify_ipn(body, sig):
        logger.warning("Invalid IPN signature")
        return web.Response(status=400, text="Invalid signature")

    data = json.loads(body)
    logger.info(f"IPN received: {data}")

    payment_status = data.get("payment_status")
    order_id = data.get("order_id")

    if payment_status in ("finished", "confirmed") and order_id:
        order = db.get_order(order_id)
        if not order or order["status"] != "pending":
            return web.Response(status=200, text="OK")

        db.mark_order_paid(order_id)

        buyer_id = order["buyer_id"]
        quantity = order["quantity"]
        buyer_username = order["buyer_username"]

        files = db.claim_files(quantity, buyer_id, buyer_username)
        app = request.app["tg_app"]

        if not files:
            await app.bot.send_message(
                buyer_id,
                "⚠️ Payment received but stock ran out. Contact /support immediately. You will be refunded."
            )
            await app.bot.send_message(
                ADMIN_ID,
                f"🚨 STOCK ERROR!\nBuyer @{buyer_username} (ID: {buyer_id}) paid for {quantity} files but stock ran out. REFUND NEEDED."
            )
            return web.Response(status=200, text="OK")

        await app.bot.send_message(
            buyer_id,
            f"✅ *Payment confirmed!*\n\n"
            f"📦 Sending your *{quantity}* files now...\n_(This may take a moment)_",
            parse_mode="Markdown"
        )

        failed = 0
        for i, file_id in enumerate(files, 1):
            try:
                await app.bot.send_document(
                    chat_id=buyer_id,
                    document=file_id,
                    caption=f"📄 File {i}/{quantity}"
                )
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"Failed to send file {file_id}: {e}")
                failed += 1

        success_count = quantity - failed
        await app.bot.send_message(
            buyer_id,
            f"🎉 *Done! {success_count}/{quantity} files delivered.*\n\nThank you!\nNeed help? /support",
            parse_mode="Markdown"
        )

        # Notify admin
        idr_value = order["total_idr"]
        remaining = db.get_stock_count()
        await app.bot.send_message(
            ADMIN_ID,
            f"🛒 *NEW ORDER PAID!*\n"
            f"──────────────────\n"
            f"👤 Buyer: @{buyer_username} (ID: `{buyer_id}`)\n"
            f"📦 Files: *{quantity}*\n"
            f"💵 USDT paid: *${order['total_usd']}*\n"
            f"🇮🇩 IDR value: *Rp {idr_value:,}*\n"
            f"📊 Stock left: *{remaining}*\n"
            f"──────────────────\n"
            f"Order: `{order_id}`",
            parse_mode="Markdown"
        )

    return web.Response(status=200, text="OK")


async def health_check(request):
    return web.Response(text="OK")


# ─── Admin commands ───────────────────────────────────────────────────────────

async def stock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    count = db.get_stock_count()
    await update.message.reply_text(f"📦 Stock: *{count}* files", parse_mode="Markdown")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        return
    doc = update.message.document
    if doc.mime_type == "application/pdf":
        db.add_file(doc.file_id)
        count = db.get_stock_count()
        await update.message.reply_text(
            f"✅ File added!\n`{doc.file_id}`\n📦 Total stock: *{count}*",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Only PDF files accepted.")


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["step"] = "awaiting_support"
    await update.message.reply_text(
        "📝 Describe your issue and I'll forward it to the store owner.\n_(Type your message now)_",
        parse_mode="Markdown"
    )


async def handle_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buyer = update.effective_user
    await context.bot.send_message(
        ADMIN_ID,
        f"📣 *SUPPORT REQUEST*\n──────────────────\n"
        f"👤 @{buyer.username or 'N/A'} (ID: `{buyer.id}`)\n"
        f"💬 {update.message.text}",
        parse_mode="Markdown"
    )
    await update.message.reply_text("✅ Sent to store owner. We'll reply soon!")
    context.user_data.clear()


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast Your message")
        return
    msg = " ".join(context.args)
    buyers = db.get_all_buyer_ids()
    sent = 0
    for uid in buyers:
        try:
            await context.bot.send_message(uid, f"📢 {msg}")
            sent += 1
            await asyncio.sleep(0.1)
        except Exception:
            pass
    await update.message.reply_text(f"✅ Sent to {sent} users.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Commands:*\n\n"
        "/start — Welcome & pricing\n"
        "/buy — Purchase files\n"
        "/support — Contact store owner\n"
        "/help — Show this menu",
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

    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("buy", buy))
    tg_app.add_handler(CommandHandler("stock", stock_cmd))
    tg_app.add_handler(CommandHandler("support", support))
    tg_app.add_handler(CommandHandler("help", help_cmd))
    tg_app.add_handler(CommandHandler("broadcast", broadcast))
    tg_app.add_handler(CallbackQueryHandler(confirm_callback))
    tg_app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    web_app = web.Application()
    web_app["tg_app"] = tg_app
    web_app.router.add_post("/webhook", webhook_handler)
    web_app.router.add_get("/", health_check)

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling()

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info(f"Bot running. Webhook server on port {PORT}")

    await asyncio.Event().wait()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
