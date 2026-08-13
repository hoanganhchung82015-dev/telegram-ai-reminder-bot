import os
import json
import logging
from datetime import datetime
import pytz
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, ContextTypes, filters
)
from google import genai
from google.genai import types
from google.genai.errors import ServerError, APIError
from aiohttp import web

# 1. Cấu hình Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)

# 2. Biến môi trường
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

ai_client = genai.Client(api_key=GEMINI_API_KEY)
TIMEZONE = pytz.timezone("Asia/Ho_Chi_Minh")

# Danh sách các model theo thứ tự ưu tiên (nếu model đầu quá tải 503 sẽ tự động chuyển model tiếp theo)
PRIMARY_MODEL = "gemini-2.5-flash"
FALLBACK_MODELS = ["gemini-1.5-flash", "gemini-2.0-flash"]


async def generate_content_with_fallback(prompt: str, config=None):
    """Hàm gọi Gemini AI có tự động chuyển model nếu gặp lỗi 503 quá tải"""
    models_to_try = [PRIMARY_MODEL] + FALLBACK_MODELS
    last_exception = None

    for model_name in models_to_try:
        try:
            logging.info(f"Đang gọi Gemini với model: {model_name}")
            if config:
                response = ai_client.models.generate_content(
                    model=model_name, contents=prompt, config=config
                )
            else:
                response = ai_client.models.generate_content(
                    model=model_name, contents=prompt
                )
            return response
        except (ServerError, APIError) as e:
            logging.warning(f"Model {model_name} bị lỗi ({e}). Đang thử model tiếp theo...")
            last_exception = e
            await asyncio.sleep(1)  # Đợi 1s trước khi thử lại
        except Exception as e:
            logging.error(f"Lỗi không xác định khi gọi {model_name}: {e}")
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
    Hôm nay là: {current_time_str} (Múi giờ Việt Nam).
    Phân tích câu nhắn của người dùng: "{user_text}"
    
    Hãy trả về duy nhất một chuỗi JSON chuẩn (không chứa thẻ markdown ```json) có cấu trúc:
    {{
        "is_reminder": true hoặc false,
        "datetime": "YYYY-MM-DD HH:MM:SS",
        "task": "Nội dung nhắc nhở"
    }}
    
    Quy tắc tính time:
    - Nếu câu nhắn bảo "5 phút nữa" và hiện tại là "{current_time_str}" -> Cộng thêm 5 phút.
    - Nếu câu nhắn chỉ có giờ (vd "14:30"), lấy ngày hôm nay (hoặc ngày mai nếu giờ đó đã qua trong ngày).
    """

    try:
        response = await generate_content_with_fallback(prompt)

        clean_text = response.text.replace("```json", "").replace("```", "").strip()
        logging.info(f"AI Response Raw: {clean_text}")

        data = json.loads(clean_text)

        if data.get("is_reminder") and data.get("datetime"):
            target_time_str = data["datetime"]
            task_content = data.get("task") or "Nhắc nhở công việc"
            
            naive_dt = datetime.strptime(target_time_str, "%Y-%m-%d %H:%M:%S")
            target_time = TIMEZONE.localize(naive_dt)

            if target_time <= now_vn:
                await update.message.reply_text("⚠️ Thời gian hẹn giờ đã qua. Bạn vui lòng chọn thời gian ở tương lai nhé!")
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
                    f"🕒 **Thời gian:** `{target_time.strftime('%H:%M ngày %d/%m/%Y')}`\n"
                    f"📌 **Nội dung:** {task_content}",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text("❌ Lỗi hệ thống JobQueue.")
        else:
            await summarize_text_with_ai(update, context, user_text)

    except Exception as e:
        logging.error(f"LỖI CHI TIẾT TẠI BOT: {e}", exc_info=True)
        await update.message.reply_text("❌ AI đang quá tải hoặc gặp sự cố tạm thời. Bạn vui lòng thử lại sau vài giây nhé!")


async def summarize_text_with_ai(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    prompt = f"Hãy tóm tắt ngắn gọn đoạn văn/nội dung sau thành các gạch đầu dòng súc tích:\n\n{text}"
    try:
        response = await generate_content_with_fallback(prompt)
        await update.message.reply_text(f"📝 **Tóm tắt nội dung:**\n\n{response.text}", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Lỗi tóm tắt: {e}")
        await update.message.reply_text("❌ Có lỗi xảy ra khi tóm tắt văn bản.")


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
