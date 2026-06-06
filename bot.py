import logging
import os
import asyncio
import hmac
import hashlib
import json
import zipfile
import shutil
import tempfile
from datetime import datetime, time as dtime
from pathlib import Path
from aiohttp import web, ClientSession
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
    ApplicationBuilder
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
PROMO_CHANNEL  = "@goatdatabase"
BOT_USERNAME   = "@datagacor_bot"

db = Database()

PAYMENT_METHODS = {
    "btc":       ("₿  Bitcoin",       "BTC",  "btc"),
    "eth":       ("⬡  Ethereum",      "ETH",  "eth"),
    "ltc":       ("Ł  Litecoin",      "LTC",  "ltc"),
    "usdttrc20": ("◈  USDT · TRC20",  "USDT", "usdttrc20"),
    "trx":       ("◉  Tron",          "TRX",  "trx"),
    "xmr":       ("◎  Monero",        "XMR",  "xmr"),
}

IDR_TO_USD = 0.000061
STORAGE_DIR = Path("storage")
STORAGE_DIR.mkdir(exist_ok=True)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def branded_name(original: str) -> str:
    """Rename file to @datagacor_bot-{original}"""
    return f"{BOT_USERNAME}-{original}"


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


# ─── Admin guard ──────────────────────────────────────────────────────────────

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text(
                "━━━━━━━━━━━━━━━━━━━━\n"
                "      🚫  *ACCESS DENIED*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "This action is restricted.\n"
                "Contact the store owner if you\n"
                "believe this is a mistake.\n\n"
                "━━━━━━━━━━━━━━━━━━━━",
                parse_mode="Markdown"
            )
            return
        return await func(update, context)
    return wrapper


