import time
import logging
import re
import asyncio
from datetime import datetime, timedelta
from urllib.parse import urlparse
from handlers.helpers import add_user_message_id, delete_cached_messages, resolve_target_user
from telegram import Update, Message, MessageEntity, ChatPermissions, User, ChatMember, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, MessageHandler, filters, ApplicationHandlerStop
from telegram.constants import ParseMode, ChatType
from utils.database import db
from utils.text_utils import normalize_text, is_zalgo_text
from utils.helpers import schedule_message_deletion, is_admin, add_bot_message_to_cache, bot_message_cache
from utils.notifications import propose_global_ban
from handlers.permissions import PERMS_FULL_RESTRICT
from config import (
    MESSAGES, MESSAGE_LIMIT, TIME_WINDOW,
    MAX_WARNINGS, MUTE_DURATION_MINUTES, CAPS_THRESHOLD,
    MAX_IDENTICAL_MESSAGES_BEFORE_WARN, ZALGO_MIN_DIACRITICS, ZALGO_RATIO_THRESHOLD, MODERATE_ADMINS,
    MODERATE_BOTS
)
import re
import asyncio
from typing import Optional, Dict, Any

# Configure logger
logger = logging.getLogger(__name__)

# In-memory tracker for user warnings and spam detection
user_moderation_tracker: Dict[tuple, Dict] = {}
MAX_HISTORY_USERS = 1000  # Limit the number of users in history to prevent memory exhaustion

# Track banned words checks
BANNED_WORDS_CACHE = {}
BANNED_WORDS_LAST_UPDATE = 0
BANNED_WORDS_UPDATE_INTERVAL = 300  # 5 minutes in seconds

# Message deletion settings
DELETE_AFTER_SECONDS = 5  # Default time after which to delete messages
SPAM_WINDOW_SECONDS = 60 # Time window for spam check

async def _handle_zalgo_violation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    chat_id: int,
    is_edited: bool,
):
    """Handles a Zalgo text violation: warns on first offense, bans on second."""
    key = (chat_id, user.id)
    
    # Ensure tracker exists and initialize zalgo_warnings if needed
    if key not in user_moderation_tracker:
        user_moderation_tracker[key] = {'warnings': 0, 'last_messages': [], 'zalgo_warnings': 0, 'mimic_warnings': 0}
    elif 'zalgo_warnings' not in user_moderation_tracker[key]:
        user_moderation_tracker[key]['zalgo_warnings'] = 0

    user_moderation_tracker[key]['zalgo_warnings'] += 1

    if user_moderation_tracker[key]['zalgo_warnings'] > 1:
        # Second offense: Ban
        action = "редактирование на" if is_edited else "использование"
        reason = f"повторное {action} Zalgo-текста"
        logger.info(f"Banning user {user.id} for '{reason}' in chat {chat_id}.")
        
        # Сначала удаляем сообщения из кеша, затем баним
        try:
            await delete_cached_messages(context, chat_id, user.id)
        except Exception as e:
            logger.error(f"Error deleting cached messages for user {user.id} before Zalgo ban: {e}")

        try:
            # revoke_messages=True удалит все сообщения за последние 24 часа
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id, revoke_messages=True)
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"🚫 {user_mention} был(а) автоматически забанен(а) в этом чате за повторное использование искаженного (Zalgo) текста.",
                parse_mode=ParseMode.HTML
            )
            add_bot_message_to_cache(chat_id, sent_msg.text)
            schedule_message_deletion(context.job_queue, chat_id, sent_msg.message_id, delay=15)
            await propose_global_ban(
                context, user_to_ban=user, chat_where_banned=update.effective_chat, reason=reason
            )
        except Exception as e:
            logger.error(f"Failed to auto-ban user {user.id} for Zalgo text: {e}")
    else:
        # First offense: Warn
        user_mention = user.mention_html()
        warn_message = (
            f"⚠️ {user_mention}, пожалуйста, не используйте чрезмерное количество "
            f"диакритических знаков (Zalgo-текст). Ваше сообщение было удалено. "
            f"Повторное нарушение приведет к бану."
        )
        sent_msg = await context.bot.send_message(chat_id=chat_id, text=warn_message, parse_mode=ParseMode.HTML)
        add_bot_message_to_cache(chat_id, sent_msg.text)
        schedule_message_deletion(context.job_queue, chat_id, sent_msg.message_id, delay=15)

