import logging
from telegram.ext import Application, CommandHandler

# Импортируем настройки из config
from config import BOT_TOKEN, LOG_LEVEL, ADMIN_IDS

# Импорт функций для регистрации обработчиков
from handlers.admin_handlers import register_admin_handlers, add_ban_word, list_ban_words
from handlers.member_handlers import register_member_handlers
from handlers.message_handlers import register_message_handlers

# Импорт экземпляра БД для корректной инициализации при старте
from utils.database import db
from telegram import Update

# Настройка логирования
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def post_shutdown(application: Application) -> None:
    """Выполняется при остановке бота."""
    db.close()
    logger.info("Бот остановлен, соединение с БД закрыто.")

def main() -> None:
    """Запуск бота."""
    try:
        logger.info("Запуск бота...")
        
        if not BOT_TOKEN or BOT_TOKEN == 'your_bot_token_here':
            logger.error("ОШИБКА: Токен бота не настроен. Пожалуйста, укажите BOT_TOKEN в файле .env")
            return
            
        # Создание экземпляра Application
        application = (
            Application.builder()
            .token(BOT_TOKEN)
            .post_shutdown(post_shutdown)
            .build()
        )

        # Регистрация всех обработчиков
        register_admin_handlers(application)
        register_member_handlers(application)
        register_message_handlers(application)

        # Добавляем недостающие обработчики
        application.add_handler(CommandHandler("add_ban_word", add_ban_word))
        application.add_handler(CommandHandler("list_ban_words", list_ban_words))
        logger.info("Все обработчики зарегистрированы.")

        # Запуск бота в режиме опроса
        logger.info("Бот запущен и работает...")
        logger.info(f"ID администраторов: {ADMIN_IDS if ADMIN_IDS else 'не указаны'}")
        
        # Запускаем бота с обработкой всех типов обновлений
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True  # Пропускать накопившиеся обновления
        )
    except KeyboardInterrupt:
        logger.info("Получен сигнал (Ctrl+C), инициирую остановку бота...")
        # application.stop() вызовет post_shutdown и корректно завершит работу
        
    except Exception as e:
        logger.error(f"КРИТИЧЕСКАЯ ОШИБКА: {str(e)}")
        logger.exception("Детали ошибки:")

if __name__ == '__main__':
    main()
