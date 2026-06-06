import logging
import os
import asyncio
import hmac
import hashlib
import json
import zipfile
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from aiohttp import web, ClientSession
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
    ApplicationBuilder
)
from database import Database

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN     = os.getenv("BOT_TOKEN")
ADMIN_ID      = int(os.getenv("ADMIN_ID"))
NP_API_KEY    = os.getenv("NP_API_KEY")
NP_IPN_SECRET = os.getenv("NP_IPN_SECRET")
RAILWAY_URL   = os.getenv("RAILWAY_URL")
PORT          = int(os.getenv("PORT", 8080))
PROMO_CHANNEL = "@goatdatabase"
BOT_USERNAME  = "@datagacor_bot"
MIN_ORDER     = 25  # minimum files per order (~$3 USD)

db = Database()

PAYMENT_METHODS = {
    "btc":       ("₿  Bitcoin",      "BTC",  "btc"),
    "eth":       ("⬡  Ethereum",     "ETH",  "eth"),
    "ltc":       ("Ł  Litecoin",     "LTC",  "ltc"),
    "usdttrc20": ("◈  USDT TRC20",   "USDT", "usdttrc20"),
    "trx":       ("◉  Tron",         "TRX",  "trx"),
    "xmr":       ("◎  Monero",       "XMR",  "xmr"),
}

IDR_TO_USD = 0.000061
STORAGE_DIR = Path("storage")
STORAGE_DIR.mkdir(exist_ok=True)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def branded(name: str) -> str:
    return f"{BOT_USERNAME}-{name}"

def get_price_tier(quantity: int):
    if quantity >= 2000:
        return 1000, "Rp 1.000 / file", "2.000+ files"
    elif quantity >= 500:
        return 1500, "Rp 1.500 / file", "500 – 1.999 files"
    else:
        return 2000, "Rp 2.000 / file", f"{MIN_ORDER} – 499 files"

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text(
                "━━━━━━━━━━━━━━━━━━━━\n"
                "      🚫  *ACCESS DENIED*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "This action is restricted to\n"
                "authorized personnel only.\n\n"
                "━━━━━━━━━━━━━━━━━━━━",
                parse_mode="Markdown"
            )
            return
        return await func(update, context)
    return wrapper


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
        "order_description": f"PDF Store · {order_id}",
    }
    async with ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json()
            logger.info(f"NowPayments response: {data}")
            return data

def verify_ipn(body: bytes, sig: str) -> bool:
    expected = hmac.new(NP_IPN_SECRET.encode(), body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, sig.lower())


# ─── UI builders ──────────────────────────────────────────────────────────────

def home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒  Buy Files",  callback_data="go_buy")],
        [InlineKeyboardButton("📊  Pricing",    callback_data="go_pricing")],
        [InlineKeyboardButton("🆘  Support",    callback_data="go_support")],
    ])

def back_keyboard(target="go_home"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("‹  Back", callback_data=target)]
    ])

def payment_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("₿  Bitcoin",    callback_data="pay_btc"),
            InlineKeyboardButton("⬡  Ethereum",   callback_data="pay_eth"),
        ],
        [
            InlineKeyboardButton("Ł  Litecoin",   callback_data="pay_ltc"),
            InlineKeyboardButton("◈  USDT TRC20", callback_data="pay_usdttrc20"),
        ],
        [
            InlineKeyboardButton("◉  Tron",       callback_data="pay_trx"),
            InlineKeyboardButton("◎  Monero",     callback_data="pay_xmr"),
        ],
        [InlineKeyboardButton("‹  Back",          callback_data="go_buy")],
    ])

HOME_TEXT = (
    "━━━━━━━━━━━━━━━━━━━━\n"
    "       🗂  *PDF STORE*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "  ⚡  Instant delivery after payment\n"
    "  🔒  Unique files — never resold\n"
    "  💳  Pay with crypto\n\n"
    "━━━━━━━━━━━━━━━━━━━━"
)

