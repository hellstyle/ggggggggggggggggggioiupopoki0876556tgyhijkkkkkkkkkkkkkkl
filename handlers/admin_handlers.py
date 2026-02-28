import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions, MessageEntity, ChatMember, Message, MessageOriginChannel, User, Chat
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters, Application, ChatMemberHandler
from telegram.ext.filters import BaseFilter
from telegram.constants import ParseMode, ChatType
from config import MESSAGES, ADMIN_IDS, BACKUP_DIR, AVATAR_HASH_THRESHOLD
from utils.database import db
from utils.database_schema import db_schema
import shutil, os
import re, asyncio
import asyncio
from pathlib import Path
from datetime import datetime, timedelta, time
from typing import Dict, Optional, List, Tuple, Any
from handlers.helpers import resolve_target_user, can_moderate_user, delete_cached_messages
from utils.helpers import schedule_message_deletion, is_admin, is_global_admin, add_bot_message_to_cache
from handlers.permissions import PERMS_UNRESTRICTED, PERMS_FULL_RESTRICT
from utils.image_utils import calculate_phash, compare_phashes
from io import BytesIO
from utils.text_utils import normalize_text
from utils.cleanup_backups import cleanup_old_backups
from handlers.member_handlers import check_username, check_user_avatar, check_user_bio
import uuid

# Configure logger
logger = logging.getLogger(__name__)

# Store support messages waiting for admin response
support_messages: Dict[int, Dict] = {}

# Store admin chat IDs for support messages
admin_chat_ids = set(ADMIN_IDS) if ADMIN_IDS else set()

# To track repetitive messages for anti-spam
user_message_history: Dict[int, Dict[int, List[Tuple[float, str]]]] = {} # chat_id -> user_id -> [(timestamp, text)]

# Custom filter for messages sent from a linked channel.
# This is more robust across PTB versions than relying on a constant that might be missing.
class _SenderChatFilter(BaseFilter):
    """Filters for messages sent on behalf of a channel."""
    def filter(self, message: Message) -> bool:
        return message and message.sender_chat is not None

sender_chat_filter = _SenderChatFilter()

def parse_duration(duration_str: str) -> Optional[timedelta]:
    """
    Parses a duration string like '10m', '2h', '3d' into a timedelta object.
    Returns None if the format is invalid.
    """
    if not duration_str:
        return None
    
    # Regex to capture value and unit (m, h, d)
    match = re.fullmatch(r'(\d+)([mhd])', duration_str.lower())
    if not match:
        return None

    value, unit = match.groups()
    value = int(value)

    if unit == 'm':
        return timedelta(minutes=value)
    if unit == 'h':
        return timedelta(hours=value)
    if unit == 'd':
        return timedelta(days=value)
    
    return None # Should not be reached

# Admin commands
async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
    
    # Send the help message and schedule it for deletion
    help_text = """
<b>📋 Доступные команды админа (сообщение удалится через 3 секунды):</b>

<b>Настройки чата:</b>
/settings - Показать настройки текущего чата

<b>Управление пользователями:</b>
/ban <@username/user_id> [причина] - Забанить пользователя
/unban <@username/user_id> - Разбанить пользователя
/mute <@username/user_id> <время> [причина] - Заглушить пользователя
/unmute <@username/user_id> - Снять заглушку
/warn <@username/user_id> [причина] - Выдать предупреждение
/unwarn <@username/user_id> - Снять предупреждение

<b>Белый список (иммунитет от авто-модерации):</b>
/add_whitelist <@user/id> - Добавить пользователя в белый список
/del_whitelist <@user/id> - Убрать пользователя из белого списка
/list_whitelist - Показать белый список

<b>Управление триггерами (ответами бота):</b>
/add_trigger <слово> <ответ> - Добавить триггер с ответом
/del_trigger <слово> - Удалить триггер
/list_triggers - Показать все триггеры в этом чате

<b>Управление запрещенными словами:</b>
/add_ban_word <слово> - Добавить запрещенное слово в этом чате
/del_ban_word <слово> - Удалить запрещенное слово
/list_ban_words - Показать запрещенные слова в этом чате

<b>Управление запрещенными словами в никах (глобально):</b>
/add_ban_nickname <слово> - Добавить запрещенное слово для ников
/del_ban_nickname <слово> - Удалить запрещенное слово для ников
/list_ban_nicknames - Показать запрещенные слова для ников

<b>Управление запрещенными словами в описании профиля:</b>
/add_ban_bio <слово> - Добавить запрещенное слово для описания
/del_ban_bio <слово> - Удалить запрещенное слово для описания
/list_ban_bios - Показать запрещенные слова для описания

<b>Управление запрещенными доменами:</b>
/add_ban_domain <домен> - Добавить домен для авто-бана
/del_ban_domain <домен> - Удалить домен из списка
/list_ban_domains - Показать список запрещенных доменов

<b>Управление шаблонами бана (глобально):</b>
/add_ban_pattern <регулярное выражение> - Добавить шаблон для бана
/del_ban_pattern <шаблон> - Удалить шаблон
/list_ban_patterns - Показать все шаблоны бана

<b>Управление запрещенными аватарками (глобально, в ЛС с ботом):</b>
Отправьте фото - Добавить аватарку в черный список
/unban_avatar <file_unique_id> - Убрать аватарку из черного списка
/list_banned_avatars - Показать список запрещенных аватарок

<b>Управление админами чата (только для глобальных админов):</b>
/add_chat_admin <@user/id> - Назначить админа в этом чате
/del_chat_admin <@user/id> - Снять админа в этом чате
/list_chat_admins - Показать админов этого чата

<b>Управление правилами чата:</b>
/set_rules <текст> - Установить правила (можно ответом на сообщение)
/del_rules - Удалить правила
/set_rules_ad &lt;текст&gt; - Установить рекламный текст для правил
/del_rules_ad - Удалить рекламный текст для правил
/rules - Показать текущие правила чата

<b>Приветствие новых участников:</b>
/set_welcome <текст> - Установить приветственное сообщение
/del_welcome - Удалить приветствие
/set_welcome_ad &lt;текст&gt; - Установить рекламный текст для приветствия
/del_welcome_ad - Удалить рекламный текст для приветствия
/welcome - Показать текущее приветствие (для админов)

<b>Приветственная капча:</b>
/enable_captcha - Включить проверку для новых участников
/disable_captcha - Выключить проверку

<b>Бан за ссылки:</b>
/enable_linkban - Включить бан за отправку любых ссылок
/disable_linkban - Выключить бан за ссылки

<b>Восстановление прав:</b>
/ask <@username/user_id> — Разрешить пользователю отправлять только сообщения и медиа (без стикеров, опросов и т. п.)

ℹ️ Примечание: Триггеры и запрещенные слова работают только в том чате, где они были добавлены, если не указано иное.

<b>Обслуживание:</b>
/backup - Создать и отправить резервную копию базы данных
    """
    sent_message = await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)
    schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id, delay=3)
    schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id, delay=3)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help for all users."""
    help_text = """
<b>Доступные команды (сообщение удалится через 3 секунды):</b>

<b>Основные команды:</b>
/start - Начать работу с ботом
/help - Показать это сообщение
/rules - Показать правила чата

<b>Информация:</b>
/profile - Показать ваш профиль
/stats - Статистика чата

<b>Для администраторов:</b>
/admin - Показать команды администратора

Если у вас есть вопросы, обратитесь к администраторам чата.
    """
    sent_message = await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)
    schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id, delay=3)
    schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id, delay=3)

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows user's profile with karma and punishment stats."""
    if not update.effective_chat or not update.effective_user:
        return

    user = update.effective_user
    chat_id = update.effective_chat.id

    # Get karma from DB for the current chat
    karma = db.get_user_karma(chat_id, user.id)

    # Get global punishment stats from DB
    punishments = db.get_user_global_punishment_stats(user.id)
    total_punishments = punishments.get('total', 0)
    punishment_emoji = "😇" if total_punishments == 0 else "😈"

    # Get join date from DB
    join_date_str = db.get_user_join_date(chat_id, user.id)
    if join_date_str:
        # Format date like '2023-10-30 15:45'
        join_date_formatted = join_date_str.split('.')[0]
        join_date_line = f"• <b>В чате с:</b> {join_date_formatted}\n"
    else:
        join_date_line = ""

    profile_text = (
        f"👤 <b>Ваш профиль в чате «{update.effective_chat.title}»</b>\n\n"
        f"• <b>ID:</b> <code>{user.id}</code>\n"
        f"• <b>Имя:</b> {user.full_name}\n"
        f"{join_date_line}"
        f"• <b>Репутация (карма):</b> {karma} ✨\n"
        f"• <b>Наказаний в сети чатов:</b> {total_punishments} {punishment_emoji}"
    )

    sent_message = await update.message.reply_text(profile_text, parse_mode=ParseMode.HTML)
    schedule_message_deletion(context.job_queue, chat_id, update.message.message_id, delay=10)
    schedule_message_deletion(context.job_queue, chat_id, sent_message.message_id, delay=10)

# Chat-specific configuration commands
async def chat_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current chat settings."""
    if not update.effective_chat:
        return
        
    chat_id = update.effective_chat.id
    triggers = db.get_chat_triggers(chat_id)
    ban_words = db.get_chat_ban_words(chat_id)
    ban_links_enabled = db.is_link_deletion_enabled(chat_id)
    captcha_enabled = db.is_welcome_captcha_enabled(chat_id)
    link_status_emoji = "✅" if ban_links_enabled else "❌"
    captcha_status_emoji = "✅" if captcha_enabled else "❌"
    
    text = (
        f"⚙️ *Настройки чата* {update.effective_chat.title or 'ЛС'}\n\n"
        f"• *Триггеры*: {len(triggers)}\n"
        f"• *Запрещенные слова*: {len(ban_words)}\n"
        f"• *Бан за ссылки*: {link_status_emoji}\n"
        f"• *Приветственная капча*: {captcha_status_emoji}"
    )
    
    sent_message = await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    # Schedule only the response for deletion
    schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)

# Trigger management commands
async def add_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a trigger for the current chat."""
    if not update.effective_chat:
        return # Should not happen in a group
        
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
        
    if len(context.args) < 2:
        sent_message = await update.message.reply_text("Использование: /add_trigger <слово> <ответ>")
        schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
        schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)
        return
        
    chat_id = update.effective_chat.id
    trigger_raw = context.args[0]
    word = normalize_text(trigger_raw)
    response = ' '.join(context.args[1:])
    
    if db.add_trigger(chat_id, word, response):
        sent_message = await update.message.reply_text(
            f"✅ Триггер добавлен в этот чат.\n"
            f"*Триггер*: {trigger_raw}\n"
            f"*Ответ*: {response}",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        sent_message = await update.message.reply_text(f"⚠️ Ошибка при добавлении триггера '{word}'")
    
    # Schedule both the command and the response for deletion
    schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
    schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)

async def del_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a trigger from the current chat."""
    if not update.effective_chat:
        return
        
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
        
    if not context.args:
        sent_message = await update.message.reply_text("Использование: /del_trigger <слово>")
        schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
        schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)
        return
        
    chat_id = update.effective_chat.id
    trigger_raw = context.args[0]
    word = normalize_text(trigger_raw)
    
    if db.remove_trigger(chat_id, word):
        sent_message = await update.message.reply_text(f"✅ Триггер '{trigger_raw}' удалён из этого чата.")
    else:
        sent_message = await update.message.reply_text(f"⚠️ Триггер '{trigger_raw}' не найден в этом чате.")
    
    schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
    schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)

async def list_triggers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all triggers for the current chat."""
    if not update.effective_chat:
        return
        
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
        
    chat_id = update.effective_chat.id
    triggers = db.get_chat_triggers(chat_id)
    
    if not triggers:
        sent_message = await update.message.reply_text("📭 В этом чате нет триггеров.")
    else:
        trigger_list = "📌 *Триггеры этого чата:*\n\n" + "\n".join(
            f"• `{t[0]}` → {t[1]}" for t in triggers
        )
        sent_message = await update.message.reply_text(trigger_list, parse_mode=ParseMode.MARKDOWN)
    
    # Schedule both the command and the response for deletion
    schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
    if 'sent_message' in locals():
        schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)

