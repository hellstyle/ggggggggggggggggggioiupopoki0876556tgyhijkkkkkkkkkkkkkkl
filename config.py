import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Bot token from environment variable
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("No BOT_TOKEN found in environment variables")

# Уровень логирования из переменной окружения (например, INFO, DEBUG, WARNING)
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()

# File paths
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / 'data'
TRIGGERS_FILE = DATA_DIR / 'triggers.json'
BANNED_USERS_FILE = DATA_DIR / 'banned_users.json'
BACKUP_DIR = BASE_DIR / 'backups'

# Ensure data directory exists
DATA_DIR.mkdir(exist_ok=True)
BACKUP_DIR.mkdir(exist_ok=True)

# Default admin ID (can be set in .env)
ADMIN_IDS = [int(id_.strip()) for id_ in os.getenv('ADMIN_IDS', '').split(',') if id_.strip().isdigit()]
# Bot settings
MESSAGE_LIMIT = 5  # Max messages before anti-spam triggers
TIME_WINDOW = 10   # Time window in seconds for anti-spam
BACKUP_RETENTION_DAYS = 7 # Keep backups for 7 days

# Moderation settings
MAX_WARNINGS = 2  # Number of warnings (for spam, caps, etc.) before mute
MUTE_DURATION_MINUTES = 30  # Mute duration in minutes
CAPS_THRESHOLD = 8  # Number of uppercase letters to trigger a warning
MAX_IDENTICAL_MESSAGES_BEFORE_WARN = 3 # Number of identical messages to trigger a spam warning
AVATAR_HASH_THRESHOLD = 5 # Порог схожести для аватарок (чем меньше, тем строже). 5 - стандарт.
MODERATE_ADMINS = False # Применять ли авто-модерацию (спам, капс, Zalgo) к администраторам.
MODERATE_BOTS = False # Применять ли авто-модерацию (проверка профиля, спам, ссылки) к ботам.

# Настройки обнаружения Zalgo-текста
ZALGO_MIN_DIACRITICS = 4  # Минимальное количество "искажающих" символов для срабатывания (уменьшено с 8).
ZALGO_RATIO_THRESHOLD = 0.5  # Порог соотношения искажающих символов к *базовым* символам (уменьшено с 0.8).

# Messages
MESSAGES = {
    'welcome': '👋 Добро пожаловать в бота модератора!',
    'not_admin': '⛔ У вас нет прав для выполнения этой команды.',
    'user_banned': '🚫 Пользователь @{username} был забанен. Причина: {reason}',
    'user_unbanned': '✅ Пользователь @{username} разбанен.',
    'user_not_found': '❌ Пользователь не найден.',
    'spam_detected': '⚠️ Обнаружен спам!',
    'command_usage': 'Использование: {command}',
}