PRICING_TEXT = (
    "━━━━━━━━━━━━━━━━━━━━\n"
    "       💰  *PRICING*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    f"  25 – 499 files\n"
    "  └─  *Rp 2.000 / file*\n\n"
    "  500 – 1.999 files\n"
    "  └─  *Rp 1.500 / file*  🔥\n\n"
    "  2.000+ files\n"
    "  └─  *Rp 1.000 / file*  ⚡ Best\n\n"
    f"  ⚠️  Minimum order: *25 files*\n\n"
    "━━━━━━━━━━━━━━━━━━━━"
)


# ─── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    stock = db.get_stock_count()
    text = HOME_TEXT.replace(
        "━━━━━━━━━━━━━━━━━━━━\n\n  ⚡",
        f"━━━━━━━━━━━━━━━━━━━━\n\n  📦  Stock: *{stock} files*\n\n  ⚡"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=home_keyboard())


# ─── Callback router ──────────────────────────────────────────────────────────

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # ── Home ──
    if data == "go_home":
        context.user_data.clear()
        stock = db.get_stock_count()
        text = HOME_TEXT.replace(
            "━━━━━━━━━━━━━━━━━━━━\n\n  ⚡",
            f"━━━━━━━━━━━━━━━━━━━━\n\n  📦  Stock: *{stock} files*\n\n  ⚡"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=home_keyboard())
        return

    # ── Pricing ──
    if data == "go_pricing":
        await query.edit_message_text(PRICING_TEXT, parse_mode="Markdown", reply_markup=back_keyboard("go_home"))
        return

    # ── Support ──
    if data == "go_support":
        context.user_data["step"] = "awaiting_support"
        await query.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "       🆘  *SUPPORT*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Type your message below.\n"
            "We'll reply as soon as possible.\n\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown",
            reply_markup=back_keyboard("go_home")
        )
        return

    # ── Buy ──
    if data == "go_buy":
        stock = db.get_stock_count()
        if stock == 0:
            await query.edit_message_text(
                "━━━━━━━━━━━━━━━━━━━━\n"
                "      😔  *OUT OF STOCK*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "No files available right now.\n"
                "Check back soon!\n\n"
                "━━━━━━━━━━━━━━━━━━━━",
                parse_mode="Markdown",
                reply_markup=back_keyboard("go_home")
            )
            return
        context.user_data["step"] = "awaiting_quantity"
        context.user_data["msg_id"] = query.message.message_id
        await query.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "       🛒  *NEW ORDER*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"  📦  Stock:  *{stock} files*\n"
            f"  ⚠️  Minimum:  *{MIN_ORDER} files*\n\n"
            "How many files do you want?\n"
            "_(Type a number)_\n\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown",
            reply_markup=back_keyboard("go_home")
        )
        return

    # ── Cancel ──
    if data == "cancel_buy":
        context.user_data.clear()
        stock = db.get_stock_count()
        text = HOME_TEXT.replace(
            "━━━━━━━━━━━━━━━━━━━━\n\n  ⚡",
            f"━━━━━━━━━━━━━━━━━━━━\n\n  📦  Stock: *{stock} files*\n\n  ⚡"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=home_keyboard())
        return

    # ── Payment method selected ──
    if data.startswith("pay_"):
        currency_key = data.replace("pay_", "")
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
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"  ⏳  *GENERATING ADDRESS*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"  🪙  Currency:  *{ticker}*\n"
            f"  _Please wait..._\n\n"
            f"━━━━━━━━━━━━━━━━━━━━",
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

            await query.edit_message_text(
                "━━━━━━━━━━━━━━━━━━━━\n"
                "    💳  *PAYMENT DETAILS*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"  📦  Files       :  *{quantity}*\n"
                f"  🪙  Currency  :  *{pay_cur}*\n"
                f"  💰  Amount    :  *{pay_amount} {pay_cur}*\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "  📬  *Send to:*\n\n"
                f"`{pay_address}`\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"  ⚠️  Send *only {pay_cur}* to this address\n"
                f"  ✅  Files delivered automatically\n"
                f"  ⏱  Expires in *60 minutes*\n"
                "━━━━━━━━━━━━━━━━━━━━",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("‹  New Order", callback_data="go_home")]
                ])
            )

        except Exception as e:
            logger.error(f"Payment error: {e}")
            await query.edit_message_text(
                "━━━━━━━━━━━━━━━━━━━━\n"
                "     ❌  *PAYMENT ERROR*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Failed to generate address.\n"
                "Please try again or contact /support.\n\n"
                "━━━━━━━━━━━━━━━━━━━━",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄  Try Again",  callback_data="go_buy")],
                    [InlineKeyboardButton("‹  Home",        callback_data="go_home")],
                ])
            )

        context.user_data.clear()