async def _issue_warning_and_mute_if_needed(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    reason: str
) -> bool:
    """
    Increments a user's warning count. Mutes the user if they exceed MAX_WARNINGS.
    Returns True if an action (warn/mute) was taken.
    """
    chat_id = update.effective_chat.id
    key = (chat_id, user_id)

    # Initialize tracker if not present
    if key not in user_moderation_tracker:
        user_moderation_tracker[key] = {'warnings': 0, 'last_messages': [], 'zalgo_warnings': 0, 'mimic_warnings': 0}

    data = user_moderation_tracker[key]
    data['warnings'] += 1

    logger.info(
        f"WarningIssued chat={chat_id} user={user_id} reason='{reason}' "
        f"warnings_total={data['warnings']}"
    )

    if data['warnings'] >= MAX_WARNINGS:
        # Mute user
        mute_duration = timedelta(minutes=MUTE_DURATION_MINUTES)
        until_date = datetime.now() + mute_duration

        try:
            # Check bot permissions
            me = await context.bot.get_me()
            bot_member = await context.bot.get_chat_member(chat_id, me.id)
            if not getattr(bot_member, 'can_restrict_members', False):
                logger.warning(f"Cannot mute user {user_id} in chat {chat_id}: Missing 'Restrict members' permission.")
                return True

            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date
            )

            # Reset warnings after mute
            data['warnings'] = 0

            user_mention = update.effective_user.mention_html()
            mute_message = (
                f"🔇 Пользователь {user_mention} получил(а) мут на {MUTE_DURATION_MINUTES} минут "
                f"за многочисленные нарушения."
            )
            sent_msg = await context.bot.send_message(chat_id=chat_id, text=mute_message, parse_mode=ParseMode.HTML)
            add_bot_message_to_cache(chat_id, sent_msg.text)
            logger.info(f"Muted user {user_id} in chat {chat_id} for {MUTE_DURATION_MINUTES} minutes.")

            # Log to DB
            db.log_moderation_action(
                chat_id=chat_id,
                user_id=user_id,
                action='mute',
                admin_id=context.bot.id,
                reason=f"Exceeded warning limit ({MAX_WARNINGS})",
                duration=mute_duration
            )

        except Exception as e:
            logger.error(f"Failed to mute user {user_id} in chat {chat_id}: {e}")

        return True  # Mute action was taken
    else:
        # Just a warning, no mute yet.
        user_mention = update.effective_user.mention_html()
        warn_message = (
            f"⚠️ {user_mention}, вы получили предупреждение за: {reason}. "
            f"У вас {data['warnings']} из {MAX_WARNINGS} предупреждений."
        )
        sent_msg = await context.bot.send_message(chat_id=chat_id, text=warn_message, parse_mode=ParseMode.HTML)
        add_bot_message_to_cache(chat_id, sent_msg.text)
        schedule_message_deletion(context.job_queue, chat_id, sent_msg.message_id, delay=15)

        # Log to DB
        db.log_moderation_action(
            chat_id=chat_id,
            user_id=user_id,
            action='warn',
            admin_id=context.bot.id,
            reason=reason
        )
        return True  # Warning action was taken

