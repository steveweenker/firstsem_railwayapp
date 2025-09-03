import asyncio
import io
import logging
import os
import time
import aiohttp
from telegram import Bot, Update
from telegram.error import TelegramError, RetryAfter
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

# Configuration - Get from environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
URL = 'https://results.beup.ac.in/BTech1stSem2024_B2024Results.aspx'
RESULT_URL_TEMPLATE = 'https://results.beup.ac.in/ResultsBTech1stSem2024_B2024Pub.aspx?Sem=I&RegNo={reg_no}'
REG_NO_FILE = 'registration_numbers.txt'

# Validate required environment variables
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")
if not CHAT_ID:
    raise ValueError("CHAT_ID environment variable is required")

# Optimization Parameters
BATCH_SIZE = 20
PARALLEL_REQUESTS = 10
MESSAGE_DELAY = 1
RETRY_MAX_ATTEMPTS = 3

# Monitoring Parameters
CHECK_INTERVAL = 2  # Check every 2 seconds
NOTIFICATION_INTERVAL = 7200  # Notify every 2 hours (7200 seconds)

# Logging Setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def clean_registration_number(reg_no):
    """Ultra-fast validation"""
    return reg_no.strip() if len(reg_no.strip()) == 11 and reg_no.strip().isdigit() else ""

async def fetch_result_html(session, reg_no):
    """Async fetch with connection reuse"""
    url = RESULT_URL_TEMPLATE.format(reg_no=reg_no)
    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            async with session.get(url, timeout=10) as response:
                html = await response.text()
                return None if "Invalid Registration Number" in html else html
        except Exception as e:
            logger.error(f"Fetch error for {reg_no} (attempt {attempt + 1}): {e}")
            if attempt == RETRY_MAX_ATTEMPTS - 1:
                return None
            await asyncio.sleep(2 ** attempt)  # exponential backoff

async def send_telegram_message(bot, message):
    """Non-blocking message sender"""
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message)
        await asyncio.sleep(MESSAGE_DELAY)
    except RetryAfter as e:
        logger.warning(f"Rate limited, sleeping for {e.retry_after} seconds")
        await asyncio.sleep(e.retry_after)
        await send_telegram_message(bot, message)  # Retry
    except TelegramError as e:
        logger.error(f"Message failed: {e}")

async def send_html_file(bot, reg_no, html_content):
    """In-memory file handling"""
    try:
        bio = io.BytesIO(html_content.encode('utf-8'))
        bio.seek(0)
        await bot.send_document(
            chat_id=CHAT_ID,
            document=bio,
            filename=f"{reg_no}_result.html",
            caption=f"Result for {reg_no}"
        )
        return True
    except Exception as e:
        logger.error(f"File send failed for {reg_no}: {e}")
        return False

async def process_batch(bot, session, batch):
    """Parallel batch processor"""
    tasks = []
    for reg_no in batch:
        clean_reg = clean_registration_number(reg_no)
        if not clean_reg:
            await send_telegram_message(bot, f"Invalid format: {reg_no}")
            continue
        tasks.append(fetch_result_html(session, clean_reg))

    results = await asyncio.gather(*tasks)

    successful = []
    for reg_no, html_content in zip(batch, results):
        if html_content and await send_html_file(bot, reg_no, html_content):
            successful.append(reg_no)
            await send_telegram_message(bot, f"✅ Sent: {reg_no}")

    return successful

async def process_registration_file(bot):
    """Optimized file processor"""
    if not os.path.exists(REG_NO_FILE):
        await send_telegram_message(bot, f"❌ File not found: {REG_NO_FILE}")
        return

    try:
        with open(REG_NO_FILE, 'r') as f:
            reg_nos = [line.strip() for line in f if line.strip()]
    except Exception as e:
        logger.error(f"File read error: {e}")
        await send_telegram_message(bot, f"⚠️ File error: {e}")
        return

    if not reg_nos:
        await send_telegram_message(bot, "❌ No registration numbers found")
        return

    total = len(reg_nos)
    await send_telegram_message(bot, f"🚀 Processing {total} numbers (batches of {BATCH_SIZE})...")

    async with aiohttp.ClientSession() as session:
        all_successful = []
        for i in range(0, total, BATCH_SIZE):
            batch = reg_nos[i:i + BATCH_SIZE]
            successful = await process_batch(bot, session, batch)
            all_successful.extend(successful)
            await asyncio.sleep(1)  # Brief pause between batches

        await send_telegram_message(
            bot,
            f"📊 Final: {len(all_successful)}/{total} successful\n"
            f"Failed: {total - len(all_successful)}"
        )

async def monitor_website():
    bot = Bot(token=BOT_TOKEN)
    last_notification_time = 0
    is_first_check = True

    await send_telegram_message(bot, "🔍 Monitoring started (checks every 2 seconds)...")

    async with aiohttp.ClientSession() as session:
        while True:
            current_time = time.time()
            try:
                async with session.get(URL, timeout=10) as response:
                    if response.status == 200:
                        if is_first_check or current_time - last_notification_time >= NOTIFICATION_INTERVAL:
                            await send_telegram_message(bot, "🌐 Website LIVE! Processing...")
                            await process_registration_file(bot)
                            break
                    else:
                        if current_time - last_notification_time >= NOTIFICATION_INTERVAL:
                            await send_telegram_message(
                                bot,
                                f"⚠️ Website returned status {response.status}"
                            )
                            last_notification_time = current_time
                            is_first_check = False
            except Exception as e:
                if current_time - last_notification_time >= NOTIFICATION_INTERVAL:
                    await send_telegram_message(
                        bot,
                        f"❌ Website DOWN - {type(e).__name__}: {str(e)}"
                    )
                    last_notification_time = current_time
                    is_first_check = False

            await asyncio.sleep(CHECK_INTERVAL)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ Turbo Results Bot\n\n"
        "Send registration numbers (1 per line) or /batch to process file"
    )

async def handle_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        await update.message.reply_text("❌ Unauthorized")
        return
    
    bot = context.bot
    await process_registration_file(bot)

async def main():
    # Start monitoring in the background
    asyncio.create_task(monitor_website())
    
    # Start the Telegram bot
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("batch", handle_batch))
    
    logger.info("Bot starting on Railway...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    logger.info("✅ Bot is fully operational on Railway!")
    await send_telegram_message(Bot(token=BOT_TOKEN), "🚀 Bot deployed and running on Railway!")
    
    # Keep running forever
    await asyncio.Future()

if __name__ == '__main__':
    asyncio.run(main())