# ─── Text input handler ───────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("step")

    if step == "awaiting_quantity":
        text = update.message.text.strip()

        # Delete user's number message to reduce clutter
        try:
            await update.message.delete()
        except Exception:
            pass

        msg_id = context.user_data.get("msg_id")

        if not text.isdigit():
            if msg_id:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=msg_id,
                    text="━━━━━━━━━━━━━━━━━━━━\n"
                         "       🛒  *NEW ORDER*\n"
                         "━━━━━━━━━━━━━━━━━━━━\n\n"
                         "  ❌  Please enter a *valid number*.\n\n"
                         "How many files do you want?\n"
                         "_(Type a number)_\n\n"
                         "━━━━━━━━━━━━━━━━━━━━",
                    parse_mode="Markdown",
                    reply_markup=back_keyboard("go_home")
                )
            return

        quantity = int(text)
        stock = db.get_stock_count()

        error = None
        if quantity < MIN_ORDER:
            error = f"Minimum order is *{MIN_ORDER} files*."
        elif quantity > stock:
            error = f"Only *{stock}* files available."

        if error:
            if msg_id:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=msg_id,
                    text=f"━━━━━━━━━━━━━━━━━━━━\n"
                         f"       🛒  *NEW ORDER*\n"
                         f"━━━━━━━━━━━━━━━━━━━━\n\n"
                         f"  ❌  {error}\n\n"
                         f"How many files do you want?\n"
                         f"_(Type a number)_\n\n"
                         f"━━━━━━━━━━━━━━━━━━━━",
                    parse_mode="Markdown",
                    reply_markup=back_keyboard("go_home")
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

        if msg_id:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg_id,
                text="━━━━━━━━━━━━━━━━━━━━\n"
                     "     🧾  *ORDER SUMMARY*\n"
                     "━━━━━━━━━━━━━━━━━━━━\n\n"
                     f"  📦  Files       :  *{quantity}*\n"
                     f"  🏷  Tier          :  *{tier_label}*\n"
                     f"  💲  Rate          :  *{rate_label}*\n"
                     f"  🇮🇩  Total IDR  :  *Rp {total_idr:,}*\n"
                     f"  💵  Total USD  :  *≈ ${total_usd}*\n\n"
                     "━━━━━━━━━━━━━━━━━━━━\n"
                     "     💳  *SELECT PAYMENT*\n"
                     "━━━━━━━━━━━━━━━━━━━━",
                parse_mode="Markdown",
                reply_markup=payment_keyboard()
            )

    elif step == "awaiting_support":
        buyer = update.effective_user
        try:
            await update.message.delete()
        except Exception:
            pass
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
        await context.bot.send_message(
            update.effective_chat.id,
            "✅  Message sent. We'll reply soon!"
        )
        context.user_data.clear()


