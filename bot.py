import logging
import os
import asyncio
from telegram import Update, LabeledPrice, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, PreCheckoutQueryHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from database import Database

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

db = Database()


def get_price_tier(quantity: int) -> tuple[int, str]:
    """Returns (stars_per_file, tier_label)"""
    if quantity >= 2000:
        return 5, "2000+ files (5⭐/file)"
    elif quantity >= 500:
        return 8, "500–1999 files (8⭐/file)"
    else:
        return 11, "1–499 files (11⭐/file)"


# ─── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock = db.get_stock_count()
    text = (
        "🛒 *Welcome to our PDF Store!*\n\n"
        f"📦 *Stock available:* {stock} files\n\n"
        "💡 *How to buy:*\n"
        "Type /buy and follow the steps.\n\n"
        "💰 *Pricing:*\n"
        "• 1–499 files → 11⭐ per file\n"
        "• 500–1,999 files → 8⭐ per file\n"
        "• 2,000+ files → 5⭐ per file\n\n"
        "⭐ *Need Stars?* Buy them at [fund.tg](https://fund.tg)\n\n"
        "❓ Need help? Type /support"
    )
    await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)


# ─── /buy ─────────────────────────────────────────────────────────────────────

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock = db.get_stock_count()
    if stock == 0:
        await update.message.reply_text("😔 Sorry, we're out of stock right now. Check back soon!")
        return
    context.user_data["step"] = "awaiting_quantity"
    await update.message.reply_text(
        f"📦 *Stock available:* {stock} files\n\n"
        "How many files do you want to buy?\n"
        "_(Type a number, e.g. 100)_",
        parse_mode="Markdown"
    )


async def handle_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("step") != "awaiting_quantity":
        return

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
            f"❌ Not enough stock. We only have *{stock}* files available.",
            parse_mode="Markdown"
        )
        return

    stars_per_file, tier_label = get_price_tier(quantity)
    total_stars = stars_per_file * quantity

    context.user_data["quantity"] = quantity
    context.user_data["total_stars"] = total_stars
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
        f"⭐ Total: *{total_stars} Stars*\n"
        f"──────────────────\n"
        f"💡 Need Stars? Get them at [fund.tg](https://fund.tg)",
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_buy":
        context.user_data.clear()
        await query.edit_message_text("❌ Order cancelled. Type /buy to start again.")
        return

    quantity = context.user_data.get("quantity")
    total_stars = context.user_data.get("total_stars")

    if not quantity or not total_stars:
        await query.edit_message_text("❌ Something went wrong. Please type /buy again.")
        return

    context.user_data["step"] = "awaiting_payment"

    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title=f"PDF Files x{quantity}",
        description=f"Purchase of {quantity} PDF files from our store.",
        payload=f"buy_{quantity}_{query.from_user.id}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{quantity} PDF Files", amount=total_stars)],
    )

    await query.edit_message_text(
        "⬆️ Invoice sent above! Complete payment to receive your files.\n\n"
        "⭐ Need Stars? Buy at [fund.tg](https://fund.tg)",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


# ─── Payment handlers ─────────────────────────────────────────────────────────

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    payload = query.invoice_payload

    if not payload.startswith("buy_"):
        await query.answer(ok=False, error_message="Invalid order.")
        return

    parts = payload.split("_")
    quantity = int(parts[1])
    stock = db.get_stock_count()

    if quantity > stock:
        await query.answer(ok=False, error_message=f"Sorry, only {stock} files left in stock.")
        return

    await query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    parts = payload.split("_")
    quantity = int(parts[1])
    buyer = update.effective_user
    stars_paid = payment.total_amount

    # Lock and fetch files
    files = db.claim_files(quantity, buyer.id, buyer.username or buyer.first_name)

    if not files:
        await update.message.reply_text(
            "⚠️ Payment received but stock ran out. Please contact /support immediately. "
            "You will be refunded."
        )
        await context.bot.send_message(
            ADMIN_ID,
            f"🚨 STOCK ERROR!\nBuyer @{buyer.username} (ID: {buyer.id}) paid {stars_paid}⭐ "
            f"for {quantity} files but stock ran out. REFUND NEEDED."
        )
        return

    # Send files to buyer
    await update.message.reply_text(
        f"✅ *Payment confirmed!*\n\n"
        f"📦 Sending your *{quantity}* files now...\n"
        f"_(This may take a moment)_",
        parse_mode="Markdown"
    )

    failed = 0
    for i, file_id in enumerate(files, 1):
        try:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=file_id,
                caption=f"📄 File {i}/{quantity}"
            )
            await asyncio.sleep(0.3)  # avoid flood limits
        except Exception as e:
            logger.error(f"Failed to send file {file_id}: {e}")
            failed += 1

    success_count = quantity - failed
    await update.message.reply_text(
        f"🎉 *Done! {success_count}/{quantity} files delivered.*\n\n"
        f"Thank you for your purchase!\n"
        f"Need help? Type /support",
        parse_mode="Markdown"
    )

    # Notify admin
    stars_per_file, tier = get_price_tier(quantity)
    idr_value = stars_per_file * quantity * 185
    remaining = db.get_stock_count()

    await context.bot.send_message(
        ADMIN_ID,
        f"🛒 *NEW ORDER!*\n"
        f"──────────────────\n"
        f"👤 Buyer: @{buyer.username or 'N/A'} (ID: `{buyer.id}`)\n"
        f"📦 Files bought: *{quantity}*\n"
        f"⭐ Stars paid: *{stars_paid}*\n"
        f"💰 Est. value: *Rp {idr_value:,}*\n"
        f"📊 Stock remaining: *{remaining}*\n"
        f"──────────────────\n"
        f"Order ID: `#ORD-{payment.telegram_payment_charge_id[-8:].upper()}`",
        parse_mode="Markdown"
    )

    context.user_data.clear()