# Ban patterns commands
async def add_ban_pattern(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_global_admin(update.effective_user.id):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
        
    if not context.args:
        await update.message.reply_text("❌ Использование: /add_ban_pattern <регулярное выражение>")
        return
        
    pattern = ' '.join(context.args)
    try:
        # Test if pattern is valid
        re.compile(pattern)
        if db.add_ban_pattern(pattern):
            await update.message.reply_text(f"✅ Паттерн для авто-бана добавлен: `{pattern}`", 
                                         parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text("⚠️ Такой паттерн уже существует.")
    except re.error as e:
        await update.message.reply_text(f"❌ Ошибка в регулярном выражении: {str(e)}")

async def del_ban_pattern(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_global_admin(update.effective_user.id):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
        
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❌ Использование: /del_ban_pattern <номер>\n"
                                      "Используйте /list_ban_patterns чтобы увидеть номера паттернов")
        return
        
    patterns = db.get_ban_patterns()
    try:
        index = int(context.args[0]) - 1
        if 0 <= index < len(patterns):
            # patterns is a list of dicts, we need the 'pattern' value
            pattern_to_delete = patterns[index]['pattern']
            if db.remove_ban_pattern(pattern_to_delete):
                await update.message.reply_text(f"✅ Паттерн удалён: `{pattern_to_delete}`", 
                                             parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await update.message.reply_text("❌ Не удалось удалить паттерн.")
        else:
            await update.message.reply_text("❌ Неверный номер паттерна.")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Ошибка при обработке номера паттерна.")

async def list_ban_patterns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_global_admin(update.effective_user.id):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
        
    patterns = db.get_ban_patterns()
    if not patterns:
        await update.message.reply_text("📭 Список паттернов для авто-бана пуст.")
    else:
        patterns_list = "\n".join(
            f"{i+1}. `{p['pattern']}`" 
            for i, p in enumerate(patterns)
        )
        await update.message.reply_text(
            f"📌 Список паттернов для авто-бана:\n{patterns_list}",
            parse_mode=ParseMode.MARKDOWN_V2
        )

# Chat Admins Management (Global Admins only)
async def add_chat_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Appoint a user as a chat-specific admin."""
    if not update.effective_chat or not update.message or not update.effective_user:
        return
    if not await is_global_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда доступна только глобальным администраторам бота.")
        return
    
    target_user = await resolve_target_user(update, context)
    if not target_user:
        await update.message.reply_text(
            "Использование: /add_chat_admin <@user/id> или ответьте на сообщение."
        )
        return

    chat_id = update.effective_chat.id
    if db.add_chat_admin(chat_id, target_user.id, update.effective_user.id):
        await update.message.reply_text(
            f"✅ {target_user.mention_html()} назначен(а) администратором в этом чате.",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            f"ℹ️ {target_user.mention_html()} уже является администратором в этом чате.",
            parse_mode=ParseMode.HTML
        )

async def del_chat_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a user's chat-specific admin rights."""
    if not update.effective_chat or not update.message or not update.effective_user:
        return
    if not await is_global_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда доступна только глобальным администраторам бота.")
        return
    
    target_user = await resolve_target_user(update, context)
    if not target_user:
        await update.message.reply_text(
            "Использование: /del_chat_admin <@user/id> или ответьте на сообщение."
        )
        return

    chat_id = update.effective_chat.id
    if db.remove_chat_admin(chat_id, target_user.id):
        await update.message.reply_text(
            f"✅ {target_user.mention_html()} больше не является администратором в этом чате.",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            f"ℹ️ {target_user.mention_html()} не был(а) администратором в этом чате.",
            parse_mode=ParseMode.HTML
        )

async def list_chat_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all bot admins for the current chat."""
    if not update.effective_chat or not update.message:
        return

    chat_id = update.effective_chat.id
    chat_admins_ids = db.get_chat_admins(chat_id)
    
    admin_list_text = "<b>Администраторы бота в этом чате:</b>\n\n"
    
    admin_list_text += "<b>Глобальные:</b>\n"
    global_admin_mentions = [f"• {(await context.bot.get_chat(admin_id)).mention_html()}" for admin_id in ADMIN_IDS]
    admin_list_text += "\n".join(global_admin_mentions) if global_admin_mentions else "• <i>Нет</i>"

    admin_list_text += "\n\n<b>Локальные:</b>\n"
    chat_admin_mentions = [f"• {(await context.bot.get_chat(admin_id)).mention_html()}" for admin_id in chat_admins_ids]
    admin_list_text += "\n".join(chat_admin_mentions) if chat_admin_mentions else "• <i>Нет</i>"

    await update.message.reply_text(admin_list_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# --- Whitelist Management ---
async def add_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Adds a user to the whitelist for the current chat."""
    if not update.effective_chat or not update.message or not update.effective_user:
        return
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    target_user = await resolve_target_user(update, context)
    if not target_user:
        await update.message.reply_text(
            "Использование: /add_whitelist <@user/id> или ответьте на сообщение."
        )
        return

    chat_id = update.effective_chat.id
    if db.add_whitelist_user(chat_id, target_user.id, update.effective_user.id):
        await update.message.reply_text(
            f"✅ {target_user.mention_html()} добавлен(а) в белый список. Авто-модерация на него/неё не действует.",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            f"ℹ️ {target_user.mention_html()} уже в белом списке.",
            parse_mode=ParseMode.HTML
        )

async def del_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removes a user from the whitelist for the current chat."""
    if not update.effective_chat or not update.message or not update.effective_user:
        return
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    target_user = await resolve_target_user(update, context)
    if not target_user:
        await update.message.reply_text(
            "Использование: /del_whitelist <@user/id> или ответьте на сообщение."
        )
        return

    chat_id = update.effective_chat.id
    if db.remove_whitelist_user(chat_id, target_user.id):
        await update.message.reply_text(
            f"✅ {target_user.mention_html()} удалён(а) из белого списка.",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            f"ℹ️ {target_user.mention_html()} не был(а) в белом списке.",
            parse_mode=ParseMode.HTML
        )

async def list_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists all whitelisted users for the current chat."""
    if not update.effective_chat or not update.message:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    chat_id = update.effective_chat.id
    whitelisted_ids = db.get_whitelisted_users(chat_id)
    
    if not whitelisted_ids:
        await update.message.reply_text("ℹ️ Белый список для этого чата пуст.")
        return

    user_mentions = []
    for user_id in whitelisted_ids:
        try:
            user = await context.bot.get_chat(user_id)
            user_mentions.append(f"• {user.mention_html()} (<code>{user_id}</code>)")
        except Exception:
            user_mentions.append(f"• <i>Неизвестный пользователь</i> (<code>{user_id}</code>)")

    whitelist_text = "<b>🛡️ Белый список (иммунитет к авто-модерации):</b>\n\n" + "\n".join(user_mentions)
    await update.message.reply_text(whitelist_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# Rules management
async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the rules for the current chat."""
    if not update.effective_chat or not update.message:
        return

    chat_id = update.effective_chat.id
    # Assuming db.get_chat_rules(chat_id) exists and returns the rules text or None
    rules = db.get_chat_rules(chat_id)
    ad_text = db.get_rules_ad(chat_id)

    if rules or ad_text:
        final_text = rules or ""
        if ad_text:
            final_text += f"\n\n{ad_text}"

        # Using HTML parse mode for better formatting, assuming rules are stored with HTML tags
        sent_message = await update.message.reply_text(
            final_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        # We cache the final composed text
        add_bot_message_to_cache(chat_id, final_text)
    else:
        sent_message = await update.message.reply_text("ℹ️ Правила для этого чата не установлены. Администратор может добавить их командой /set_rules.")
        add_bot_message_to_cache(chat_id, sent_message.text)

    # Schedule deletion to keep chat clean, consistent with other commands
    schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id, delay=15)
    schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id, delay=15)

async def set_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the rules for the current chat. Can be used as a reply."""
    if not update.effective_chat or not update.message:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    rules_text = ""
    # 1. Check for replied message (text or caption)
    if update.message.reply_to_message:
        rules_text = update.message.reply_to_message.text or update.message.reply_to_message.caption
    # 2. Check for arguments if not a reply
    elif context.args:
        rules_text = ' '.join(context.args)

    if not rules_text:
        await update.message.reply_text(
            "Использование: /set_rules <текст правил>\n"
            "Либо ответьте этой командой на сообщение, которое нужно сделать правилами."
        )
        return

    chat_id = update.effective_chat.id

    # Assuming db.set_chat_rules(chat_id, rules_text) exists
    if db.set_chat_rules(chat_id, rules_text):
        await update.message.reply_text("✅ Правила для этого чата обновлены.")
    else:
        await update.message.reply_text("❌ Произошла ошибка при обновлении правил.")


async def del_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete the rules for the current chat."""
    if not update.effective_chat or not update.message:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    chat_id = update.effective_chat.id
    # Assuming db.delete_chat_rules(chat_id) exists
    if db.delete_chat_rules(chat_id):
        await update.message.reply_text("✅ Правила для этого чата удалены.")
    else:
        await update.message.reply_text("ℹ️ Для этого чата правила не были установлены или произошла ошибка при удалении.")

async def set_rules_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the ad text for the rules."""
    if not update.effective_chat or not update.message:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    ad_text = ' '.join(context.args)

    if not ad_text:
        await update.message.reply_text(
            "<b>Использование:</b> /set_rules_ad &lt;текст рекламы&gt;\n\n"
            "Текст будет добавлен в конец правил. Вы можете использовать HTML-разметку.\n"
            "<b>Пример скрытой ссылки:</b>\n"
            "<code>/set_rules_ad &lt;a href=\"https://t.me/my_channel\"&gt;&#8204;&lt;/a&gt;Реклама нашего канала</code>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        return

    chat_id = update.effective_chat.id
    # Assuming db.set_rules_ad(chat_id, ad_text) exists
    if db.set_rules_ad(chat_id, ad_text):
        await update.message.reply_text("✅ Рекламный текст для правил обновлен.")
    else:
        await update.message.reply_text("❌ Произошла ошибка при обновлении рекламного текста.")

async def del_rules_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete the ad text for the rules."""
    if not update.effective_chat or not update.message:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    chat_id = update.effective_chat.id
    # Assuming db.delete_rules_ad(chat_id) exists
    if db.delete_rules_ad(chat_id):
        await update.message.reply_text("✅ Рекламный текст для правил удален.")
    else:
        await update.message.reply_text("ℹ️ Рекламный текст для правил не был установлен или произошла ошибка.")

async def set_rules_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the ad text for the rules."""
    if not update.effective_chat or not update.message:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    ad_text = ' '.join(context.args)

    if not ad_text:
        await update.message.reply_text(
            "<b>Использование:</b> /set_rules_ad &lt;текст рекламы&gt;\n\n"
            "Текст будет добавлен в конец правил. Вы можете использовать HTML-разметку.\n"
            "<b>Пример скрытой ссылки:</b>\n"
            "<code>/set_rules_ad &lt;a href=\"https://t.me/my_channel\"&gt;&#8204;&lt;/a&gt;Реклама нашего канала</code>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        return

    chat_id = update.effective_chat.id
    if db.set_rules_ad(chat_id, ad_text):
        await update.message.reply_text("✅ Рекламный текст для правил обновлен.")
    else:
        await update.message.reply_text("❌ Произошла ошибка при обновлении рекламного текста.")

async def del_rules_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete the ad text for the rules."""
    if not update.effective_chat or not update.message:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    chat_id = update.effective_chat.id
    if db.delete_rules_ad(chat_id):
        await update.message.reply_text("✅ Рекламный текст для правил удален.")
    else:
        await update.message.reply_text("ℹ️ Рекламный текст для правил не был установлен или уже удален.")

# Welcome message management
async def set_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the welcome message for the current chat."""
    if not update.effective_chat or not update.message or not update.effective_user:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    welcome_text = ""
    # Use HTML from replied message to preserve formatting, or plain text as fallback
    if update.message.reply_to_message:
        welcome_text = update.message.reply_to_message.text_html or update.message.reply_to_message.text
    elif context.args:
        welcome_text = ' '.join(context.args)

    if not welcome_text:
        await update.message.reply_text(
            "Использование: <code>/set_welcome &lt;текст приветствия&gt;</code>\n"
            "Либо ответьте этой командой на сообщение.\n\n"
            "<b>Доступные переменные:</b>\n"
            "<code>{user_mention}</code> - упоминание пользователя\n"
            "<code>{chat_title}</code> - название чата\n"
            "<code>{first_name}</code> - имя пользователя",
            parse_mode=ParseMode.HTML
        )
        return

    chat_id = update.effective_chat.id
    if db.set_welcome_message(chat_id, welcome_text):
        await update.message.reply_text("✅ Приветственное сообщение обновлено.")
    else:
        await update.message.reply_text("❌ Произошла ошибка при обновлении приветствия.")

async def del_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete the welcome message for the current chat."""
    if not update.effective_chat or not update.message or not update.effective_user:
        return
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
    chat_id = update.effective_chat.id
    if db.delete_welcome_message(chat_id):
        await update.message.reply_text("✅ Приветственное сообщение удалено.")
    else:
        await update.message.reply_text("ℹ️ Приветствие не было установлено.")

async def set_welcome_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the ad text for the welcome message."""
    if not update.effective_chat or not update.message:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    ad_text = ' '.join(context.args)

    if not ad_text:
        await update.message.reply_text(
            "<b>Использование:</b> /set_welcome_ad &lt;текст рекламы&gt;\n\n"
            "Текст будет добавлен в конец приветствия. Вы можете использовать HTML-разметку.\n"
            "<b>Пример скрытой ссылки:</b>\n"
            "<code>/set_welcome_ad &lt;a href=\"https://t.me/my_channel\"&gt;&#8204;&lt;/a&gt;Реклама нашего канала</code>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        return

    chat_id = update.effective_chat.id
    # Assuming db.set_welcome_ad(chat_id, ad_text) exists
    if db.set_welcome_ad(chat_id, ad_text):
        await update.message.reply_text("✅ Рекламный текст для приветствия обновлен.")
    else:
        await update.message.reply_text("❌ Произошла ошибка при обновлении рекламного текста.")

async def del_welcome_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete the ad text for the welcome message."""
    if not update.effective_chat or not update.message:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    chat_id = update.effective_chat.id
    # Assuming db.delete_welcome_ad(chat_id) exists
    if db.delete_welcome_ad(chat_id):
        await update.message.reply_text("✅ Рекламный текст для приветствия удален.")
    else:
        await update.message.reply_text("ℹ️ Рекламный текст для приветствия не был установлен или произошла ошибка.")

async def set_welcome_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the ad text for the welcome message."""
    if not update.effective_chat or not update.message:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    ad_text = ' '.join(context.args)

    if not ad_text:
        await update.message.reply_text(
            "<b>Использование:</b> /set_welcome_ad &lt;текст рекламы&gt;\n\n"
            "Текст будет добавлен в конец приветствия. Вы можете использовать HTML-разметку.\n"
            "<b>Пример скрытой ссылки:</b>\n"
            "<code>/set_welcome_ad &lt;a href=\"https://t.me/my_channel\"&gt;&#8204;&lt;/a&gt;Реклама нашего канала</code>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        return

    chat_id = update.effective_chat.id
    if db.set_welcome_ad(chat_id, ad_text):
        await update.message.reply_text("✅ Рекламный текст для приветствия обновлен.")
    else:
        await update.message.reply_text("❌ Произошла ошибка при обновлении рекламного текста.")

async def del_welcome_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete the ad text for the welcome message."""
    if not update.effective_chat or not update.message:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    chat_id = update.effective_chat.id
    if db.delete_welcome_ad(chat_id):
        await update.message.reply_text("✅ Рекламный текст для приветствия удален.")
    else:
        await update.message.reply_text("ℹ️ Рекламный текст для приветствия не был установлен или уже удален.")

async def show_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the current welcome message for admins to preview."""
    if not update.effective_chat or not update.message or not update.effective_user:
        return
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
    chat_id = update.effective_chat.id
    welcome_settings = db.get_welcome_message(chat_id)
    if welcome_settings and welcome_settings.get("text"):
        await update.message.reply_text(
            "Текущее приветственное сообщение:\n\n" + welcome_settings["text"],
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
    else:
        await update.message.reply_text("ℹ️ Приветствие не установлено.")

async def set_captcha_status(update: Update, context: ContextTypes.DEFAULT_TYPE, enabled: bool):
    """Helper to enable or disable welcome captcha."""
    if not update.effective_chat or not update.message or not update.effective_user:
        return
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
    chat_id = update.effective_chat.id
    if db.set_welcome_captcha(chat_id, enabled):
        status = "включена" if enabled else "выключена"
        await update.message.reply_text(f"✅ Приветственная проверка (капча) {status}.")
    else:
        await update.message.reply_text("❌ Произошла ошибка при изменении настройки.")

async def enable_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_captcha_status(update, context, enabled=True)

async def disable_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_captcha_status(update, context, enabled=False)

async def set_link_ban_status(update: Update, context: ContextTypes.DEFAULT_TYPE, enabled: bool):
    """Helper to enable or disable link banning."""
    if not update.effective_chat or not update.message or not update.effective_user:
        return
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
    chat_id = update.effective_chat.id
    if db.set_link_deletion(chat_id, enabled):
        status = "включен" if enabled else "выключен"
        await update.message.reply_text(f"✅ Автоматический бан за ссылки {status}.")
    else:
        await update.message.reply_text("❌ Произошла ошибка при изменении настройки.")

async def enable_linkban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_link_ban_status(update, context, enabled=True)

async def disable_linkban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_link_ban_status(update, context, enabled=False)

async def handle_banned_avatar_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles a photo sent by an admin in a private chat to add or remove it from the banned avatars list."""
    # 1. Check for private chat, global admin, and photo
    if not (update.message and update.effective_user and update.message.photo and update.effective_chat.type == ChatType.PRIVATE):
        return

    if not await is_global_admin(update.effective_user.id):
        # Silently ignore photos from non-admins in PM
        return

    admin_id = update.effective_user.id
    
    avatar_to_process = update.message.photo[-1]
    file_unique_id = avatar_to_process.file_unique_id
    file_id = avatar_to_process.file_id

    try:
        photo_file = await avatar_to_process.get_file()
        photo_bytes_io = BytesIO()
        await photo_file.download_to_memory(photo_bytes_io)
        photo_bytes = photo_bytes_io.getvalue()
        
        phash = await calculate_phash(photo_bytes)
        
        if not phash:
            await update.message.reply_text("❌ Не удалось обработать изображение для создания хэша.")
            return

        # Check if this exact avatar is banned by file_unique_id
        if db.is_avatar_banned(file_unique_id):
            keyboard = [[
                InlineKeyboardButton("Да, убрать из бана", callback_data=f"unban_avatar_confirm_{file_unique_id}"),
                InlineKeyboardButton("Отмена", callback_data="unban_avatar_cancel"),
            ]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "ℹ️ Эта аватарка уже находится в списке запрещенных. Хотите убрать ее?",
                reply_markup=reply_markup
            )
            return

        # Check if a similar avatar is banned by phash
        banned_avatars = db.get_banned_avatars()
        for banned_avatar in banned_avatars:
            if banned_avatar.get('phash') and compare_phashes(phash, banned_avatar['phash'], threshold=AVATAR_HASH_THRESHOLD):
                file_unique_id_to_unban = banned_avatar['file_unique_id']
                keyboard = [[
                    InlineKeyboardButton("Да, убрать из бана", callback_data=f"unban_avatar_confirm_{file_unique_id_to_unban}"),
                    InlineKeyboardButton("Отмена", callback_data="unban_avatar_cancel"),
                ]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    "ℹ️ Похожая аватарка уже находится в списке запрещенных. Хотите убрать ее?",
                    reply_markup=reply_markup
                )
                return

        # If not banned, add it
        if db.add_banned_avatar(file_unique_id, file_id, phash, admin_id):
            await update.message.reply_text("✅ Аватарка добавлена в глобальный черный список. Пользователи с такой аватаркой будут автоматически забанены.")
        else:
            # This case should ideally not be reached if the checks above are correct, but as a fallback.
            await update.message.reply_text("❌ Произошла ошибка при добавлении аватарки в базу данных. Возможно, она уже была добавлена другим способом.")

    except Exception as e:
        logger.error(f"Error processing photo for banning/unbanning: {e}", exc_info=True)
        await update.message.reply_text("❌ Произошла ошибка при загрузке или обработке фото.")

async def unban_avatar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removes an avatar from the banned list by its file_unique_id."""
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("❌ Эту команду можно использовать только в личных сообщениях с ботом.")
        return

    if not await is_global_admin(update.effective_user.id):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    if not context.args:
        await update.message.reply_text("❌ Использование: /unban_avatar <file_unique_id>\nID можно получить из списка /list_banned_avatars.")
        return

    file_unique_id = context.args[0]
    if db.remove_banned_avatar(file_unique_id):
        await update.message.reply_text(f"✅ Аватарка с ID `{file_unique_id}` удалена из списка запрещенных.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("❌ Аватарка с таким ID не найдена в списке запрещенных.")

async def unban_avatar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the confirmation callback for avatar unbanning."""
    query = update.callback_query
    await query.answer()

    if not await is_global_admin(query.from_user.id):
        await query.edit_message_text("⛔ У вас нет прав для выполнения этой команды.")
        return

    data = query.data
    
    if data == "unban_avatar_cancel":
        await query.edit_message_text("❌ Разблокировка отменена.")
        return

    if data.startswith("unban_avatar_confirm_"):
        file_unique_id = data.replace("unban_avatar_confirm_", "")
        if db.remove_banned_avatar(file_unique_id):
            await query.edit_message_text("✅ Аватарка удалена из списка запрещенных.")
        else:
            await query.edit_message_text("❌ Аватарка не найдена в списке запрещенных. Возможно, она уже была удалена.")
        return


async def list_banned_avatars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists all banned avatars by sending their photos."""
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("❌ Эту команду можно использовать только в личных сообщениях с ботом.")
        return

    if not await is_global_admin(update.effective_user.id):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    banned_avatars = db.get_banned_avatars()
    if not banned_avatars:
        await update.message.reply_text("ℹ️ Список запрещенных аватарок пуст.")
        return

    await update.message.reply_text(f"🚫 <b>Список запрещенных аватарок ({len(banned_avatars)}):</b>", parse_mode=ParseMode.HTML)

    for avatar in banned_avatars:
        file_id = avatar.get('file_id')
        file_unique_id = avatar['file_unique_id']
        phash = avatar.get('phash')
        
        caption = f"<b>ID:</b> <code>{file_unique_id}</code>"
        if phash:
            caption += f"\n<b>pHash:</b> <code>{phash}</code>"
        caption += f"\n\nЧтобы удалить, используйте:\n/unban_avatar {file_unique_id}"

        if file_id:
            try:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.warning(f"Could not send banned avatar photo with file_id {file_id}. Error: {e}")
                # Fallback to text if sending photo fails
                await context.bot.send_message(chat_id=update.effective_chat.id, text=f"⚠️ Не удалось отправить фото для аватарки.\n{caption}", parse_mode=ParseMode.HTML)
        else:
            # Fallback for old entries without file_id
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"ℹ️ Нет `file_id` для этой аватарки (старая запись).\n{caption}", parse_mode=ParseMode.HTML)
        await asyncio.sleep(0.5) # Avoid hitting rate limits

# Maintenance commands
async def backup_database(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create and send a backup of the database."""
    if not update.message or not update.effective_user:
        return

    if not await is_global_admin(update.effective_user.id):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    try:
        # 1. Get the database file path from db_schema
        source_db_path = Path(db_schema.db_path)
        if not source_db_path.exists():
            await update.message.reply_text("❌ Файл базы данных не найден.")
            return

        # 2. Define backup file path
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_filename = f"backup_{source_db_path.stem}_{timestamp}{source_db_path.suffix}"
        backup_filepath = BACKUP_DIR / backup_filename

        # 3. Copy the database file
        shutil.copyfile(source_db_path, backup_filepath)

        # 4. Send the backup file
        with open(backup_filepath, 'rb') as backup_file:
            await update.message.reply_document(
                document=backup_file,
                filename=backup_filename,
                caption=f"✅ Резервная копия базы данных от {timestamp} сохранена на сервере."
            )
    except Exception as e:
        logger.error(f"Error creating database backup: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Произошла ошибка при создании резервной копии: {e}")

async def restore_database(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /restore command with a database file in a private chat."""
    # 1. Check for private chat, global admin, and document with the correct caption
    if not (update.message and update.effective_user and update.message.document and update.effective_chat.type == ChatType.PRIVATE):
        return

    if not (update.message.caption and update.message.caption.strip() == '/restore'):
        return  # Silently ignore documents without the right caption

    if not await is_global_admin(update.effective_user.id):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    # 2. Check file type (basic check)
    if not update.message.document.file_name.lower().endswith(('.db', '.sqlite', '.sqlite3')):
        await update.message.reply_text("❌ Неверный тип файла. Пожалуйста, отправьте файл базы данных SQLite (.db).")
        return

    # 3. Confirmation step
    keyboard = [[
        InlineKeyboardButton("✅ Да, восстановить", callback_data=f"restore_confirm_{update.message.id}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"restore_cancel_{update.message.id}"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Store file_id in context for the callback handler, keyed by message_id to avoid conflicts
    context.bot_data.setdefault('restore_requests', {})[update.message.id] = {
        'file_id': update.message.document.file_id,
        'file_name': update.message.document.file_name,
        'user_id': update.effective_user.id
    }

    await update.message.reply_text(
        "⚠️ <b>ВНИМАНИЕ!</b> Вы собираетесь заменить текущую базу данных. "
        "Это действие необратимо и приведет к потере всех текущих данных (кроме логов и бэкапов).\n\n"
        "Текущая база данных будет сохранена в качестве резервной копии.\n\n"
        "Вы уверены, что хотите продолжить?",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )

async def restore_database_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the confirmation callback for database restoration."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not await is_global_admin(user_id):
        await query.edit_message_text("⛔ У вас нет прав для выполнения этой команды.")
        return

    try:
        _, action, message_id_str = query.data.split('_')
        message_id = int(message_id_str)
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Ошибка в данных обратного вызова. Попробуйте снова.")
        return

    restore_requests = context.bot_data.get('restore_requests', {})
    request_data = restore_requests.get(message_id)

    if not request_data or request_data['user_id'] != user_id:
        await query.edit_message_text("❌ Запрос на восстановление не найден или истек. Пожалуйста, отправьте файл снова.")
        return

    if action == "cancel":
        restore_requests.pop(message_id, None)
        await query.edit_message_text("❌ Восстановление отменено.")
        return

    if action == "confirm":
        await query.edit_message_text("⏳ Начинаю процесс восстановления... Пожалуйста, подождите.")
        temp_restore_path = None
        try:
            db_file = await context.bot.get_file(request_data['file_id'])
            current_db_path = Path(db_schema.db_path)
            temp_restore_path = BACKUP_DIR / f"restore_temp_{request_data['file_name']}"
            await db_file.download_to_drive(custom_path=temp_restore_path)

            with open(temp_restore_path, 'rb') as f:
                if f.read(16) != b'SQLite format 3\x00':
                    await query.edit_message_text("❌ Ошибка: загруженный файл не является действительной базой данных SQLite.")
                    return

            backup_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            current_db_backup_path = BACKUP_DIR / f"pre-restore-backup_{current_db_path.name}_{backup_timestamp}"
            
            logger.info("Closing database connections for restore...")
            db.close()
            
            shutil.copyfile(current_db_path, current_db_backup_path)
            logger.info(f"Current database backed up to {current_db_backup_path}")
            
            os.replace(temp_restore_path, current_db_path)
            logger.info(f"Database restored from uploaded file {request_data['file_name']}")

            await query.edit_message_text(
                "✅ База данных успешно восстановлена.\n\n"
                "‼️ <b>ВАЖНО:</b> Для применения изменений <b>необходимо перезапустить бота</b>. "
                "Без перезапуска бот может работать нестабильно или с ошибками.",
                parse_mode=ParseMode.HTML
            )

        except Exception as e:
            logger.error(f"Error during database restore: {e}", exc_info=True)
            await query.edit_message_text(f"❌ Ошибка во время восстановления: {e}")
        finally:
            restore_requests.pop(message_id, None)
            if temp_restore_path and temp_restore_path.exists():
                temp_restore_path.unlink()

async def scheduled_backup(context: ContextTypes.DEFAULT_TYPE):
    """Creates and sends a scheduled backup of the database to all admins."""
    logger.info("Running scheduled database backup...")
    backup_filepath = None
    try:
        # 1. Get the database file path
        source_db_path = Path(db_schema.db_path)
        if not source_db_path.exists():
            logger.error("Scheduled backup failed: Database file not found at %s.", source_db_path)
            return

        # 2. Define backup file path
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_filename = f"backup_{source_db_path.stem}_{timestamp}{source_db_path.suffix}"
        backup_filepath = BACKUP_DIR / backup_filename

        # 3. Copy the database file
        shutil.copyfile(source_db_path, backup_filepath)

        # 4. Send the backup file to all admins
        if not ADMIN_IDS:
            logger.warning("Scheduled backup created, but no ADMIN_IDS are configured to send it to.")
            # We still keep the backup file, so we don't return here.

        sent_to_admins = []
        for admin_id in ADMIN_IDS:
            try:
                with open(backup_filepath, 'rb') as backup_file:
                    await context.bot.send_document(
                        chat_id=admin_id,
                        document=backup_file,
                        filename=backup_filename,
                        caption=f"✅ Ежедневная резервная копия от {timestamp} сохранена на сервере."
                    )
                sent_to_admins.append(str(admin_id))
            except Exception as e:
                logger.error(f"Failed to send scheduled backup to admin {admin_id}: {e}")
        
        if sent_to_admins:
            logger.info(f"Scheduled backup successfully sent to admins: {', '.join(sent_to_admins)}")

    except Exception as e:
        logger.error(f"Error creating scheduled database backup: {e}", exc_info=True)
        # Notify an admin about the failure
        if ADMIN_IDS:
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"❌ Ошибка при создании автоматической резервной копии: {e}"
                    )
                except Exception as admin_e:
                    logger.error(f"Failed to send backup error notification to admin {admin_id}: {admin_e}")

async def global_ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the confirmation callback for a global ban."""
    query = update.callback_query
    await query.answer()

    if not await is_global_admin(query.from_user.id):
        await query.edit_message_text("⛔ У вас нет прав для выполнения этой команды.")
        return

    data = query.data

    if data == "global_ban_reject":
        await query.edit_message_text("❌ Глобальный бан отклонен.")
        return

    if data.startswith("global_ban_confirm_"):
        try:
            user_id_to_ban = int(data.split("_")[-1])
        except (ValueError, IndexError):
            await query.edit_message_text("❌ Ошибка: неверный ID пользователя в callback data.")
            return

        try:
            user = await context.bot.get_chat(user_id_to_ban)
        except Exception as e:
            logger.error(f"Could not fetch user {user_id_to_ban} for global ban: {e}")
            await query.edit_message_text(f"❌ Не удалось получить информацию о пользователе {user_id_to_ban}.")
            return

        reason = "Глобальный бан по решению администратора после авто-бана."
        if db.ban_user(
            user_id=user.id, reason=reason, admin_id=query.from_user.id,
            username=user.username, first_name=user.first_name, last_name=user.last_name,
        ):
            await query.edit_message_text(
                f"✅ Пользователь {user.mention_html()} (<code>{user.id}</code>) добавлен в глобальный черный список.",
                parse_mode=ParseMode.HTML
            )
        else:
            await query.edit_message_text(
                f"ℹ️ Пользователь {user.mention_html()} (<code>{user.id}</code>) уже находится в глобальном черном списке.",
                parse_mode=ParseMode.HTML
            )

async def propose_automated_rule(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user: User):
    """After a manual ban, propose adding a rule based on the context."""
    admin_user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Case 1: Ban was a reply to a message
    if update.message.reply_to_message and update.message.reply_to_message.text:
        message_text = update.message.reply_to_message.text
        # Store the text to be banned, as it can be long for callback_data
        request_id = str(uuid.uuid4())
        context.bot_data.setdefault('ban_proposals', {})[request_id] = {
            'type': 'message',
            'text': message_text,
            'chat_id': chat_id,
            'admin_id': admin_user.id
        }
        
        keyboard = [[
            InlineKeyboardButton("✅ Добавить в запрещенные слова", callback_data=f"auto_rule_add_word_{request_id}"),
            InlineKeyboardButton("❌ Пропустить", callback_data=f"auto_rule_skip_{request_id}")
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"ℹ️ Пользователь забанен. Хотите добавить его сообщение в список запрещенных слов чата?\n\n"
            f"<blockquote>{message_text}</blockquote>",
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
        return

    # Case 2: Ban was by username/ID, check profile
    buttons = []
    # Fetch full user info for bio
    try:
        user_chat_info = await context.bot.get_chat(target_user.id)
        bio = getattr(user_chat_info, 'bio', None)
    except Exception:
        bio = None

    # Store user info for the callback
    request_id = str(uuid.uuid4())
    context.bot_data.setdefault('ban_proposals', {})[request_id] = {
        'type': 'profile',
        'chat_id': chat_id,
        'admin_id': admin_user.id,
        'first_name': target_user.first_name,
        'last_name': target_user.last_name,
        'bio': bio
    }

    if target_user.first_name:
        buttons.append([InlineKeyboardButton(f"🚫 Запретить имя (чат): '{target_user.first_name}'", callback_data=f"auto_rule_add_name_first_{request_id}")])
        buttons.append([InlineKeyboardButton(f"🚫🌍 Запретить имя (глобально): '{target_user.first_name}'", callback_data=f"auto_rule_add_name_first_global_{request_id}")])
    if target_user.last_name:
        buttons.append([InlineKeyboardButton(f"🚫 Запретить фамилию (чат): '{target_user.last_name}'", callback_data=f"auto_rule_add_name_last_{request_id}")])
        buttons.append([InlineKeyboardButton(f"🚫🌍 Запретить фамилию (глобально): '{target_user.last_name}'", callback_data=f"auto_rule_add_name_last_global_{request_id}")])
    if bio:
        # Truncate long bios for the button text
        bio_short = (bio[:30] + '...') if len(bio) > 30 else bio
        buttons.append([InlineKeyboardButton(f"🚫 Запретить описание (чат): '{bio_short}'", callback_data=f"auto_rule_add_bio_{request_id}")])
        buttons.append([InlineKeyboardButton(f"🚫🌍 Запретить описание (глобально): '{bio_short}'", callback_data=f"auto_rule_add_bio_global_{request_id}")])
    
    if not buttons:
        # Nothing to suggest banning from profile
        return

    buttons.append([InlineKeyboardButton("❌ Пропустить", callback_data=f"auto_rule_skip_{request_id}")])
    reply_markup = InlineKeyboardMarkup(buttons)
    
    await update.message.reply_text(
        "ℹ️ Пользователь забанен. Хотите добавить часть его профиля в автоматические фильтры?",
        reply_markup=reply_markup
    )

# User management commands
async def ask_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow a user to send messages and media only."""
    if not await is_admin(update):
        sent = await update.message.reply_text(MESSAGES['not_admin'])
        # Clean up command and response
        schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
        schedule_message_deletion(context.job_queue, sent.chat.id, sent.message_id)
        return

    if not update.effective_chat:
        return

    # Allow using as reply or with argument
    if not context.args and not update.message.reply_to_message:
        sent = await update.message.reply_text(
            "❌ Использование: /ask <user_id> или ответьте на сообщение с /ask"
        )
        schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
        schedule_message_deletion(context.job_queue, sent.chat.id, sent.message_id)
        return

    target_user = await resolve_target_user(update, context)
    if not target_user:
        sent = await update.message.reply_text("❌ Не удалось определить пользователя. Укажите numeric user_id или ответьте на сообщение.")
        schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
        schedule_message_deletion(context.job_queue, sent.chat.id, sent.message_id)
        return

    chat_id = update.effective_chat.id
    # Build permissions: allow messages + media only (granular fields)
    perms = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=True,
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
        can_manage_topics=False,
    )

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target_user.id,
            permissions=perms
        )
        mention = target_user.mention_html()
        sent = await update.message.reply_text(
            f"✅ Пользователю {mention} разрешено отправлять сообщения и медиа. Другие права ограничены."
        )
    except Exception as e:
        sent = await update.message.reply_text(f"⚠️ Не удалось обновить права пользователя: {e}")

    # Clean up command and response
    schedule_message_deletion(context.job_queue, sent.chat.id, sent.message_id)

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
        
    target_user = await resolve_target_user(update, context)
    if not target_user:
        await update.message.reply_text("❌ Использование: /ban <@username/user_id> [причина] или ответьте на сообщение с /ban")
        return

    # --- Permission Checks ---
    # Re-implemented permission checks to ensure manual admin commands are not
    # affected by the MODERATE_BOTS auto-moderation setting. An admin should be
    # able to moderate anyone except another admin.
    if target_user.id == context.bot.id:
        await update.message.reply_text("🤖 Я не могу забанить сам себя.")
        return

    # Check against bot's own admin list (DB)
    target_is_bot_admin = await is_global_admin(target_user.id) or db.is_chat_admin(update.effective_chat.id, target_user.id)
    if target_is_bot_admin:
        await update.message.reply_text("⛔ Нельзя забанить другого администратора бота.")
        return

    # Check against Telegram's admin list (API)
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, target_user.id)
        if member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            await update.message.reply_text("⛔ Нельзя забанить администратора или владельца чата.")
            return
    except Exception as e:
        logger.warning(f"Could not check admin status for target user {target_user.id} via API: {e}")

    # Determine the reason for the ban
    # If it's a reply, all args are the reason. Otherwise, args after the user mention.
    reason_args = context.args if update.message.reply_to_message else context.args[1:]
    reason = " ".join(reason_args) if reason_args else "Нарушение правил чата"

    # Delete the command message
    try:
        await update.message.delete()
    except Exception as e:
        logger.error(f"Error deleting ban command message: {e}")
        
    # If this was a reply to a message, delete the replied message too
    if update.message.reply_to_message:
        try:
            await update.message.reply_to_message.delete()
        except Exception as e:
            logger.error(f"Error deleting replied message: {e}")
            
    # Delete the ban confirmation message after a delay
    try:
        sent = await update.message.reply_text("⏳ Бан в процессе...")
        schedule_message_deletion(context.job_queue, update.effective_chat.id, sent.message_id, delay=5)
    except Exception as e:
        logger.error(f"Error sending ban confirmation: {e}")
        
    # Ban the user
    if db.ban_user(user_id=target_user.id, 
                   reason=reason, 
                   admin_id=update.effective_user.id,
                   username=target_user.username,
                   first_name=target_user.first_name,
                   last_name=target_user.last_name):
        
        # Try to actually ban the user from the chat
        try:
            await context.bot.ban_chat_member(
                chat_id=update.effective_chat.id,
                user_id=target_user.id,
                revoke_messages=True
            )
            # As a fallback, also delete any messages seen by the bot
            await delete_cached_messages(context, update.effective_chat.id, target_user.id)
            
            # Send message to the chat
            user_mention = target_user.mention_markdown()
            ban_text = (
                f"🚫 {user_mention} был(а) забанен(а) администратором.\n"
                f"Причина: {reason}"
            )
            await update.message.reply_text(ban_text, parse_mode=ParseMode.MARKDOWN)
            add_bot_message_to_cache(update.effective_chat.id, ban_text)

            # Propose adding a rule based on this ban
            await propose_automated_rule(update, context, target_user)
            
        except Exception as e:
            logger.error(f"Error banning user {target_user.id}: {e}")
            await update.message.reply_text(
                f"⚠ Пользователь добавлен в черный список, но не удалось его забанить в чате (или удалить его сообщения). "
                f"Убедитесь, что у бота есть права администратора на бан пользователей и удаление сообщений.\n"
                f"Ошибка: {str(e)}"
            )
    else:
        await update.message.reply_text("❌ Этот пользователь уже в черном списке.")

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
        
    target_user = await resolve_target_user(update, context)
    if not target_user:
        await update.message.reply_text("❌ Использование: /unban <@username/user_id> или ответьте на сообщение с /unban")
        return
    
    # Unban the user
    if db.unban_user(target_user.id):
        
        # Try to actually unban the user from the chat
        try:
            await context.bot.unban_chat_member(
                chat_id=update.effective_chat.id,
                user_id=target_user.id
            )
            
            # Generate invite link
            try:
                invite_link = await context.bot.create_chat_invite_link(
                    chat_id=update.effective_chat.id,
                    member_limit=1,
                    name=f"unban_{target_user.id}"
                )
                invite_text = f"\n\n🔗 Ссылка для приглашения: {invite_link.invite_link}"
            except Exception as e:
                logger.error(f"Error creating invite link: {e}")
                invite_text = "\n\n⚠ Не удалось создать пригласительную ссылку. Убедитесь, что у бота есть права на создание пригласительных ссылок."
            
            # Send message to the chat
            user_mention = target_user.mention_markdown()
            admin_mention = f"@{update.effective_user.username}" if update.effective_user.username else f"[Администратор](tg://user?id={update.effective_user.id})"
            
            unban_text = (
                f"👋 {user_mention} был(а) разбанен(а) администратором {admin_mention}.{invite_text}"
            )
            await update.message.reply_text(
                unban_text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
            )
            add_bot_message_to_cache(update.effective_chat.id, unban_text)
            
            # Try to send a direct message to the unbanned user
            try:
                if target_user.username or target_user.first_name:
                    welcome_back = (
                        f"👋 {target_user.first_name or ''} {target_user.last_name or ''}, вы были разбанены в чате "
                        f"{update.effective_chat.title or 'чате'}. "
                        f"Вы можете вернуться по пригласительной ссылке выше."
                    )
                    await context.bot.send_message(
                        chat_id=target_user.id,
                        text=f"{welcome_back}{invite_text if 'invite_link' in locals() else ''}"
                    )
            except Exception as e:
                logger.error(f"Error sending DM to unbanned user: {e}")
            
        except Exception as e:
            logger.error(f"Error unbanning user {target_user.id}: {e}")
            await update.message.reply_text(
                f"⚠ Пользователь удален из черного списка, но не удалось его разбанить в чате. "
                f"Убедитесь, что у бота есть права администратора на бан пользователей.\n"
                f"Ошибка: {str(e)}"
            )
    else:
        await update.message.reply_text("❌ Этот пользователь не в черном списке.")

async def mute_user(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE, 
    prefilled_duration: Optional[timedelta] = None, 
    prefilled_reason: Optional[str] = None
):
    """Mute a user for a specified duration (e.g., 10m, 2h, 3d)."""
    if not update.effective_chat or not update.message or not update.effective_user:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    # --- Argument and User Resolution ---
    duration_str = ""
    reason = ""
    
    if prefilled_duration:
        target_user = update.effective_user
        duration = prefilled_duration
        reason = prefilled_reason or "Нарушение правил чата."
    else:
        target_user = await resolve_target_user(update, context)
        if update.message.reply_to_message:
            # /mute <time> [reason]
            duration_str = context.args[0] if context.args else ""
            reason = ' '.join(context.args[1:])
        else:
            # /mute <user> <time> [reason]
            duration_str = context.args[1] if len(context.args) > 1 else ""
            reason = ' '.join(context.args[2:])

    if not target_user or not duration_str:
        await update.message.reply_text(
            "<b>Использование:</b>\n"
            "<code>/mute &lt;@user/id&gt; &lt;время&gt; [причина]</code>\n"
            "<code>/mute &lt;время&gt; [причина]</code> (в ответ на сообщение)\n\n"
            "<b>Формат времени:</b>\n"
            "• <code>10m</code> - 10 минут\n"
            "• <code>2h</code> - 2 часа\n"
            "• <code>3d</code> - 3 дня",
            parse_mode=ParseMode.HTML
        )
        return

    # Удаляем команду и (если это ответ) триггерное сообщение пользователя
    try:
        await update.message.delete()
    except Exception as e:
        logger.warning(f"Error deleting mute command message: {e}")

    if update.message.reply_to_message:
        try:
            await update.message.reply_to_message.delete()
        except Exception as e:
            logger.warning(f"Error deleting replied message for /mute: {e}")

    if not prefilled_duration:
        # --- Duration Parsing ---
        duration = parse_duration(duration_str)
        if not duration:
            await update.message.reply_text(
                f"❌ Неверный формат времени: <code>{duration_str}</code>.\n"
                "Используйте, например: <code>10m</code>, <code>2h</code>, <code>3d</code>.",
                parse_mode=ParseMode.HTML
            )
            return

        if not reason:
            reason = "Нарушение правил чата."

    # --- Mute Logic ---
    # Re-implemented permission checks to ensure manual admin commands are not
    # affected by the MODERATE_BOTS auto-moderation setting.
    if target_user.id == context.bot.id:
        await update.message.reply_text("🤖 Я не могу заглушить сам себя.")
        return

    # Check against bot's own admin list (DB)
    target_is_bot_admin = await is_global_admin(target_user.id) or db.is_chat_admin(update.effective_chat.id, target_user.id)
    if target_is_bot_admin:
        await update.message.reply_text("⛔ Нельзя заглушить другого администратора бота.")
        return

    # Check against Telegram's admin list (API)
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, target_user.id)
        if member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            await update.message.reply_text("⛔ Нельзя заглушить администратора или владельца чата.")
            return
    except Exception as e:
        logger.warning(f"Could not check admin status for target user {target_user.id} via API: {e}")

    try:
        until_date = datetime.now() + duration
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target_user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date
        )
        
        # Log the mute action
        db.log_moderation_action(
            chat_id=update.effective_chat.id,
            user_id=target_user.id,
            action='mute',
            admin_id=update.effective_user.id,
            reason=reason,
            duration=duration
        )
        
        user_mention_html = target_user.mention_html()
        mute_text = (
            f"🔇 Пользователь {user_mention_html} был(а) заглушен(а) до {until_date.strftime('%Y-%m-%d %H:%M:%S')}.\n"
            f"<b>Причина:</b> {reason}"
        )
        await update.message.reply_text(mute_text, parse_mode=ParseMode.HTML)
        add_bot_message_to_cache(update.effective_chat.id, mute_text)
    except Exception as e:
        logger.error(f"Error muting user {target_user.id}: {e}")
        await update.message.reply_text(f"⚠️ Не удалось заглушить пользователя: {e}")

async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Размутить пользователя, вернув права на отправку сообщений."""
    if not update.effective_chat or not update.message or not update.effective_user:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    # --- Argument and User Resolution ---
    target_user = await resolve_target_user(update, context)

    if not target_user:
        await update.message.reply_text(
            "<b>Использование:</b>\n"
            "<code>/unmute &lt;@user/id&gt;</code>\n"
            "<code>/unmute</code> (в ответ на сообщение)",
            parse_mode=ParseMode.HTML
        )
        return

    # Удаляем команду и (если это ответ) триггерное сообщение пользователя
    try:
        await update.message.delete()
    except Exception as e:
        logger.warning(f"Error deleting unmute command message: {e}")

    if update.message.reply_to_message:
        try:
            await update.message.reply_to_message.delete()
        except Exception as e:
            logger.warning(f"Error deleting replied message for /unmute: {e}")

    # --- Unmute Logic ---
    try:
        # Restore default permissions for a member by setting all to True, except for admin-like ones
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id, user_id=target_user.id, permissions=PERMS_UNRESTRICTED
        )
        
        user_mention_html = target_user.mention_html()
        unmute_text = (
            f"🔊 Ограничения сняты с пользователя {user_mention_html}.",
        )
        await update.message.reply_text(unmute_text, parse_mode=ParseMode.HTML)
        add_bot_message_to_cache(update.effective_chat.id, unmute_text)
    except Exception as e:
        logger.error(f"Error unmuting user {target_user.id}: {e}")
        await update.message.reply_text(f"⚠️ Не удалось снять ограничения с пользователя: {e}")

async def auto_rule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the callback for automated rule suggestions."""
    query = update.callback_query
    await query.answer()

    callback_data = query.data
    request_id = callback_data.split('_')[-1]

    proposal = context.bot_data.get('ban_proposals', {}).get(request_id)
    if not proposal or proposal.get('admin_id') != query.from_user.id:
        await query.edit_message_text("❌ Этот запрос не для вас или он истек.")
        return

    chat_id = proposal['chat_id']
    admin_id = proposal['admin_id']

    # --- Handle different actions ---
    if callback_data.startswith("auto_rule_skip_"):
        await query.edit_message_text("✅ Действие пропущено.")
    elif callback_data.startswith("auto_rule_add_word_"):
        text_to_ban = proposal.get('text')
        if text_to_ban and db.add_ban_word(chat_id, normalize_text(text_to_ban)):
            await query.edit_message_text(f"✅ Сообщение добавлено в запрещенные слова для этого чата.")
        else:
            await query.edit_message_text("❌ Не удалось добавить слово. Возможно, оно уже в списке.")

    elif callback_data.startswith("auto_rule_add_name_first_"):
        text_to_ban = proposal.get('first_name')
        is_global = "_global_" in callback_data
        target_chat_id = 0 if is_global else chat_id
        scope_text = "глобально" if is_global else "для этого чата"
        if text_to_ban and db.add_ban_nickname_word(target_chat_id, normalize_text(text_to_ban), admin_id):
            await query.edit_message_text(f"✅ Имя '{text_to_ban}' добавлено в запрещенные для ников ({scope_text}).")
        else:
            await query.edit_message_text("❌ Не удалось добавить имя. Возможно, оно уже в списке.")

    elif callback_data.startswith("auto_rule_add_name_last_"):
        text_to_ban = proposal.get('last_name')
        is_global = "_global_" in callback_data
        target_chat_id = 0 if is_global else chat_id
        scope_text = "глобально" if is_global else "для этого чата"
        if text_to_ban and db.add_ban_nickname_word(target_chat_id, normalize_text(text_to_ban), admin_id):
            await query.edit_message_text(f"✅ Фамилия '{text_to_ban}' добавлена в запрещенные для ников ({scope_text}).")
        else:
            await query.edit_message_text("❌ Не удалось добавить фамилию. Возможно, она уже в списке.")

    elif callback_data.startswith("auto_rule_add_bio_"):
        text_to_ban = proposal.get('bio')
        is_global = "_global_" in callback_data
        target_chat_id = 0 if is_global else chat_id
        scope_text = "глобально" if is_global else "для этого чата"
        if text_to_ban and db.add_ban_bio_word(target_chat_id, normalize_text(text_to_ban), admin_id):
            await query.edit_message_text(f"✅ Описание профиля добавлено в запрещенные ({scope_text}).")
        else:
            await query.edit_message_text("❌ Не удалось добавить описание. Возможно, оно уже в списке.")

    else:
        await query.edit_message_text("❌ Неизвестное действие.")

    # Clean up the proposal from bot_data
    context.bot_data.get('ban_proposals', {}).pop(request_id, None)

async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Issue a warning to a user."""
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    target_user = await resolve_target_user(update, context)
    if not target_user:
        await update.message.reply_text("❌ Использование: /warn <@user/id> [причина] или ответьте на сообщение.")
        return

    # Удаляем команду и (если это ответ) триггерное сообщение пользователя
    try:
        await update.message.delete()
    except Exception as e:
        logger.warning(f"Error deleting warn command message: {e}")

    if update.message.reply_to_message:
        try:
            await update.message.reply_to_message.delete()
        except Exception as e:
            logger.warning(f"Error deleting replied message for /warn: {e}")

    # Determine reason
    reason_args = []
    if update.message.reply_to_message:
        reason_args = context.args or []
    elif context.args and len(context.args) > 1:
        reason_args = context.args[1:]
    reason = ' '.join(reason_args) if reason_args else "Нарушение правил чата"

    # Warn the user
    if db.warn_user(
        user_id=target_user.id,
        chat_id=update.effective_chat.id,
        warned_by=update.effective_user.id,
        reason=reason
    ):
        warn_text = (
            f"⚠️ Пользователь {target_user.mention_html()} был(а) предупрежден(а).\n"
            f"<b>Причина:</b> {reason}"
        )
        sent = await update.message.reply_text(warn_text, parse_mode=ParseMode.HTML)
        add_bot_message_to_cache(update.effective_chat.id, warn_text)
        schedule_message_deletion(context.job_queue, sent.chat.id, sent.message_id)
    else:
        info_text = f"ℹ️ У пользователя {target_user.mention_html()} уже есть предупреждение."
        sent = await update.message.reply_text(info_text, parse_mode=ParseMode.HTML)
        add_bot_message_to_cache(update.effective_chat.id, info_text)
        schedule_message_deletion(context.job_queue, sent.chat.id, sent.message_id)

async def unwarn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
    
    target_user = await resolve_target_user(update, context)
    if not target_user:
        sent = await update.message.reply_text("❌ Использование: /unwarn <@username/user_id> или ответьте на сообщение с /unwarn")
        schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
        schedule_message_deletion(context.job_queue, sent.chat.id, sent.message_id)
        return
    
    # Удаляем команду и (если это ответ) триггерное сообщение пользователя
    try:
        await update.message.delete()
    except Exception as e:
        logger.warning(f"Error deleting warn command message: {e}")

    if update.message.reply_to_message:
        try:
            await update.message.reply_to_message.delete()
        except Exception as e:
            logger.warning(f"Error deleting replied message for /warn: {e}")
    
    # Unwarn the user
    if db.unwarn_user(user_id=target_user.id, chat_id=update.effective_chat.id):
        user_mention = target_user.mention_markdown()
        sent = await update.message.reply_text(
            f"✅ Предупреждение снято: {user_mention}.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        sent = await update.message.reply_text("❌ Предупреждение не найдено.")
        
    # Clean up command and response
    schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
    schedule_message_deletion(context.job_queue, sent.chat.id, sent.message_id)

# Ban word commands
async def list_ban_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all banned words for the current chat."""
    if not update.effective_chat:
        return
        
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
        
    chat_id = update.effective_chat.id
    words = db.get_chat_ban_words(chat_id)
    
    if not words:
        sent_message = await update.message.reply_text("📭 В этом чате нет запрещённых слов.")
    else:
        word_list = "🚫 *Запрещённые слова в этом чате:*\n\n" + "\n".join(f"• `{w}`" for w in words)
        sent_message = await update.message.reply_text(word_list, parse_mode=ParseMode.MARKDOWN)
    
    # Schedule both the command and the response for deletion
    schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
    if 'sent_message' in locals():
        schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)

async def add_ban_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    if not context.args:
        sent_message = await update.message.reply_text(
            "❌ Использование: /add_ban_word <слова через запятую>\n\n"
            "Пример: `/add_ban_word слово1,слово2,слово 3` - добавит несколько слов",
            parse_mode=ParseMode.MARKDOWN
        )
        schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
        schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)
        return
    
    words_input = ' '.join(context.args)
    words = [word.strip() for word in words_input.split(',') if word.strip()]
    
    if not words:
        sent_message = await update.message.reply_text("❌ Не указаны слова для добавления.")
    else:
        chat_id = update.effective_chat.id
        added = []
        exists = []
        
        for word in words:
            # Normalize the word before adding it to the database
            if db.add_ban_word(chat_id, normalize_text(word)):
                added.append(word)
            else:
                exists.append(word)
        
        response = []
        if added:
            response.append(f"✅ Добавлены слова: {', '.join(f'`{w}`' for w in added)}")
        if exists:
            response.append(f"ℹ️ Уже были в списке: {', '.join(f'`{w}`' for w in exists)}")
        
        sent_message = await update.message.reply_text("\n".join(response), parse_mode=ParseMode.MARKDOWN)
    
    # Schedule deletion of both command and response
    schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
    schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)

async def del_ban_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
        
    if not context.args:
        sent_message = await update.message.reply_text(
            "❌ Использование: /del_ban_word <слово>\n"
            "Чтобы увидеть список, используйте /list_ban_words"
        )
        schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
        schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)
        return
        
    try:
        if not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        word_raw = ' '.join(context.args)
        word_to_delete = normalize_text(word_raw)
        if db.remove_ban_word(chat_id, word_to_delete):
            await update.message.reply_text(
                f"✅ Слово удалено из списка запрещённых этого чата: `{word_raw}`",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text("❌ Слово не найдено в списке запрещённых этого чата.")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Неверный формат. Используйте: /del_ban_word <слово>")

# Nickname ban commands
async def list_ban_nicknames(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all banned nickname words for the current chat"""
    if not update.effective_chat:
        return
        
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
        
    chat_id = update.effective_chat.id
    words = db.get_ban_nickname_words(chat_id)
    if not words:
        sent_message = await update.message.reply_text("ℹ️ В этом чате нет запрещенных слов в никах.")
    else:
        words_list = '\n'.join([f'• `{word}`' for word in sorted(words)])
        sent_message = await update.message.reply_text(
            f"📋 Список запрещенных слов в никах (всего {len(words)}):\n\n{words_list}",
            parse_mode=ParseMode.MARKDOWN
        )
    
    # Schedule deletion of both command and response
    schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
    schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)

async def add_ban_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not context.args:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        sent_message = await update.message.reply_text(
            "❌ Использование: /add_ban_nickname <слова через запятую>\n\n"
            "Пример: `/add_ban_nickname admin,модератор,бот` - добавит несколько слов",
            parse_mode=ParseMode.MARKDOWN
        )
        schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
        schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)
        return
        
    chat_id = update.effective_chat.id
    admin_id = update.effective_user.id
    
    # Split input by commas and strip whitespace
    words_input = ' '.join(context.args)
    words = [word.strip() for word in words_input.split(',') if word.strip()]
    
    if not words:
        sent_message = await update.message.reply_text("❌ Не указаны слова для добавления.")
    else:
        added = []
        exists = []
        
        for word in words:
            # Normalize the word before adding it to the database
            if db.add_ban_nickname_word(chat_id, normalize_text(word), admin_id):
                added.append(word)
            else:
                exists.append(word)
        
        response = []
        if added:
            response.append(f"✅ Добавлены слова для ников: {', '.join(f'`{w}`' for w in added)}")
        if exists:
            response.append(f"ℹ️ Уже были в списке: {', '.join(f'`{w}`' for w in exists)}")
        
        sent_message = await update.message.reply_text("\n".join(response), parse_mode=ParseMode.MARKDOWN)
    
    # Schedule deletion of both command and response
    schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
    schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)

async def del_ban_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat:
        return
        
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
        
    chat_id = update.effective_chat.id
    admin_id = update.effective_user.id
    
    if not context.args:
        words = db.get_ban_nickname_words(chat_id)
        if not words:
            sent_message = await update.message.reply_text("ℹ️ В этом чате нет запрещенных слов в никах.")
        else:
            words_text = "\n".join(f"{i+1}. `{w}`" for i, w in enumerate(words))
            sent_message = await update.message.reply_text(
                f"📋 Список запрещенных слов в никах (всего {len(words)}):\n\n{words_text}\n\n"
                "Для удаления используйте: /del_ban_nickname <слово>",
                parse_mode=ParseMode.MARKDOWN
            )
        
        # Schedule deletion of both command and response
        schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
        schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)
        return
        
    try:
        word_raw = ' '.join(context.args)
        word_to_delete = normalize_text(word_raw)
        if db.remove_ban_nickname_word(chat_id, word_to_delete, admin_id):
            sent_message = await update.message.reply_text(
                f"✅ Слово `{word_raw}` удалено из списка запрещенных ников в этом чате.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            sent_message = await update.message.reply_text(
                "❌ Слово не найдено в списке запрещенных ников этого чата."
            )
        
        # Schedule deletion of both command and response
        schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
        schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)
        
    except Exception as e:
        logger.error(f"Error removing ban nickname word: {e}")
        sent_message = await update.message.reply_text(
            "❌ Произошла ошибка при удалении слова. Пожалуйста, попробуйте снова."
        )
        # Schedule deletion of error message
        schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
        schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)


async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle support command from users."""
    logger.info(f"Support command received from {update.effective_user.id}")
    
    # Get message text
    message_text = ' '.join(context.args) if context.args else None
    if not message_text and update.message and update.message.text:
        # Try to get text after command
        parts = update.message.text.split(' ', 1)
        message_text = parts[1] if len(parts) > 1 else None
    
    if update.effective_chat.type != "private":
        try:
            bot_username = (await context.bot.get_me()).username
            logger.info(f"Command used in group chat, redirecting to @{bot_username}")
            await update.message.reply_text(
                f"ℹ️ Пожалуйста, напишите мне это в личные сообщения @{bot_username}."
            )
        except Exception as e:
            logger.error(f"Error in group chat redirect: {e}")
        return
    
    logger.info(f"Processing support request in private chat")
    user = update.effective_user
    user_info = f"ID: {user.id}"
    
    # Get message text from command arguments or from message text
    message_text = ' '.join(context.args) if context.args else None
    if not message_text and update.message.text:
        # Try to extract message after command
        message_text = update.message.text.split(' ', 1)[1] if ' ' in update.message.text else None
    
    if message_text:
        logger.info(f"Forwarding message to admins: {message_text}")
        success = False
        for admin_id in admin_chat_ids:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"✉️ Новое сообщение от пользователя {user_info}:\n\n{message_text}"
                )
                success = True
                logger.info(f"Message forwarded to admin {admin_id}")
            except Exception as e:
                logger.error(f"Error sending message to admin {admin_id}: {e}")
        
        if success:
            await update.message.reply_text("✅ Ваше сообщение отправлено администратору.")
        else:
            await update.message.reply_text("❌ Не удалось отправить сообщение администраторам.")
    else:
        logger.info("No message text provided")
        await update.message.reply_text(
            "Пожалуйста, укажите текст сообщения после команды /связь\n"
            "Например: /связь Мне нужна помощь с..."
        )

async def reply_to_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Replies to a message from a linked channel with the chat's rules.
    """
    if not update.message or not update.effective_chat:
        return

    # This handles automatic posts from a linked channel.
    is_linked_channel_post = (
        update.message.sender_chat and update.message.sender_chat.type == ChatType.CHANNEL
    )

    if is_linked_channel_post:
        chat_id = update.effective_chat.id
        rules = db.get_chat_rules(chat_id)

        if rules:
            try:
                await update.message.reply_text(
                    text=rules,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
            except Exception as e:
                logger.error(f"Failed to reply with rules in chat {chat_id}: {e}")

async def reload_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверить известных участников из БД на запрещённые никнеймы.
    Обходит ограничение Bot API, используя кэш известных участников (`known_members`).
    Теперь проверяет только тех, кто не был проверен ранее.
    """
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
    
    if not update.effective_chat:
        return
    
    chat_id = update.effective_chat.id
    message = None
    
    try:
        # Проверим права бота на бан (не обязательно, но полезно для понятного сообщения об ошибке)
        try:
            me = await context.bot.get_me()
            bot_member = await context.bot.get_chat_member(chat_id, me.id)
            if getattr(bot_member, 'can_restrict_members', False) is False and bot_member.status not in ['administrator', 'creator']:
                await update.message.reply_text(
                    "❌ У бота нет прав на ограничение участников. Выдайте права администратора."
                )
                return
        except Exception as e:
            logger.warning(f"Can't verify bot permissions: {e}")
        
        # Получаем НЕПРОВЕРЕННЫХ известных активных участников из БД
        known = db.get_unchecked_known_members(chat_id)
        total_members = len(known)
        if total_members == 0:
            await update.message.reply_text(
                "ℹ️ В базе нет новых непроверенных участников для этого чата. "
                "Проверка запускается автоматически для новых пользователей и по расписанию."
            )
            return
        
        message = await update.message.reply_text(
            f"🔄 Начинаю проверку {total_members} непроверенных участников...\n"
            "<i>Примечание: проверка описаний профиля (bio) требует доп. запросов к API и может быть медленной.</i>",
            parse_mode=ParseMode.HTML
        )
        
        checked = 0
        banned = 0
        
        for m in known:
            user_id = m['user_id']
            username = m.get('username')
            first_name = m.get('first_name')
            last_name = m.get('last_name')
            
            # Пропускаем админов (проверяем через API, чтобы учесть и локальных)
            try:
                member = await context.bot.get_chat_member(chat_id, user_id)
                if member.status in [ChatMember.ADMINISTRATOR, ChatMember.CREATOR]:
                    checked += 1
                    db.mark_user_profile_checked(chat_id, user_id) # Mark admin as checked to not see them again
                    continue
            except Exception:
                pass # Если проверка не удалась, продолжаем. Бан все равно не сработает, если он админ.

            banned_now = False
            # Check bio first
            if await check_user_bio(chat_id, user_id, context):
                banned += 1
                banned_now = True
            
            # If not banned for bio, check nickname
            if not banned_now:
                fields = [username, first_name, last_name]
                for val in filter(None, fields):
                    if await check_username(chat_id, user_id, val, context):
                        banned += 1
                        banned_now = True
                        break # Stop checking names for this user
            
            checked += 1
            # Mark user as checked so we don't check them again
            db.mark_user_profile_checked(chat_id, user_id)
            
            # Прогресс раз в 10 итераций или на последнем
            if checked % 10 == 0 or checked == total_members:
                try:
                    await message.edit_text(
                        f"🔍 Проверено {checked}/{total_members}. Заблокировано: {banned}"
                    )
                    await asyncio.sleep(0.2)
                except Exception as e:
                    logger.debug(f"Progress update failed: {e}")
        
        # Финальный итог
        if message:
            await message.edit_text(
                f"✅ Проверка завершена!\n"
                f"• Всего проверено: {checked}\n"
                f"• Заблокировано: {banned}"
            )
        else:
            await update.message.reply_text(
                f"✅ Проверка завершена! Проверено: {checked}. Заблокировано: {banned}."
            )
    except Exception as e:
        logger.error(f"Error in reload_members (DB-based): {e}", exc_info=True)
        if message:
            await message.edit_text(f"❌ Ошибка при проверке: {e}")
        else:
            await update.message.reply_text(f"❌ Ошибка при проверке: {e}")
    
    # Удаление сообщений позже (если в проекте это принятая практика)
    try:
        if message:
            schedule_message_deletion(context.job_queue, message.chat.id, message.message_id)
        schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
    except Exception as e:
        logger.error(f"Error scheduling message deletion: {e}")

async def scheduled_name_check(context: ContextTypes.DEFAULT_TYPE):
    """Periodically check profiles of users who haven't been checked before."""
    logger.info("Running scheduled name check job...")
    try:
        # Get all chats where the bot has known members
        chat_ids = db.get_all_known_chat_ids()
        if not chat_ids:
            logger.info("Scheduled name check: No known chats to check.")
            return

        for chat_id in chat_ids:
            # Check bot permissions in this chat before proceeding
            try:
                me = await context.bot.get_me()
                bot_member = await context.bot.get_chat_member(chat_id, me.id)
                if getattr(bot_member, 'can_restrict_members', False) is False and bot_member.status not in ['administrator', 'creator']:
                    logger.warning(f"Scheduled check: Skipping chat {chat_id} due to missing 'Restrict members' permission.")
                    continue
            except Exception as e:
                # Если бот не может получить информацию о себе в чате (например, "Chat not found"),
                # значит, он больше не является его участником.
                if "not found" in str(e).lower():
                    logger.info(f"Scheduled check: Bot is no longer in chat {chat_id}. Marking chat as inactive.")
                    # Помечаем чат как неактивный, чтобы не проверять его в будущем.
                    db.set_chat_active_status(chat_id, is_active=False)
                else:
                    logger.warning(f"Scheduled check: Could not verify bot permissions in chat {chat_id}, skipping. Error: {e}")
                continue

            # Получаем непроверенных участников только для активных чатов
            unchecked_members = db.get_unchecked_known_members(chat_id, only_active_chat=True)
            if not unchecked_members:
                continue

            logger.info(f"Found {len(unchecked_members)} unchecked members in chat {chat_id}.")
            
            banned_count = 0
            for member_data in unchecked_members:
                user_id = member_data['user_id']
                
                # Skip global admins, but mark them as checked
                if user_id in ADMIN_IDS:
                    db.mark_user_profile_checked(chat_id, user_id)
                    continue

                banned_now = False
                # Check bio first
                if await check_user_bio(chat_id, user_id, context):
                    banned_now = True
                    banned_count += 1
                
                # If not banned for bio, check nickname
                if not banned_now:
                    fields = [member_data.get('username'), member_data.get('first_name'), member_data.get('last_name')]
                    for val in filter(None, fields):
                        if await check_username(chat_id, user_id, val, context):
                            banned_count += 1
                            break 
                
                db.mark_user_profile_checked(chat_id, user_id)
                await asyncio.sleep(0.1) # small delay to avoid hitting limits

            if banned_count > 0:
                logger.info(f"Scheduled name check in chat {chat_id} finished. Banned {banned_count} users.")

    except Exception as e:
        logger.error(f"Error in scheduled_name_check job: {e}", exc_info=True)

async def list_ban_bios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all banned bio words for the current chat."""
    if not update.effective_chat:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    chat_id = update.effective_chat.id
    words = db.get_ban_bio_words(chat_id)
    if not words:
        sent_message = await update.message.reply_text("ℹ️ В этом чате нет запрещенных слов в описаниях профиля.")
    else:
        words_list = '\n'.join([f'• `{word}`' for word in sorted(words)])
        sent_message = await update.message.reply_text(
            f"📋 Список запрещенных слов в описаниях (всего {len(words)}):\n\n{words_list}",
            parse_mode=ParseMode.MARKDOWN
        )

    schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
    schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)

async def add_ban_bio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add banned words for user bios in the current chat."""
    if not update.effective_chat or not context.args:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    chat_id = update.effective_chat.id
    admin_id = update.effective_user.id

    words_input = ' '.join(context.args)
    words = [word.strip() for word in words_input.split(',') if word.strip()]

    if not words:
        sent_message = await update.message.reply_text("❌ Не указаны слова для добавления.")
    else:
        added = []
        exists = []

        for word in words:
            if db.add_ban_bio_word(chat_id, normalize_text(word), admin_id):
                added.append(word)
            else:
                exists.append(word)

        response = []
        if added:
            response.append(f"✅ Добавлены слова для описаний: {', '.join(f'`{w}`' for w in added)}")
        if exists:
            response.append(f"ℹ️ Уже были в списке: {', '.join(f'`{w}`' for w in exists)}")

        sent_message = await update.message.reply_text("\n".join(response), parse_mode=ParseMode.MARKDOWN)

    schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
    schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)

async def del_ban_bio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete banned words for user bios in the current chat."""
    if not update.effective_chat:
        return

    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return

    chat_id = update.effective_chat.id
    admin_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text("Использование: /del_ban_bio <слово>")
        return

    try:
        word_raw = ' '.join(context.args)
        word_to_delete = normalize_text(word_raw)
        if db.remove_ban_bio_word(chat_id, word_to_delete, admin_id):
            sent_message = await update.message.reply_text(
                f"✅ Слово `{word_raw}` удалено из списка запрещенных в описаниях.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            sent_message = await update.message.reply_text(
                "❌ Слово не найдено в списке запрещенных в описаниях."
            )

        schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
        schedule_message_deletion(context.job_queue, sent_message.chat.id, sent_message.message_id)
    except Exception as e:
        logger.error(f"Error removing ban bio word: {e}")
        await update.message.reply_text("❌ Произошла ошибка при удалении слова.")

async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    """Sends a daily summary of moderation actions to admins."""
    logger.info("Running daily moderation report job...")
    
    if not ADMIN_IDS:
        logger.warning("Daily report job ran, but no ADMIN_IDS are configured.")
        return

    stats = db.get_daily_moderation_stats()
    bans = stats.get('bans', 0)
    mutes = stats.get('mutes', 0)

    # Only send a report if there's something to report
    if bans == 0 and mutes == 0:
        logger.info("No moderation actions in the last 24 hours. Skipping daily report.")
        return
    
    report_text = (
        f"📊 **Ежедневный отчет по модерации за 24 часа**\n\n"
        f"🚫 Забанено пользователей: `{bans}`\n"
        f"🔇 Выдано мутов: `{mutes}`"
    )
    
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=report_text,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to send daily report to admin {admin_id}: {e}")

async def link_moderation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the callback for link moderation (ban or unmute)."""
    query = update.callback_query
    await query.answer()

    admin_user = query.from_user

    if not await is_global_admin(admin_user.id):
        await query.edit_message_text("⛔ У вас нет прав для выполнения этой команды.")
        return

    try:
        # Используем rsplit, чтобы корректно обработать отрицательные chat_id
        # 'link_mod_ban_-100_123' -> ['link_mod_ban', '-100', '123']
        parts = query.data.rsplit('_', 2)
        action = parts[0].replace('link_mod_', '') # 'ban' или 'unmute'
        chat_id_str, user_id_str = parts[1], parts[2]
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)
    except (ValueError, IndexError) as e:
        logger.error(f"Error parsing link_moderation_callback data: {query.data}, error: {e}")
        await query.edit_message_text("❌ Ошибка в данных. Не удалось выполнить действие.")
        return

    try:
        # Получаем информацию о пользователе и чате для уведомлений
        user_to_moderate = await context.bot.get_chat(user_id)
        chat = await context.bot.get_chat(chat_id)
        user_mention = user_to_moderate.mention_html()
    except Exception as e:
        logger.error(f"Could not get info for user {user_id} or chat {chat_id}: {e}")
        await query.edit_message_text(f"❌ Не удалось получить информацию о пользователе/чате.")
        return

    original_message_text = query.message.text_html

    if action == "ban":
        try:
            # Сначала удаляем кешированные сообщения, затем баним
            await delete_cached_messages(context, chat_id, user_id)
            # revoke_messages=True удалит сообщения за последние 24 часа
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id, revoke_messages=True)
            await delete_cached_messages(context, chat_id, user_id)
            
            # Обновляем сообщение у админа
            await query.edit_message_text(
                original_message_text + f"\n\n<b>✅ РЕШЕНИЕ: Пользователь {user_mention} забанен.</b> (Администратор: {admin_user.mention_html()})",
                parse_mode=ParseMode.HTML, reply_markup=None
            )
            # Отправляем уведомление в чат
            await context.bot.send_message(chat_id, f"🚫 Пользователь {user_mention} был забанен администратором за отправку ссылки.", parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Failed to ban user {user_id} from link moderation: {e}")
            await query.edit_message_text(original_message_text + f"\n\n❌ Не удалось забанить пользователя. Ошибка: {e}", reply_markup=None)

    elif action == "unmute":
        try:
            # Снимаем ограничения
            await context.bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=PERMS_UNRESTRICTED)
            # Добавляем пользователя в белый список
            db.add_whitelist_user(chat_id, user_id, admin_user.id)
            
            # Обновляем сообщение у админа
            await query.edit_message_text(
                original_message_text + f"\n\n<b>✅ РЕШЕНИЕ: Пользователю {user_mention} возвращены права и он добавлен в белый список.</b> (Администратор: {admin_user.mention_html()})",
                parse_mode=ParseMode.HTML, reply_markup=None
            )
            # Отправляем уведомление в чат
            await context.bot.send_message(
                chat_id, f"✅ Пользователю {user_mention} возвращены права после проверки администратором. Он добавлен в белый список и больше не будет проверяться.", parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Failed to unmute user {user_id} from link moderation: {e}")
            await query.edit_message_text(original_message_text + f"\n\n❌ Не удалось вернуть права и добавить в белый список. Ошибка: {e}", reply_markup=None)


def _cleanup_job_wrapper(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Wrapper function for the job queue to call cleanup_old_backups.
    The context argument is required by the job queue but not used here.
    """
    cleanup_old_backups()

# Bannable domains management
async def add_ban_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
    if not context.args:
        await update.message.reply_text("Использование: /add_ban_domain <домен>")
        return
    
    domain = context.args[0].lower().strip()
    chat_id = update.effective_chat.id
    admin_id = update.effective_user.id

    if db.add_bannable_domain(chat_id, domain, admin_id):
        await update.message.reply_text(f"✅ Домен `{domain}` добавлен в список авто-бана для этого чата.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"ℹ️ Домен `{domain}` уже в списке.", parse_mode=ParseMode.MARKDOWN)

async def del_ban_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
    if not context.args:
        await update.message.reply_text("Использование: /del_ban_domain <домен>")
        return
    
    domain = context.args[0].lower().strip()
    chat_id = update.effective_chat.id

    if db.remove_bannable_domain(chat_id, domain):
        await update.message.reply_text(f"✅ Домен `{domain}` удален из списка авто-бана.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"ℹ️ Домен `{domain}` не найден в списке.", parse_mode=ParseMode.MARKDOWN)

async def list_ban_domains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text(MESSAGES['not_admin'])
        return
    
    chat_id = update.effective_chat.id
    domains = db.get_bannable_domains(chat_id)

    if not domains:
        await update.message.reply_text("ℹ️ Список запрещенных доменов для этого чата пуст.")
    else:
        domain_list = "\n".join(f"• `{d}`" for d in domains)
        await update.message.reply_text(f"🚫 Запрещенные домены в этом чате:\n{domain_list}", parse_mode=ParseMode.MARKDOWN)

# Register all admin handlers
def register_admin_handlers(application: Application):
    """Register all admin command handlers."""
    # Admin help command
    application.add_handler(CommandHandler("start", help_command))
    application.add_handler(CommandHandler("help", help_command))

    application.add_handler(CommandHandler("admin", admin_help))
    
    # General commands (available to all)
    application.add_handler(CommandHandler("profile", show_profile))

    # Chat settings command with auto-delete
    async def wrapped_chat_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await chat_settings(update, context)
        schedule_message_deletion(context.job_queue, update.effective_chat.id, update.message.message_id)
    
    application.add_handler(CommandHandler("settings", wrapped_chat_settings))
    
    # Trigger management commands
    application.add_handler(CommandHandler("add_trigger", add_trigger))
    application.add_handler(CommandHandler("del_trigger", del_trigger))
    application.add_handler(CommandHandler("list_triggers", list_triggers))
    
    # Ban patterns commands
    application.add_handler(CommandHandler("add_ban_pattern", add_ban_pattern))
    application.add_handler(CommandHandler("del_ban_pattern", del_ban_pattern))
    application.add_handler(CommandHandler("list_ban_patterns", list_ban_patterns))

    # Avatar ban commands
    application.add_handler(CommandHandler("unban_avatar", unban_avatar))
    application.add_handler(CommandHandler("list_banned_avatars", list_banned_avatars))
    application.add_handler(MessageHandler(
        filters.PHOTO & filters.ChatType.PRIVATE,
        handle_banned_avatar_photo
    ))
    application.add_handler(CallbackQueryHandler(unban_avatar_callback, pattern=r'^unban_avatar_(confirm_.+|cancel)$'))
    application.add_handler(CallbackQueryHandler(global_ban_callback, pattern=r'^global_ban_(confirm_.+|reject)$'))
    application.add_handler(CallbackQueryHandler(auto_rule_callback, pattern=r'^auto_rule_'))
    
    # Link moderation callback handler
    application.add_handler(CallbackQueryHandler(link_moderation_callback, pattern=r'^link_mod_'))

    # Chat Admins Management
    application.add_handler(CommandHandler("add_chat_admin", add_chat_admin))
    application.add_handler(CommandHandler("del_chat_admin", del_chat_admin))
    application.add_handler(CommandHandler("list_chat_admins", list_chat_admins))

    # Rules management
    application.add_handler(CommandHandler("rules", show_rules))
    application.add_handler(CommandHandler("set_rules", set_rules))
    application.add_handler(CommandHandler("del_rules", del_rules))
    application.add_handler(CommandHandler("set_rules_ad", set_rules_ad))
    application.add_handler(CommandHandler("del_rules_ad", del_rules_ad))
    
    # Welcome message management
    application.add_handler(CommandHandler("set_welcome", set_welcome))
    application.add_handler(CommandHandler("del_welcome", del_welcome))
    application.add_handler(CommandHandler("welcome", show_welcome))
    application.add_handler(CommandHandler("set_welcome_ad", set_welcome_ad))
    application.add_handler(CommandHandler("del_welcome_ad", del_welcome_ad))    
    application.add_handler(CommandHandler("enable_captcha", enable_captcha))
    application.add_handler(CommandHandler("disable_captcha", disable_captcha))
    # Link ban commands
    application.add_handler(CommandHandler("enable_linkban", enable_linkban))
    application.add_handler(CommandHandler("disable_linkban", disable_linkban))

    # Whitelist commands
    application.add_handler(CommandHandler("add_whitelist", add_whitelist))
    application.add_handler(CommandHandler("del_whitelist", del_whitelist))
    application.add_handler(CommandHandler("list_whitelist", list_whitelist))

    # Maintenance commands
    application.add_handler(CommandHandler("backup", backup_database))
    application.add_handler(MessageHandler(
        filters.Document.ALL & filters.ChatType.PRIVATE,
        restore_database
    ))
    application.add_handler(CallbackQueryHandler(restore_database_callback, pattern=r'^restore_(confirm|cancel)_\d+$'))
    
    # User management commands
    application.add_handler(CommandHandler("ban", ban_user))
    application.add_handler(CommandHandler("unban", unban_user))
    application.add_handler(CommandHandler("mute", mute_user))
    application.add_handler(CommandHandler("unmute", unmute_user))
    application.add_handler(CommandHandler("warn", warn_user))
    application.add_handler(CommandHandler("unwarn", unwarn_user))
    
    # Ban word commands
    application.add_handler(CommandHandler("add_ban_word", add_ban_word))
    application.add_handler(CommandHandler("del_ban_word", del_ban_word))
    application.add_handler(CommandHandler("list_ban_words", list_ban_words))
    
    # Nickname ban commands
    application.add_handler(CommandHandler("add_ban_nickname", add_ban_nickname))
    application.add_handler(CommandHandler("del_ban_nickname", del_ban_nickname))
    application.add_handler(CommandHandler("list_ban_nicknames", list_ban_nicknames))  # Reuse function to show list
    
    # Ban bio commands
    application.add_handler(CommandHandler("add_ban_bio", add_ban_bio))
    application.add_handler(CommandHandler("del_ban_bio", del_ban_bio))
    application.add_handler(CommandHandler("list_ban_bios", list_ban_bios))

    # Bannable domains management
    application.add_handler(CommandHandler("add_ban_domain", add_ban_domain))
    application.add_handler(CommandHandler("del_ban_domain", del_ban_domain))
    application.add_handler(CommandHandler("list_ban_domains", list_ban_domains))

    # Add other admin commands here
    application.add_handler(CommandHandler("namecheck", reload_members))  # Check all members' usernames
    
    # Support command with Latin alias
    # Для кириллической команды /связь используем MessageHandler с Regex
    application.add_handler(MessageHandler(
        filters.Regex(r'^/связь(@\w+)?(\s|$)') & filters.COMMAND,
        support_command
    ))
    application.add_handler(CommandHandler("helpme", support_command))
    
    # Other user management commands
    application.add_handler(CommandHandler("ask", ask_user))

    # Russian alias for /unmute
    application.add_handler(MessageHandler(
        filters.Regex(r'^/говори(@\w+)?(\s|$)') & filters.COMMAND,
        unmute_user
    ))

    # Handler for channel posts
    # This handler replies with rules to posts from a linked channel.
    application.add_handler(MessageHandler(
        sender_chat_filter & filters.ChatType.GROUPS & ~filters.COMMAND,
        reply_to_channel_post
    ))

    # Schedule daily backup
    # Запускается раз в день в 03:00 по UTC. Вы можете изменить время.
    # Например, для 8:00 утра по Москве (UTC+3) используйте time(hour=5)
    application.job_queue.run_daily(
        scheduled_backup,
        time=time(hour=3, minute=0, second=0)
    )
    logger.info("Scheduled daily backup job.")

    # Schedule daily backup cleanup
    # Запускается раз в день в 04:00 по UTC.
    application.job_queue.run_daily(
        _cleanup_job_wrapper,
        time=time(hour=4, minute=0, second=0),
        name="daily_backup_cleanup"
    )
    logger.info("Scheduled daily backup cleanup job.")

    # Schedule daily moderation report
    # Запускается раз в день в 08:00 по UTC.
    application.job_queue.run_daily(
        send_daily_report,
        time=time(hour=8, minute=0, second=0),
        name="daily_moderation_report"
    )
    logger.info("Scheduled daily moderation report job.")

    # Schedule automatic name check every 2 minutes
    application.job_queue.run_repeating(
        scheduled_name_check,
        interval=timedelta(minutes=2),
        first=timedelta(seconds=10), # Start 10 seconds after launch
        name="scheduled_name_check"
    )
    logger.info("Scheduled automatic name check job to run every 2 minutes.")
