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
from aiohttp import web

# 1. Cấu hình Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)

# 2. Lấy API Token & Key từ biến môi trường (Environment Variables trên Render)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "NHẬP_TELEGRAM_TOKEN_TẠI_ĐÂY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "NHẬP_GEMINI_KEY_TẠI_ĐÂY")

# Khởi tạo Gemini Client & Múi giờ Việt Nam
ai_client = genai.Client(api_key=GEMINI_API_KEY)
TIMEZONE = pytz.timezone("Asia/Ho_Chi_Minh")

# --- CÁC HÀM XỬ LÝ TELEGRAM BOT (HANDLERS) ---

# Lệnh /start
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 **Chào bạn! Tôi là Trợ lý Thư ký AI.**\n\n"
        "Tôi có thể giúp bạn:\n"
        "1. **Đặt lịch nhắc nhở:** Hãy nhắn tin tự nhiên như: *'Nhắc tôi 14:30 gửi bài báo cáo'*\n"
        "2. **Tóm tắt nội dung:** Paste văn bản hoặc ý tưởng dài vào đây để tôi tóm tắt giúp bạn."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

# Hàm gửi thông báo nhắc nhở khi đến giờ
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

# Lập lịch đặt nhắc nhở bằng Gemini AI
async def process_reminder_with_ai(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    now_vn = datetime.now(TIMEZONE)
    current_time_str = now_vn.strftime("%Y-%m-%d %H:%M:%S")

    prompt = f"""
    Hôm nay là: {current_time_str}.
    Phân tích câu nhắn sau của người dùng: "{user_text}"
    Trích xuất:
    1. Thời gian nhắc nhở chính xác theo định dạng "YYYY-MM-DD HH:MM:SS". Nếu người dùng chỉ nói giờ (ví dụ 14:30), mặc định lấy ngày hôm nay (hoặc ngày mai nếu giờ đó đã trôi qua hôm nay).
    2. Nội dung công việc cần nhắc.

    Trả về kết quả duy nhất ở dạng JSON chuẩn với cấu trúc:
    {{"is_reminder": true, "datetime": "YYYY-MM-DD HH:MM:SS", "task": "Nội dung công việc"}}
    Nếu câu nhắn KHÔNG PHẢI là yêu cầu nhắc nhở, trả về:
    {{"is_reminder": false}}
    """

    try:
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        cleaned_json = response.text.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(cleaned_json)

        if data.get("is_reminder"):
            target_time_str = data["datetime"]
            task_content = data["task"]
            target_time = TIMEZONE.localize(datetime.strptime(target_time_str, "%Y-%m-%d %H:%M:%S"))

            if target_time <= now_vn:
                await update.message.reply_text("⚠️ Thời gian hẹn giờ nằm trong quá khứ. Bạn vui lòng chọn lại thời gian nhé!")
                return

            # Đặt job hẹn giờ trong Telegram JobQueue
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
            # Nếu không phải câu nhắc nhở -> Gọi AI tóm tắt văn bản
            await summarize_text_with_ai(update, context, user_text)

    except Exception as e:
        logging.error(f"Lỗi AI xử lý: {e}")
        await update.message.reply_text("❌ Không thể xử lý tin nhắn. Bạn vui lòng thử lại nhé!")

# Tóm tắt nội dung bằng Gemini
async def summarize_text_with_ai(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    prompt = f"Hãy tóm tắt ngắn gọn đoạn văn/nội dung sau thành các gạch đầu dòng súc tích, dễ hiểu:\n\n{text}"
    
    try:
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        await update.message.reply_text(f"📝 **Tóm tắt nội dung:**\n\n{response.text}", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text("❌ Có lỗi xảy ra khi tóm tắt văn bản.")

# Xử lý toàn bộ tin nhắn văn bản
async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    await process_reminder_with_ai(update, context, user_text)

# Xử lý sự kiện khi bấm nút trên thông báo nhắc nhở
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "task_done":
        await query.edit_message_text(text=f"{query.message.text}\n\n🎉 **[Trạng thái: Đã hoàn thành]**")
    
    elif query.data.startswith("snooze_10_"):
        content = query.data.replace("snooze_10_", "")
        
        # Hoãn lại 10 phút (600 giây)
        context.job_queue.run_once(
            send_reminder_notification,
            when=600,
            chat_id=query.message.chat_id,
            data={"chat_id": query.message.chat_id, "content": content}
        )
        await query.edit_message_text(text=f"{query.message.text}\n\n💤 **[Đã hoãn lại 10 phút nữa sẽ nhắc lại]**")


# --- PHẦN WEB SERVER (GIỮ BOT THỨC TRÊN RENDER WEB SERVICE) ---

async def handle_ping_web(request):
    """Hàm phản hồi khi UptimeRobot hoặc Browser truy cập vào link Web Service"""
    return web.Response(text="Bot Web Service đang hoạt động 24/7!", status=200)

async def start_web_server():
    """Khởi chạy Web Server bằng aiohttp trên PORT do Render cấp"""
    web_app = web.Application()
    web_app.router.add_get("/", handle_ping_web)
    web_app.router.add_get("/health", handle_ping_web)

    port = int(os.environ.get("PORT", 10000))
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"🌐 Web server đã lắng nghe trên cổng {port}")


# --- HÀM MAIN CHẠY SONG SONG BOT & WEB SERVER ---

async def main():
    # 1. Khởi tạo Telegram Bot App
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Đăng ký Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_messages))

    # 2. Bật Web Server
    await start_web_server()

    # 3. Chạy Bot Polling
    logging.info("🚀 Bot Web Service đã sẵn sàng!")
    async with app:
        await app.start()
        await app.updater.start_polling()
        # Giữ loop chạy liên tục
        await asyncio.Event().wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot đã dừng!")