# ─── Admin: ZIP upload ────────────────────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "      🚫  *ACCESS DENIED*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "File uploads are restricted.\n\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )
        return

    doc = update.message.document
    mime = doc.mime_type or ""

    if mime not in ("application/zip", "application/x-zip-compressed") and not doc.file_name.endswith(".zip"):
        await update.message.reply_text("❌  Only *.zip* files accepted.", parse_mode="Markdown")
        return

    status_msg = await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "  📥  *PROCESSING ZIP*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "  ⬇️  Downloading...",
        parse_mode="Markdown"
    )

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        tmp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(tmp_dir, doc.file_name)
        await tg_file.download_to_drive(zip_path)

        await status_msg.edit_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "  📥  *PROCESSING ZIP*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "  ✅  Downloaded\n"
            "  📂  Extracting...",
            parse_mode="Markdown"
        )

        extract_dir = os.path.join(tmp_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_dir)
        os.remove(zip_path)

        all_pdfs = []
        seen_names = set()
        duplicates = 0
        for root, dirs, files in os.walk(extract_dir):
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
            await status_msg.edit_text("⚠️  ZIP had no PDF files.")
            return

        await status_msg.edit_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "  📥  *PROCESSING ZIP*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"  ✅  Downloaded\n"
            f"  ✅  Extracted\n"
            f"  🔍  Found *{len(all_pdfs)}* PDFs\n"
            f"  💾  Saving to storage...",
            parse_mode="Markdown"
        )

        added = 0
        skipped = 0
        for pdf_path in all_pdfs:
            original_name = os.path.basename(pdf_path)
            branded_name  = branded(original_name)
            dest = STORAGE_DIR / branded_name
            shutil.copy2(pdf_path, dest)
            result = db.add_file_local(str(dest), original_name, branded_name)
            if result:
                added += 1
            else:
                skipped += 1
                if dest.exists():
                    dest.unlink()

        shutil.rmtree(tmp_dir)
        total_stock = db.get_stock_count()

        await status_msg.edit_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "      ✅  *ZIP DONE*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"  📂  PDFs found      :  *{len(all_pdfs)}*\n"
            f"  ✅  Added to stock  :  *{added}*\n"
            f"  ♻️  Duplicates        :  *{duplicates + skipped}*\n\n"
            f"  📦  Total stock      :  *{total_stock} files*\n\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"ZIP error: {e}")
        await status_msg.edit_text(f"❌  Failed: `{str(e)[:100]}`", parse_mode="Markdown")


# ─── Delivery ─────────────────────────────────────────────────────────────────

async def deliver_files(bot, buyer_id: int, files: list, quantity: int):
    date_str = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_name = f"{BOT_USERNAME}-{quantity}-{date_str}.zip"
    tmp_dir  = tempfile.mkdtemp()
    zip_path = os.path.join(tmp_dir, zip_name)
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for file_path, branded_name in files:
                if os.path.exists(file_path):
                    zf.write(file_path, branded_name)
        with open(zip_path, 'rb') as f:
            await bot.send_document(
                chat_id=buyer_id,
                document=f,
                filename=zip_name,
                caption=f"📦  *{quantity} files*  ·  {BOT_USERNAME}",
                parse_mode="Markdown"
            )
        return True
    except Exception as e:
        logger.error(f"Delivery error: {e}")
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ─── Webhook ──────────────────────────────────────────────────────────────────