# ─── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock = db.get_stock_count()
    keyboard = [
        [InlineKeyboardButton("🛒  Buy Files",  callback_data="go_buy")],
        [InlineKeyboardButton("📊  Pricing",    callback_data="go_pricing")],
        [InlineKeyboardButton("🆘  Support",    callback_data="go_support")],
    ]
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "       🗂  *PDF STORE*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  📦  Stock ready   :  *{stock} files*\n\n"
        "  ⚡  Instant delivery after payment\n"
        "  🔒  Unique files — never resold\n"
        "  💳  Pay with crypto\n\n"
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
            "       💰  *PRICING*\n"
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
            "       🆘  *SUPPORT*\n"
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
        await message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "      😔  *OUT OF STOCK*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "No files available right now.\n"
            "Check back soon!\n\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )
        return
    context.user_data["step"] = "awaiting_quantity"
    await message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "       🛒  *NEW ORDER*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  📦  Available stock:  *{stock} files*\n\n"
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
            f"❌  Not enough stock.\n"
            f"📦  Only *{stock}* files available.",
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

    keyboard = [
        [
            InlineKeyboardButton("₿  Bitcoin",     callback_data="pay_btc"),
            InlineKeyboardButton("⬡  Ethereum",    callback_data="pay_eth"),
        ],
        [
            InlineKeyboardButton("Ł  Litecoin",    callback_data="pay_ltc"),
            InlineKeyboardButton("◈  USDT TRC20",  callback_data="pay_usdttrc20"),
        ],
        [
            InlineKeyboardButton("◉  Tron",        callback_data="pay_trx"),
            InlineKeyboardButton("◎  Monero",      callback_data="pay_xmr"),
        ],
        [InlineKeyboardButton("❌  Cancel",         callback_data="cancel_buy")],
    ]

    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "     🧾  *ORDER SUMMARY*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  📦  Files        :  *{quantity}*\n"
        f"  🏷  Tier          :  *{tier_label}*\n"
        f"  💲  Rate          :  *{rate_label}*\n"
        f"  🇮🇩  Total IDR   :  *Rp {total_idr:,}*\n"
        f"  💵  Total USD   :  *≈ ${total_usd}*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "     💳  *SELECT PAYMENT*\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data in ("go_buy", "go_pricing", "go_support"):
        await start_callback(update, context)
        return

    if query.data == "cancel_buy":
        context.user_data.clear()
        await query.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "      ❌  *CANCELLED*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Order cancelled.\n"
            "Type /buy to start a new order.\n\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )
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
        f"⏳  Generating *{ticker}* address...\n_Please wait._",
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
            raise Exception(f"No address: {payment}")

        db.set_order_payment_id(order_id, payment_id)

        await context.bot.send_message(
            buyer.id,
            "━━━━━━━━━━━━━━━━━━━━\n"
            "    💳  *PAYMENT DETAILS*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"  📦  Files        :  *{quantity}*\n"
            f"  🪙  Currency   :  *{pay_cur}*\n"
            f"  💰  Amount     :  *{pay_amount} {pay_cur}*\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "  📬  *Send to this address:*\n\n"
            f"`{pay_address}`\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"  ⚠️  Send *only {pay_cur}* to this address\n"
            f"  ✅  Files delivered *automatically*\n"
            f"  ⏱  Expires in *60 minutes*\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Payment creation failed: {e}")
        await context.bot.send_message(
            buyer.id,
            "━━━━━━━━━━━━━━━━━━━━\n"
            "     ❌  *PAYMENT ERROR*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Failed to generate address.\n"
            "Please try /buy again or\n"
            "contact /support.\n\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )

    context.user_data.clear()


# ─── Admin: handle ZIP upload ─────────────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Non-admin gets professional error
    if user.id != ADMIN_ID:
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "      🚫  *ACCESS DENIED*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "File uploads are restricted\n"
            "to authorized personnel only.\n\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )
        return

    doc = update.message.document
    mime = doc.mime_type or ""

    # Must be ZIP
    if mime not in ("application/zip", "application/x-zip-compressed") and not doc.file_name.endswith(".zip"):
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "      ❌  *INVALID FILE*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Only *.zip* files are accepted.\n"
            "Please upload a ZIP archive.\n\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )
        return

    status_msg = await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "  📥  *PROCESSING ZIP...*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "  ⬇️  Downloading...",
        parse_mode="Markdown"
    )

    try:
        # Download ZIP
        tg_file = await context.bot.get_file(doc.file_id)
        tmp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(tmp_dir, doc.file_name)
        await tg_file.download_to_drive(zip_path)

        await status_msg.edit_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "  📥  *PROCESSING ZIP...*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "  ✅  Downloaded\n"
            "  📂  Extracting...",
            parse_mode="Markdown"
        )

        # Extract
        extract_dir = os.path.join(tmp_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)

        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_dir)

        # Delete ZIP immediately
        os.remove(zip_path)

        # Collect all PDFs, deduplicate by filename
        all_pdfs = []
        seen_names = set()
        duplicates = 0

        for root, dirs, files in os.walk(extract_dir):
            # Skip hidden/system dirs
            dirs[:] = [d for d in dirs if not d.startswith("__") and not d.startswith(".")]
            for fname in files:
                if fname.lower().endswith(".pdf") and not fname.startswith("."):
                    if fname in seen_names:
                        duplicates += 1
                        continue
                    seen_names.add(fname)
                    all_pdfs.append(os.path.join(root, fname))

        if not all_pdfs:
            shutil.rmtree(tmp_dir)
            await status_msg.edit_text(
                "━━━━━━━━━━━━━━━━━━━━\n"
                "      ⚠️  *NO PDFs FOUND*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "ZIP contained no PDF files.\n\n"
                "━━━━━━━━━━━━━━━━━━━━",
                parse_mode="Markdown"
            )
            return

        await status_msg.edit_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "  📥  *PROCESSING ZIP...*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"  ✅  Downloaded\n"
            f"  ✅  Extracted\n"
            f"  🔍  Found *{len(all_pdfs)}* PDFs\n"
            f"  ♻️  Uploading to storage...",
            parse_mode="Markdown"
        )

        # Save each PDF to storage and register in DB
        added = 0
        skipped = 0
        for pdf_path in all_pdfs:
            original_name = os.path.basename(pdf_path)
            branded = branded_name(original_name)
            dest = STORAGE_DIR / branded

            # Copy with branded name
            shutil.copy2(pdf_path, dest)

            # Add to DB using local path as identifier
            result = db.add_file_local(str(dest), original_name, branded)
            if result:
                added += 1
            else:
                skipped += 1
                os.remove(dest)  # already exists

        # Cleanup temp
        shutil.rmtree(tmp_dir)

        total_stock = db.get_stock_count()
        await status_msg.edit_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "      ✅  *ZIP PROCESSED*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"  📂  Total PDFs found  :  *{len(all_pdfs)}*\n"
            f"  ✅  Added to stock    :  *{added}*\n"
            f"  ♻️  Duplicates skipped :  *{duplicates + skipped}*\n\n"
            f"  📦  Total stock now  :  *{total_stock} files*\n\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"ZIP processing error: {e}")
        await status_msg.edit_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "      ❌  *UPLOAD FAILED*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Error: `{str(e)[:100]}`\n\n"
            "Please try again.\n\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )


# ─── Deliver files to buyer ───────────────────────────────────────────────────