async def _ban_for_word(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    chat_id: int,
    word: str,
    is_edited: bool,
):
    """A helper to ban a user for a forbidden word, log it, and notify."""
    reason_text = f"использование запрещенного слова: `{word}`"
    if is_edited:
        reason_text = f"редактирование сообщения на спам (слово: `{word}`)"

    logger.info(f"Locally banning user {user.id} for '{reason_text}' in chat {chat_id}.")

    try:
        # 1. Ban with revoke_messages - как в команде /ban
        await context.bot.ban_chat_member(
            chat_id=chat_id,
            user_id=user.id,
            revoke_messages=True  # Удалит все сообщения пользователя
        )
        logger.info(f"Banned user {user.id} with revoke_messages=True")
        
        # 2. Дополнительно удаляем кешированные сообщения (фолбэк)
        await delete_cached_messages(context, chat_id, user.id)
        
        # 3. Получаем информацию о пользователе для уведомления
        try:
            member = await context.bot.get_chat_member(chat_id, user.id)
            user_obj = member.user
            user_mention = user_obj.mention_html()
        except Exception:
            user_obj = user
            user_mention = f'<a href="tg://user?id={user.id}">пользователь</a>'

        # 4. Send notification to the chat
        notification_text = f"🚫 {user_mention} был(а) автоматически забанен(а) в этом чате. Причина: {reason_text}."
        sent_msg = await context.bot.send_message(
            chat_id=chat_id, text=notification_text, parse_mode=ParseMode.HTML,
        )
        add_bot_message_to_cache(chat_id, sent_msg.text)
        schedule_message_deletion(context.job_queue, chat_id, sent_msg.message_id, delay=15)

        # 5. Propose global ban to admins
        if user_obj:
            chat = await context.bot.get_chat(chat_id)
            await propose_global_ban(
                context=context,
                user_to_ban=user_obj,
                chat_where_banned=chat,
                reason=reason_text
            )
            
        # 6. Логируем в БД
        db.ban_user(
            user_id=user.id,
            reason=reason_text,
            admin_id=context.bot.id,  # Авто-модерация
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )

    except Exception as e:
        logger.error(f"Failed to auto-ban user {user.id} for banned word '{word}': {e}", exc_info=True)
        # Fallback: try to delete just the trigger message
        try:
            if is_edited and update.edited_message:
                await update.edited_message.delete()
            elif update.message:
                await update.message.delete()
            logger.info(f"Deleted trigger message for user {user.id} as a fallback after ban failure.")
        except Exception as del_e:
            logger.error(f"Also failed to delete the trigger message as a fallback: {del_e}")

async def _handle_mimicking_violation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    chat_id: int,
):
    """Handles a user mimicking bot messages."""
    key = (chat_id, user.id)
    user_mention = user.mention_html()

    # Ensure tracker exists and initialize mimic_warnings if needed
    if key not in user_moderation_tracker:
        user_moderation_tracker[key] = {'warnings': 0, 'last_messages': [], 'zalgo_warnings': 0, 'mimic_warnings': 0}
    elif 'mimic_warnings' not in user_moderation_tracker[key]:
        user_moderation_tracker[key]['mimic_warnings'] = 0

    user_moderation_tracker[key]['mimic_warnings'] += 1

    if user_moderation_tracker[key]['mimic_warnings'] > 1:
        # Second offense: Mute for 30 minutes
        reason = "повторение сообщений бота"
        logger.info(f"Muting user {user.id} for '{reason}' in chat {chat_id}.")
        try:
            mute_duration = timedelta(minutes=MUTE_DURATION_MINUTES)
            until_date = datetime.now() + mute_duration
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date
            )
            
            mute_message = (
                f"🔇 Пользователь {user_mention} получил(а) мут на {MUTE_DURATION_MINUTES} минут "
                f"за повторение сообщений бота."
            )
            sent_msg = await context.bot.send_message(chat_id=chat_id, text=mute_message, parse_mode=ParseMode.HTML)
            add_bot_message_to_cache(chat_id, sent_msg.text)
            schedule_message_deletion(context.job_queue, chat_id, sent_msg.message_id, delay=15)
            
            # Reset warnings after mute
            user_moderation_tracker[key]['mimic_warnings'] = 0

        except Exception as e:
            logger.error(f"Failed to auto-mute user {user.id} for mimicking: {e}")
    else:
        # First offense: Warn
        user_mention = user.mention_html()
        warn_message = (
            f"⚠️ {user_mention}, пожалуйста, не повторяйте сообщения бота. "
            f"Ваше сообщение было удалено. "
            f"Повторное нарушение приведет к муту."
        )
        sent_msg = await context.bot.send_message(chat_id=chat_id, text=warn_message, parse_mode=ParseMode.HTML)
        add_bot_message_to_cache(chat_id, sent_msg.text)
        schedule_message_deletion(context.job_queue, chat_id, sent_msg.message_id, delay=15)