# ─── /stock (admin only) ──────────────────────────────────────────────────────

async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    count = db.get_stock_count()
    await update.message.reply_text(f"📦 Current stock: *{count}* files available.", parse_mode="Markdown")


# ─── /addfile (admin only) — forward a PDF to bot to add it ──────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    doc = update.message.document

    # Admin adding files
    if user.id == ADMIN_ID:
        if doc.mime_type == "application/pdf":
            file_id = doc.file_id
            db.add_file(file_id)
            count = db.get_stock_count()
            await update.message.reply_text(
                f"✅ File added!\n`{file_id}`\n📦 Total stock: *{count}*",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("❌ Only PDF files accepted.")
        return


# ─── /support ─────────────────────────────────────────────────────────────────

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["step"] = "awaiting_support"
    await update.message.reply_text(
        "📝 Please describe your issue and I'll forward it to the store owner.\n"
        "_(Type your message now)_",
        parse_mode="Markdown"
    )


async def handle_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("step") != "awaiting_support":
        return
    buyer = update.effective_user
    msg = update.message.text

    await context.bot.send_message(
        ADMIN_ID,
        f"📣 *SUPPORT REQUEST*\n"
        f"──────────────────\n"
        f"👤 From: @{buyer.username or 'N/A'} (ID: `{buyer.id}`)\n"
        f"💬 Message: {msg}",
        parse_mode="Markdown"
    )
    await update.message.reply_text(
        "✅ Your message has been sent to the store owner.\n"
        "We'll get back to you soon!"
    )
    context.user_data.clear()


# ─── /broadcast (admin only) ─────────────────────────────────────────────────

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
            await context.bot.send_message(uid, f"📢 {msg}")
            sent += 1
            await asyncio.sleep(0.1)
        except Exception:
            pass
    await update.message.reply_text(f"✅ Broadcast sent to {sent} users.")


# ─── /help ────────────────────────────────────────────────────────────────────

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Commands:*\n\n"
        "/start — Welcome & pricing info\n"
        "/buy — Purchase files\n"
        "/support — Contact store owner\n"
        "/help — Show this menu\n\n"
        "⭐ Buy Stars at [fund.tg](https://fund.tg)",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("stock", stock))
    app.add_handler(CommandHandler("support", support))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast))

    app.add_handler(CallbackQueryHandler(confirm_callback))
    app.add_handler(PreCheckoutQueryHandler(precheckout))

    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started.")
    app.run_polling()


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("step")
    if step == "awaiting_quantity":
        await handle_quantity(update, context)
    elif step == "awaiting_support":
        await handle_support_message(update, context)


if __name__ == "__main__":
    main()