async def deliver_files(bot, buyer_id: int, files: list, quantity: int):
    """Package files into branded ZIP and send to buyer."""
    date_str = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_name = f"{BOT_USERNAME}-{quantity}-{date_str}.zip"
    tmp_dir  = tempfile.mkdtemp()
    zip_path = os.path.join(tmp_dir, zip_name)

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for file_path, branded_name_str in files:
                if os.path.exists(file_path):
                    zf.write(file_path, branded_name_str)

        with open(zip_path, 'rb') as zf:
            await bot.send_document(
                chat_id=buyer_id,
                document=zf,
                filename=zip_name,
                caption=f"📦  *{quantity} files*  |  {BOT_USERNAME}",
                parse_mode="Markdown"
            )
        return True
    except Exception as e:
        logger.error(f"Delivery failed: {e}")
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ─── Webhook (Nowpayments IPN) ────────────────────────────────────────────────

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

        file_records = db.claim_files(quantity, buyer_id, buyer_username)

        if not file_records:
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
            f"  📦  Packaging *{quantity}* files...\n"
            "  _This may take a moment._",
            parse_mode="Markdown"
        )

        success = await deliver_files(tg_app.bot, buyer_id, file_records, quantity)

        if success:
            await tg_app.bot.send_message(
                buyer_id,
                "━━━━━━━━━━━━━━━━━━━━\n"
                "    🎉  *DELIVERY COMPLETE*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"  ✅  *{quantity}* files delivered\n\n"
                "  Thank you for your purchase!\n"
                "  Need help?  /support\n\n"
                "━━━━━━━━━━━━━━━━━━━━",
                parse_mode="Markdown"
            )
        else:
            await tg_app.bot.send_message(buyer_id,
                "⚠️  Delivery failed. Contact /support with your order ID.")

        remaining = db.get_stock_count()
        await tg_app.bot.send_message(
            ADMIN_ID,
            "━━━━━━━━━━━━━━━━━━━━\n"
            "       🛒  *NEW SALE*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"  👤  Buyer       :  @{buyer_username}\n"
            f"  🆔  ID            :  `{buyer_id}`\n"
            f"  📦  Files        :  *{quantity}*\n"
            f"  💵  USD          :  *${order['total_usd']}*\n"
            f"  🇮🇩  IDR          :  *Rp {order['total_idr']:,}*\n"
            f"  📊  Remaining  :  *{remaining} files*\n\n"
            f"  🔖  Order        :  `{order_id}`\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )

    return web.Response(status=200, text="OK")


async def health_check(request):
    return web.Response(text="OK")


# ─── Daily promo ──────────────────────────────────────────────────────────────

async def send_daily_promo(bot):
    """Send 1 random available file to promo channel daily."""
    try:
        promo = db.get_random_file_for_promo()
        if not promo:
            logger.info("No files for promo.")
            return

        file_path, branded = promo
        if not os.path.exists(file_path):
            logger.warning(f"Promo file missing: {file_path}")
            return

        with open(file_path, 'rb') as f:
            await bot.send_document(
                chat_id=PROMO_CHANNEL,
                document=f,
                filename=branded,
                caption=f"🗂  *Sample File*\n\n📦  Get yours — order at {BOT_USERNAME}",
                parse_mode="Markdown"
            )
        logger.info("Daily promo sent.")
    except Exception as e:
        logger.error(f"Promo failed: {e}")


async def schedule_daily_promo(app):
    """Run daily promo at 09:00 UTC every day."""
    while True:
        now = datetime.utcnow()
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target.replace(day=target.day + 1)
        wait_seconds = (target - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        await send_daily_promo(app.bot)


# ─── Admin commands ───────────────────────────────────────────────────────────

@admin_only
async def stock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = db.get_stock_count()
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "      📦  *STOCK STATUS*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  Available  :  *{count} files*\n\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown"
    )


@admin_only
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /broadcast Your message")
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


@admin_only
async def promo_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force send promo immediately."""
    await send_daily_promo(context.bot)
    await update.message.reply_text("✅  Promo sent to channel.")


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["step"] = "awaiting_support"
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "       🆘  *SUPPORT*\n"
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
    await update.message.reply_text("✅  Sent to store owner. We'll reply soon!")
    context.user_data.clear()


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "       📖  *COMMANDS*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "  /start    —  Home\n"
        "  /buy       —  Purchase files\n"
        "  /support  —  Contact us\n"
        "  /help      —  This menu\n\n"
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
    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()

    tg_app.add_handler(CommandHandler("start",     start))
    tg_app.add_handler(CommandHandler("buy",       buy))
    tg_app.add_handler(CommandHandler("stock",     stock_cmd))
    tg_app.add_handler(CommandHandler("support",   support))
    tg_app.add_handler(CommandHandler("help",      help_cmd))
    tg_app.add_handler(CommandHandler("broadcast", broadcast))
    tg_app.add_handler(CommandHandler("promonow",  promo_now))
    tg_app.add_handler(CallbackQueryHandler(confirm_callback))
    tg_app.add_handler(MessageHandler(filters.Document.ALL,            handle_document))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    web_app = web.Application()
    web_app["tg_app"] = tg_app
    web_app.router.add_post("/webhook", webhook_handler)
    web_app.router.add_get("/",         health_check)

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling()

    # Start daily promo scheduler
    asyncio.create_task(schedule_daily_promo(tg_app))

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info(f"Bot running on port {PORT}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