async def _check_bot_mimicking(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, message_text: str) -> bool:
    """Checks if a user is repeating a recent bot message."""
    if not message_text or not (recent_bot_messages := bot_message_cache.get(update.effective_chat.id)):
        return False

    if normalize_text(message_text) in recent_bot_messages:
        await update.message.delete()
        await _handle_mimicking_violation(update, context, update.effective_user, update.effective_chat.id)
        return True

    return False

async def check_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle non-command messages from regular users to check for spam, links, and banned words."""
    if not update.message or not update.effective_chat or not update.effective_user:
        return

    # Add message to cache for potential deletion on ban
    add_user_message_id(update.effective_chat.id, update.effective_user.id, update.message.message_id)

    # If we are not moderating admins, check if the user is a chat admin via API.
    if not MODERATE_ADMINS:
        try:
            # This is a more reliable check than the old `is_admin` as it queries the API
            member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
            if member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                logger.debug(f"Ignoring message from chat admin {update.effective_user.id} in chat {update.effective_chat.id} based on MODERATE_ADMINS setting.")
                return
        except Exception as e:
            # If the check fails, we might proceed, but it's safer to log and potentially stop.
            # For now, just log the warning. The subsequent moderation action will likely fail anyway.
            logger.warning(f"Could not check admin status for user {update.effective_user.id}: {e}")

    # --- Bot moderation check ---
    if update.effective_user.is_bot and not MODERATE_BOTS:
        logger.debug(f"Ignoring message from bot {update.effective_user.id} in chat {update.effective_chat.id} based on MODERATE_BOTS setting.")
        return

    # --- NEW: Check if the message is a comment on a channel post ---
    if (
        update.message.reply_to_message
        and update.message.reply_to_message.sender_chat
        and update.message.reply_to_message.sender_chat.type == ChatType.CHANNEL
    ):
        # This is a comment on a channel post, ignore it for moderation.
        pass

    user = update.effective_user
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Get message text early to use in all relevant checks
    message_text = update.message.text or update.message.caption or ""

    # --- -1. Global ban check ---
    if db.is_banned(user_id):
        logger.info(f"Globally banned user {user_id} detected in chat {chat_id}. Re-banning.")
        # Сначала удаляем сообщения из кеша, затем баним
        try:
            await delete_cached_messages(context, chat_id, user.id)
        except Exception as e:
            logger.error(f"Error deleting cached messages for globally banned user {user.id}: {e}")

        try:
            # revoke_messages=True удалит все сообщения за последние 24 часа
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id, revoke_messages=True)
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"🚫 {user_mention} находится в глобальном черном списке и был(а) удален(а) из чата.",
                parse_mode=ParseMode.HTML
            )
            add_bot_message_to_cache(chat_id, sent_msg.text)
            schedule_message_deletion(context.job_queue, chat_id, sent_msg.message_id, delay=15)
        except Exception as e:
            logger.error(f"Failed to re-ban globally banned user {user_id}: {e}")
        raise ApplicationHandlerStop

    # --- 0. Whitelist check ---
    if db.is_whitelisted(chat_id, user_id):
        return

    # --- NEW: Bot Mimicking Check ---
    # This check should be early to prevent trolls from triggering other warnings with bot's own text.
    handled = await _check_bot_mimicking(update, context, user_id, message_text)
    if handled:
        return

    # --- NEW: Forwarded message from public channel/group check ---
    if update.message.forward_from_chat and update.message.forward_from_chat.type in [ChatType.CHANNEL, ChatType.SUPERGROUP]:
        reason = "реклама (пересылка из другого паблика)"
        logger.info(f"Locally banning user {user.id} for '{reason}' in chat {chat_id}.")
        user_mention = user.mention_html() # Определяем здесь для использования в уведомлении.
        # Сначала удаляем сообщения из кеша, затем баним
        try:
            await delete_cached_messages(context, chat_id, user.id)
        except Exception as e:
            logger.error(f"Error deleting cached messages for user {user.id} before forward ban: {e}")

        try:
            # revoke_messages=True удалит все сообщения за последние 24 часа, включая это
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id, revoke_messages=True)
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"🚫 {user_mention} был(а) автоматически забанен(а) в этом чате за рекламу (пересылка из другого паблика).",
                parse_mode=ParseMode.HTML
            )
            add_bot_message_to_cache(chat_id, sent_msg.text)
            schedule_message_deletion(context.job_queue, chat_id, sent_msg.message_id, delay=15)
            await propose_global_ban(
                context, user_to_ban=user, chat_where_banned=update.effective_chat, reason=reason
            )
        except Exception as e:
            logger.error(f"Failed to auto-ban user {user.id} for forwarding from a public chat: {e}")
        return  # Action taken, stop processing

    # --- 1. Spam check ---
    handled = await _check_spam(update, context, user_id, message_text)
    if handled:
        return

    # --- 2. Anti-caps check ---
    handled = await _check_caps(update, context, user_id, message_text)
    if handled:
        return

    # --- 3. Zalgo text check ---
    if is_zalgo_text(
        message_text,
        min_diacritics=ZALGO_MIN_DIACRITICS,
        ratio_threshold=ZALGO_RATIO_THRESHOLD
    ):
        try:
            await update.message.delete()
        except Exception as e:
            logger.warning(f"Failed to delete Zalgo message from user {user_id}: {e}")

        await _handle_zalgo_violation(
            update, context, user, chat_id, is_edited=False
        )
        # Stop processing, as an action (warn/ban) was taken and message deleted.
        return

    # --- 4. Link check ---
    entities = update.message.entities or update.message.caption_entities or []
    has_link_entity = any(e.type in [MessageEntity.URL, MessageEntity.TEXT_LINK] for e in entities)

    # Также проверяем текст на наличие ссылок, которые Telegram мог не распознать как сущности.
    has_link_in_text = False
    if not has_link_entity and message_text:
        normalized_text = message_text.lower()
        # Упростим проверку для начала
        link_patterns = [
            r'https?://',
            r't\.me/',
            r'telegram\.me/',
            r'\b[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b'
        ]
        
        for pattern in link_patterns:
            if re.search(pattern, normalized_text):
                has_link_in_text = True
                logger.debug(f"Found link pattern '{pattern}' in text")
                break

    logger.debug(f"Link check - has_link_entity: {has_link_entity}, has_link_in_text: {has_link_in_text}")

    # Check if link banning is enabled for this chat
    if (has_link_entity or has_link_in_text) and db.is_link_deletion_enabled(chat_id):
        # --- Новая логика: БАН вместо ограничения ---
        logger.info(f"User {user.id} sent a link in chat {chat_id} with linkban enabled. Banning.")
        
        # Бан с revoke_messages (как в команде /ban)
        try:
            await context.bot.ban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                revoke_messages=True  # Удалит все сообщения пользователя
            )
            logger.info(f"Banned user {user.id} for sending link")
            
            # Дополнительно удаляем кешированные сообщения
            await delete_cached_messages(context, chat_id, user_id)
            
            # Отправляем уведомление
            user_mention = user.mention_html()
            notification_text = f"🚫 {user_mention} был(а) автоматически забанен(а) за отправку ссылки."
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=notification_text,
                parse_mode=ParseMode.HTML
            )
            schedule_message_deletion(context.job_queue, chat_id, sent_msg.message_id, delay=15)
            
            # Предлагаем глобальный бан
            await propose_global_ban(
                context=context,
                user_to_ban=user,
                chat_where_banned=update.effective_chat,
                reason="отправка ссылки при включенном линкбане"
            )
            
            # Логируем в БД
            db.ban_user(
                user_id=user.id,
                reason="отправка ссылки при включенном линкбане",
                admin_id=context.bot.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name
            )
            
        except Exception as e:
            logger.error(f"Failed to ban user {user.id} for sending link: {e}")
            # Фолбэк: пытаемся хотя бы удалить сообщение
            try:
                await update.message.delete()
                logger.info(f"Deleted link message from user {user.id} as fallback")
            except Exception as del_e:
                logger.error(f"Failed to delete link message: {del_e}")

        raise ApplicationHandlerStop # Останавливаем обработку сообщения

    # --- 5. Banned words check (final check) ---
    if not message_text:
        return

    banned_words = db.get_chat_ban_words(chat_id)
    if not banned_words:
        return
    
    normalized_message_text = normalize_text(message_text)
    for word in banned_words:
        # Banned words in DB are already normalized
        if word in normalized_message_text:
            # Delete the message with the banned word BEFORE the ban
            try:
                await update.message.delete()
                logger.info(f"Deleted message with banned word '{word}' from user {user.id}")
            except Exception as e:
                logger.warning(f"Failed to delete message with banned word from user {user.id}: {e}")

            await _ban_for_word(update, context, user, chat_id, word, is_edited=bool(update.edited_message))
            # Stop processing after the first violation is handled
            raise ApplicationHandlerStop


async def _check_spam(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, message_text: str) -> bool:
    """Check for duplicate messages in a time window and issue a warning if needed."""
    if not message_text:
        return False

    chat_id = update.effective_chat.id
    now = time.time()
    key = (chat_id, user_id)

    # Limit the size of the history to prevent memory exhaustion
    if len(user_moderation_tracker) > MAX_HISTORY_USERS and key not in user_moderation_tracker:
        # Simple strategy: remove the first (oldest) entry. A better one would be LRU.
        oldest_key = next(iter(user_moderation_tracker))
        del user_moderation_tracker[oldest_key]
    if key not in user_moderation_tracker:
        user_moderation_tracker[key] = {'warnings': 0, 'last_messages': [], 'zalgo_warnings': 0, 'mimic_warnings': 0}

    data = user_moderation_tracker[key]
    # Append current message
    data['last_messages'].append({'text': message_text, 'time': now})

    # Keep only messages within the spam window
    window_start = now - SPAM_WINDOW_SECONDS
    data['last_messages'] = [m for m in data['last_messages'] if m['time'] >= window_start]

    # Count identical messages within the window
    identical_count = sum(1 for m in data['last_messages'] if m['text'] == message_text)

    if identical_count >= MAX_IDENTICAL_MESSAGES_BEFORE_WARN:
        # Reset identical messages to avoid repeated warns on same burst
        data['last_messages'] = [m for m in data['last_messages'] if m['text'] != message_text]

        # Delete the offending message
        try:
            await update.message.delete()
        except Exception as e:
            logger.warning(f"Failed to delete spam message from user {user_id}: {e}")

        return await _issue_warning_and_mute_if_needed(
            update, context, user_id, reason="спам/флуд"
        )

    return False
    
async def _check_caps(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, message_text: str) -> bool:
    """Checks for excessive capitalization in a message and issues a warning if needed."""
    if not message_text or len(message_text) < CAPS_THRESHOLD:
        return False

    # Count uppercase Cyrillic and Latin letters
    uppercase_letters = re.findall(r'[A-ZА-ЯЁ]', message_text)
    if len(uppercase_letters) >= CAPS_THRESHOLD:
        try:
            await update.message.delete()
        except Exception as e:
            logger.warning(f"Failed to delete message with caps from user {user_id}: {e}")

        return await _issue_warning_and_mute_if_needed(
            update, context, user_id, reason="использование верхнего регистра (CAPS)"
        )

    return False

async def handle_triggers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Checks for and responds to triggers in messages."""
    # Don't respond to edited messages or messages without text
    if not update.message or not update.message.text or update.edited_message:
        return

    chat_id = update.effective_chat.id
    message_text = normalize_text(update.message.text)

    # Check for a trigger response from the database
    # The DB function is designed to find a trigger word within the message text
    response = db.get_trigger_response(chat_id, message_text) # message_text is already normalized

    if response:
        try:
            # Using reply_text to make it clear what message triggered the bot
            await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
            logger.info(f"Responded to trigger in chat {chat_id}")
            # Stop other handlers from processing this message to prevent conflicts
            raise ApplicationHandlerStop
        except Exception as e:
            logger.error(f"Error sending trigger response in chat {chat_id}: {e}")