async def webhook_handler(request: web.Request):
    body = await request.read()
    sig  = request.headers.get("x-nowpayments-sig", "")
    if not verify_ipn(body, sig):
        return web.Response(status=400, text="Invalid signature")

    data   = json.loads(body)
    status = data.get("payment_status")
    oid    = data.get("order_id")
    logger.info(f"IPN: {status} / {oid}")

    if status in ("finished", "confirmed") and oid:
        order = db.get_order(oid)
        if not order or order["status"] != "pending":
            return web.Response(status=200, text="OK")

        db.mark_order_paid(oid)
        buyer_id  = order["buyer_id"]
        quantity  = order["quantity"]
        username  = order["buyer_username"]
        tg_app    = request.app["tg_app"]
        files     = db.claim_files(quantity, buyer_id, username)

        if not files:
            await tg_app.bot.send_message(buyer_id, "⚠️  Stock ran out. Contact /support. Refund incoming.")
            await tg_app.bot.send_message(ADMIN_ID, f"🚨 STOCK ERROR — @{username} paid for {quantity} files. REFUND NEEDED.")
            return web.Response(status=200, text="OK")

        await tg_app.bot.send_message(
            buyer_id,
            "━━━━━━━━━━━━━━━━━━━━\n"
            "    ✅  *PAYMENT CONFIRMED*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"  📦  Packaging *{quantity}* files...\n"
            "  _Please wait._\n\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )

        success = await deliver_files(tg_app.bot, buyer_id, files, quantity)

        if success:
            await tg_app.bot.send_message(
                buyer_id,
                "━━━━━━━━━━━━━━━━━━━━\n"
                "    🎉  *DELIVERED!*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"  ✅  *{quantity}* files in your ZIP\n\n"
                "  Thank you for your purchase!\n"
                "  Need help?  /support\n\n"
                "━━━━━━━━━━━━━━━━━━━━",
                parse_mode="Markdown"
            )

        remaining = db.get_stock_count()
        await tg_app.bot.send_message(
            ADMIN_ID,
            "━━━━━━━━━━━━━━━━━━━━\n"
            "       🛒  *NEW SALE*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"  👤  @{username}  (`{buyer_id}`)\n"
            f"  📦  Files       :  *{quantity}*\n"
            f"  💵  USD         :  *${order['total_usd']}*\n"
            f"  🇮🇩  IDR         :  *Rp {order['total_idr']:,}*\n"
            f"  📊  Remaining  :  *{remaining}*\n\n"
            f"  🔖  `{oid}`\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )

    return web.Response(status=200, text="OK")


async def health_check(request):
    return web.Response(text="OK")


# ─── Daily promo ──────────────────────────────────────────────────────────────

async def send_daily_promo(bot):
    try:
        promo = db.get_random_file_for_promo()
        if not promo:
            return
        file_path, branded_name = promo
        if not os.path.exists(file_path):
            return
        with open(file_path, 'rb') as f:
            await bot.send_document(
                chat_id=PROMO_CHANNEL,
                document=f,
                filename=branded_name,
                caption=f"🗂  *Sample File*\n\n📦  Order yours at {BOT_USERNAME}",
                parse_mode="Markdown"
            )
        logger.info("Daily promo sent.")
    except Exception as e:
        logger.error(f"Promo error: {e}")


async def schedule_daily_promo(app):
    while True:
        now = datetime.utcnow()
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= target:
            from datetime import timedelta
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        await send_daily_promo(app.bot)


# ─── Admin commands ───────────────────────────────────────────────────────────

@admin_only
async def stock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = db.get_stock_count()
    await update.message.reply_text(
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"      📦  *STOCK STATUS*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  Available  :  *{count} files*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━",
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
    await update.message.reply_text(f"✅  Sent to {sent} users.")

@admin_only
async def promo_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_daily_promo(context.bot)
    await update.message.reply_text("✅  Promo sent.")

async def support_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["step"] = "awaiting_support"
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "       🆘  *SUPPORT*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Type your message below.\n\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown"
    )

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


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()

    tg_app.add_handler(CommandHandler("start",     start))
    tg_app.add_handler(CommandHandler("buy",       lambda u, c: _buy_cmd(u, c)))
    tg_app.add_handler(CommandHandler("stock",     stock_cmd))
    tg_app.add_handler(CommandHandler("support",   support_cmd))
    tg_app.add_handler(CommandHandler("help",      help_cmd))
    tg_app.add_handler(CommandHandler("broadcast", broadcast))
    tg_app.add_handler(CommandHandler("promonow",  promo_now))
    tg_app.add_handler(CallbackQueryHandler(callback_router))
    tg_app.add_handler(MessageHandler(filters.Document.ALL,            handle_document))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    web_app = web.Application()
    web_app["tg_app"] = tg_app
    web_app.router.add_post("/webhook", webhook_handler)
    web_app.router.add_get("/",         health_check)

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling()
    asyncio.create_task(schedule_daily_promo(tg_app))

    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    logger.info(f"Bot live on port {PORT}")
    await asyncio.Event().wait()


async def _buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock = db.get_stock_count()
    if stock == 0:
        await update.message.reply_text("😔  Out of stock. Check back soon!")
        return
    context.user_data["step"] = "awaiting_quantity"
    msg = await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "       🛒  *NEW ORDER*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  📦  Stock:  *{stock} files*\n"
        f"  ⚠️  Minimum:  *{MIN_ORDER} files*\n\n"
        "How many files do you want?\n"
        "_(Type a number)_\n\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=back_keyboard("go_home")
    )
    context.user_data["msg_id"] = msg.message_id


if __name__ == "__main__":
    asyncio.run(main())
