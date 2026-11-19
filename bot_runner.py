import time
import logging
import sys
import os

# Добавляем текущую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telegram_bot import HynixTelegramBot

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def main():
    # Секреты задаются только через переменные окружения.
    BOT_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("HYNIX_TELEGRAM_BOT_TOKEN")
    ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID") or os.environ.get("HYNIX_TELEGRAM_CHAT_ID")
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        raise SystemExit("Set BOT_TOKEN and ADMIN_CHAT_ID environment variables before starting the bot.")

    bot = HynixTelegramBot(BOT_TOKEN, ADMIN_CHAT_ID)
    
    logger.info("🤖 Hynix Telegram Bot запущен!")
    logger.info(f"🔐 Админ ID: {ADMIN_CHAT_ID}")
    
    # Отправка приветственного сообщения админу
    bot.send_message(ADMIN_CHAT_ID, """
🤖 <b>Hynix Telegram Bot запущен!</b>

🎛 Используйте /start для открытия админ панели
❓ Используйте /help для списка команд
    """)
    
    offset = 0
    
    try:
        while True:
            # Получение обновлений
            updates = bot.get_updates(offset=offset, timeout=30)
            
            if updates.get("ok"):
                for update in updates.get("result", []):
                    # Обновление offset
                    offset = update.get("update_id", 0) + 1
                    
                    # Обработка сообщения
                    if "message" in update:
                        bot.process_message(update["message"])
                    
                    # Обработка callback (кнопки)
                    elif "callback_query" in update:
                        callback = update["callback_query"]
                        bot.handle_callback(callback)
                    
            time.sleep(1)  # Пауза между запросами
            
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"❌ Ошибка в работе бота: {e}")
        # Отправка уведомления админу об ошибке
        bot.send_message(ADMIN_CHAT_ID, f"❌ <b>Бот упал с ошибкой:</b>\n\n{str(e)}")

if __name__ == "__main__":
    main()