async def handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Checks edited messages for forbidden words and bans the user if found.
    """
    if not update.edited_message or not update.edited_message.text:
        return

    # This check is for group chats only
    if update.edited_message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return

    # Check if the message is a comment on a channel post
    if (
        update.edited_message.reply_to_message
        and update.edited_message.reply_to_message.sender_chat
        and update.edited_message.reply_to_message.sender_chat.type == ChatType.CHANNEL
    ):
        # This is a comment on a channel post, ignore it for moderation.
        pass

    # Don't check admins (if configured)
    if not MODERATE_ADMINS:
        try:
            member = await context.bot.get_chat_member(update.edited_message.chat_id, update.edited_message.from_user.id)
            if member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                return # Silently ignore edits from admins
        except Exception:
            pass # If check fails, proceed, ban will likely fail if they are admin
    if db.is_whitelisted(update.edited_message.chat_id, update.edited_message.from_user.id):
        return

    chat_id = update.edited_message.chat_id
    user = update.edited_message.from_user
    text = update.edited_message.text

    # --- Zalgo check for edited messages ---
    if is_zalgo_text(
        text,
        min_diacritics=ZALGO_MIN_DIACRITICS,
        ratio_threshold=ZALGO_RATIO_THRESHOLD
    ):
        try:
            await update.edited_message.delete()
        except Exception as e:
            logger.warning(f"Failed to delete edited Zalgo message from user {user.id}: {e}")

        await _handle_zalgo_violation(
            update, context, user, chat_id, is_edited=True
        )
        raise ApplicationHandlerStop

    # Check against banned words for this chat
    banned_words = db.get_chat_ban_words(chat_id)
    if not banned_words:
        return

    normalized_text = normalize_text(text)

    for word in banned_words:
        # Banned words in DB are already normalized
        if word in normalized_text:
            await _ban_for_word(update, context, user, chat_id, word, is_edited=True)
            # Stop processing after the first violation is handled
            raise ApplicationHandlerStop

async def handle_karma(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles karma increase from user replies."""
    if (not update.message or not update.message.reply_to_message or
            not update.message.text or not update.effective_chat):
        return

    # Check if the message is a simple karma-giving word
    if normalize_text(update.message.text) not in ('+', 'спасибо', 'дякую', 'thanks'):
        return

    giver = update.effective_user
    receiver = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id

    # --- Prevent abuse ---
    if not giver or not receiver: return
    if giver.id == receiver.id: return # Can't give karma to yourself
    if receiver.id == context.bot.id: return # Can't give karma to the bot

    # Prevent giving karma to the same message multiple times
    # We use a simple cache in context.chat_data
    karma_cache = context.chat_data.setdefault('karma_given', {})
    message_id = update.message.reply_to_message.message_id
    if karma_cache.get(message_id, set()) and giver.id in karma_cache.get(message_id, set()):
        # User already gave karma for this message, silently ignore
        return

    # Add karma point
    new_karma = db.change_karma(chat_id, receiver.id, 1)

    # Update cache
    if message_id not in karma_cache:
        karma_cache[message_id] = set()
    karma_cache[message_id].add(giver.id)

    # Notify (optional, can be removed if too noisy)
    receiver_mention = receiver.mention_html()
    karma_msg = await update.message.reply_text(f"👍 {receiver_mention} получил(а) +1 к репутации. Теперь у него/неё {new_karma} очков.", parse_mode=ParseMode.HTML)
    schedule_message_deletion(context.job_queue, chat_id, karma_msg.message_id, 10)

