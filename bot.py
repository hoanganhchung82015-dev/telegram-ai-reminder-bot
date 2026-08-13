import os
import json
import re
import logging
from datetime import datetime, timedelta
import pytz
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, ContextTypes, filters
)
from google import genai
from google.genai.errors import ServerError, APIError
from aiohttp import web

# 1. Cấu hình Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)

# 2. Lấy API Token & Key từ môi trường
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

ai_client = genai.Client(api_key=GEMINI_API_KEY)
TIMEZONE = pytz.timezone("Asia/Ho_Chi_Minh")

# Các model dự phòng
PRIMARY_MODEL = "gemini-2.5-flash"
FALLBACK_MODELS = ["gemini-1.5-flash", "gemini-2.0-flash"]


async def generate_content_with_fallback(prompt: str):
    """Gửi yêu cầu tới Gemini, tự động chuyển model dự phòng nếu quá tải"""
    models_to_try = [PRIMARY_MODEL] + FALLBACK_MODELS
    last_exception = None

    for model_name in models_to_try:
        try:
            logging.info(f"Đang gọi model: {model_name}")
            response = ai_client.models.generate_content(
                model=model_name, contents=prompt
            )
            return response
        except (ServerError, APIError) as e:
            logging.warning(f"Model {model_name} gặp lỗi API: {e}. Đang thử model khác...")
            last_exception = e
            await asyncio.sleep(1)
        except Exception as e:
            logging.error(f"Lỗi hệ thống khi gọi {model_name}: {e}")
            last_exception = e
            break

    raise last_exception


# --- HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 **Chào bạn! Tôi là Trợ lý Thư ký AI.**\n\n"
        "Tôi có thể giúp bạn:\n"
        "1. **Đặt lịch nhắc nhở:** Hãy nhắn tin tự nhiên như: *'Nhắc tôi 14:30 gửi bài báo cáo'* hoặc *'Nhắc tôi 5 phút nữa đi uống nước'*\n"
        "2. **Tóm tắt nội dung:** Paste văn bản hoặc ý tưởng dài vào đây để tôi tóm tắt giúp bạn."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def send_reminder_notification(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = job_data["chat_id"]
    content = job_data["content"]

    keyboard = [
        [InlineKeyboardButton("✅ Đã hoàn thành", callback_data="task_done")],
        [InlineKeyboardButton("💤 Nhắc lại sau 10 phút", callback_data=f"snooze_10_{content}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔔 **ĐÃ ĐẾN GIỜ NHẮC NHỞ!**\n\n📌 **Nội dung:** {content}",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def process_reminder_with_ai(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    now_vn = datetime.now(TIMEZONE)
    current_time_str = now_vn.strftime("%Y-%m-%d %H:%M:%S")

    prompt = f"""
    Thời gian hiện tại: {current_time_str} (Múi giờ Việt Nam - ICT).
    Phân tích câu nhắn của người dùng: "{user_text}"

    Hãy trả về đúng 1 định dạng JSON (không kèm văn bản giải thích thêm) theo cấu trúc:
    {{
        "is_reminder": true,
        "datetime": "YYYY-MM-DD HH:MM:SS",
        "task": "Nội dung công việc"
    }}

    Quy tắc:
    - Nếu là yêu cầu nhắc nhở/đặt lịch: is_reminder = true.
    - Nếu câu có dạng "X phút nữa", "Y giờ nữa", hãy cộng thêm số phút/giờ đó vào thời gian hiện tại {current_time_str}.
    - Nếu không phải nhắc nhở: is_reminder = false.
    """

    try:
        response = await generate_content_with_fallback(prompt)
        raw_text = response.text or ""
        logging.info(f"AI Output Raw: {raw_text}")

        # Dùng Regex lọc lấy chuỗi JSON
        json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if not json_match:
            await summarize_text_with_ai(update, context, user_text)
            return

        data = json.loads(json_match.group(0))

        if data.get("is_reminder") and data.get("datetime"):
            target_time_str = data["datetime"]
            task_content = data.get("task") or "Nhắc nhở công việc"

            naive_dt = datetime.strptime(target_time_str, "%Y-%m-%d %H:%M:%S")
            target_time = TIMEZONE.localize(naive_dt)

            if target_time <= now_vn:
                await update.message.reply_text("⚠️ Thời gian hẹn giờ đã qua. Bạn vui lòng chọn thời điểm trong tương lai nhé!")
                return

            if context.job_queue:
                context.job_queue.run_once(
                    send_reminder_notification,
                    when=target_time,
                    chat_id=update.effective_chat.id,
                    data={"chat_id": update.effective_chat.id, "content": task_content}
                )

                await update.message.reply_text(
                    f"✅ **Đã hẹn giờ thành công!**\n\n"
                    f"🕒 **Thời gian:** `{target_time.strftime('%H:%M:%S ngày %d/%m/%Y')}`\n"
                    f"📌 **Nội dung:** {task_content}",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text("❌ Hệ thống hẹn giờ (JobQueue) chưa khởi tạo thành công.")
        else:
            await summarize_text_with_ai(update, context, user_text)

    except Exception as e:
        logging.error(f"Lỗi khi xử lý tin nhắn: {e}", exc_info=True)
        # Báo chi tiết lỗi trực tiếp ra Telegram để dễ theo dõi
        await update.message.reply_text(f"❌ **Phát sinh lỗi:** `{str(e)}`", parse_mode="Markdown")


async def summarize_text_with_ai(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    prompt = f"Hãy tóm tắt ngắn gọn đoạn văn/nội dung sau thành các gạch đầu dòng súc tích:\n\n{text}"
    try:
        response = await generate_content_with_fallback(prompt)
        await update.message.reply_text(f"📝 **Tóm tắt nội dung:**\n\n{response.text}", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Lỗi tóm tắt: {e}")
        await update.message.reply_text(f"❌ Không thể tóm tắt: `{str(e)}`", parse_mode="Markdown")


async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_reminder_with_ai(update, context, update.message.text)


async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "task_done":
        await query.edit_message_text(text=f"{query.message.text}\n\n🎉 **[Trạng thái: Đã hoàn thành]**")
    elif query.data.startswith("snooze_10_"):
        content = query.data.replace("snooze_10_", "")
        if context.job_queue:
            context.job_queue.run_once(
                send_reminder_notification,
                when=600,
                chat_id=query.message.chat_id,
                data={"chat_id": query.message.chat_id, "content": content}
            )
        await query.edit_message_text(text=f"{query.message.text}\n\n💤 **[Đã hoãn lại 10 phút nữa sẽ nhắc lại]**")


# --- WEB SERVER GIỮ APPS ACTIVE ---

async def handle_ping_web(request):
    return web.Response(text="Bot Web Service đang hoạt động 24/7!", status=200)

async def start_web_server():
    web_app = web.Application()
    web_app.router.add_get("/", handle_ping_web)
    web_app.router.add_get("/health", handle_ping_web)

    port = int(os.environ.get("PORT", 10000))
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"🌐 Web server đã chạy trên cổng {port}")


# --- MAIN ---

async def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_messages))

    await start_web_server()

    logging.info("🚀 Bot Web Service đã sẵn sàng!")
    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot đã dừng!")