def register_message_handlers(application):
    # Создаем фильтр для обычных пользователей (не админов)
    # Этот фильтр будет использоваться для основного обработчика модерации.
    class NonAdminFilter(filters.BaseFilter):
        async def filter(self, message: Message) -> bool:
            if not message.from_user or not message.chat:
                return False
            # Пропускаем, если модерация админов выключена и пользователь - админ
            if not MODERATE_ADMINS:
                member = await message.chat.get_member(message.from_user.id)
                return member.status not in [ChatMember.ADMINISTRATOR, ChatMember.CREATOR]
            return True

    application.add_handler(
        MessageHandler(
            NonAdminFilter() & ~filters.COMMAND & filters.ChatType.GROUPS,
            check_user_message,
        ),
        group=0,
    )

    # The trigger handler runs after moderation checks.
    # It should work for all users, including admins.
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_triggers,
        ),
        group=1,
    )

    # The edited message handler also checks for admin status internally.
    application.add_handler(MessageHandler(
        filters.UpdateType.EDITED_MESSAGE & filters.ChatType.GROUPS,
        handle_edited_message
    ), group=2)

    # Karma handler - runs after moderation but before triggers
    application.add_handler(
        MessageHandler(
            filters.REPLY & filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            handle_karma
        ),
        group=3
    )
