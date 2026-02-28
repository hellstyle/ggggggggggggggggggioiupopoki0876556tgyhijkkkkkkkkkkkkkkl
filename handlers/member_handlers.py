# Standard library
import logging
import re
import time
from datetime import timedelta
from io import BytesIO
from typing import Dict, Optional

# Third-party libraries
from telegram import (ChatFullInfo, ChatMember, ChatMemberUpdated,
                      InlineKeyboardButton, InlineKeyboardMarkup, Update)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (CallbackQueryHandler, ChatMemberHandler,
                          ContextTypes, MessageHandler, filters)

# Local application imports
from config import (ADMIN_IDS, AVATAR_HASH_THRESHOLD, MODERATE_ADMINS,
                    MODERATE_BOTS)
from handlers.helpers import delete_cached_messages, add_user_message_id
from handlers.permissions import (PERMS_FULL_RESTRICT, PERMS_MEDIA_RESTRICT,
                                  PERMS_UNRESTRICTED)
from utils.database import db
from utils.helpers import schedule_message_deletion
from utils.image_utils import calculate_phash, compare_phashes
from utils.notifications import propose_global_ban
from utils.text_utils import normalize_text

logger = logging.getLogger(__name__)

# Regex for link detection in bios.
# It looks for http/https, t.me/, or patterns like domain.tld
LINK_IN_BIO_PATTERN = re.compile(
    r'https?://|'  # http:// or https://
    r't\.me/|telegram\.me/|'  # Telegram links
    # domain.tld patterns. This is not exhaustive but covers many cases.
    r'\b[a-zA-Z0-9\.\-]+\.(com|org|net|info|biz|ru|su|рф|me|io|dev|app|xyz|gg|dog|ly|sh)\b'
)

# Cache to avoid checking the same user's avatar too frequently
avatar_check_cache: Dict[int, float] = {}
AVATAR_CHECK_COOLDOWN = 300  # 5 minutes

# Cache to avoid checking the same user's bio too frequently
bio_check_cache: Dict[int, float] = {}
BIO_CHECK_COOLDOWN = 300  # 5 minutes

# Cache to store phash results for a given file_unique_id to avoid re-downloads
user_avatar_phash_cache: Dict[str, str] = {}

# This function is used by other modules
async def check_user_avatar(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, update: Optional[Update] = None) -> bool:
    """
    Checks a user's profile photo against the banned list (by file_unique_id and
    perceptual hash) and bans them if it matches.
    Returns True if the user was banned, False otherwise.
    """
    now = time.time()
    # Check cache to avoid API spam
    last_check = avatar_check_cache.get(user_id)
    if last_check and (now - last_check) < AVATAR_CHECK_COOLDOWN:
        return False

    avatar_check_cache[user_id] = now  # Update timestamp

    try:
        profile_photos = await context.bot.get_user_profile_photos(user_id=user_id, limit=1)
        if not profile_photos or not profile_photos.photos:
            return False  # No avatar to check

        current_avatar_photo = profile_photos.photos[0][-1]
        current_avatar_id = current_avatar_photo.file_unique_id

        # 1. Check for exact match using file_unique_id (fast)
        if db.is_avatar_banned(current_avatar_id):
            logger.info(f"Banning user {user_id} in chat {chat_id} for banned avatar (exact match).")
            # Если мы находимся в контексте сообщения, удаляем его
            if update and update.message:
                try:
                    await update.message.delete()
                except Exception as e:
                    logger.warning(f"Failed to delete message for user {user_id} with banned avatar: {e}")
            return await _ban_for_profile_violation(context, chat_id, user_id, "запрещенная аватарка (точное совпадение)")

        # 2. Check our in-memory cache for the phash of this specific avatar file
        if current_avatar_id in user_avatar_phash_cache:
            current_phash = user_avatar_phash_cache[current_avatar_id]
            logger.debug(f"Found cached phash for avatar {current_avatar_id}: {current_phash}")
        else:
            # 3. If not cached, download and calculate the hash
            logger.debug(f"No cached phash for avatar {current_avatar_id}. Downloading...")
            photo_file = await current_avatar_photo.get_file()
            photo_bytes_io = BytesIO()
            await photo_file.download_to_memory(photo_bytes_io)
            photo_bytes = photo_bytes_io.getvalue()
            
            current_phash = await calculate_phash(photo_bytes)
            if current_phash:
                # Store the calculated hash in our cache to avoid future downloads for this file
                user_avatar_phash_cache[current_avatar_id] = current_phash

        if not current_phash:
            return False # Could not hash the image

        # Get all banned hashes from DB
        banned_hashes = db.get_all_banned_avatar_hashes()
        # Compare current hash with all banned hashes
        for banned_hash in banned_hashes:
            # The threshold can be adjusted. Lower is stricter. 5 is a reasonable default.
            if compare_phashes(current_phash, banned_hash, threshold=AVATAR_HASH_THRESHOLD):
                logger.info(
                    f"Banning user {user_id} in chat {chat_id} for banned avatar "
                    f"(similar hash match: current={current_phash}, banned={banned_hash})."
                )
                # Если мы находимся в контексте сообщения, удаляем его
                if update and update.message:
                    try:
                        await update.message.delete()
                    except Exception as e:
                        logger.warning(f"Failed to delete message for user {user_id} with banned avatar: {e}")
                return await _ban_for_profile_violation(context, chat_id, user_id, "запрещенная аватарка (схожее изображение)")

    except Exception as e:
        # This can fail if the user has privacy settings, etc.
        logger.warning(f"Could not check avatar for user {user_id}: {e}")

    return False

# This function is used by other modules
async def check_username(chat_id: int, user_id: int, username: str, context: ContextTypes.DEFAULT_TYPE, update: Optional[Update] = None) -> bool:
    """
    Check if username contains banned words and ban if needed.
    Performs case-insensitive and normalized check against banned nicknames.
    """
    if not username:
        return False
    
    # Get all banned words for the chat
    banned_words = db.get_ban_nickname_words(chat_id)
    if not banned_words:
        return False

    # Normalize the user's name/username for a robust check
    normalized_username = normalize_text(username)
    
    matched_banned_word = None
    for word in banned_words:
        # Banned words in DB are already normalized
        if word in normalized_username:
            matched_banned_word = word # Keep original for the message
            break

    if not matched_banned_word:
        return False
    
    # Если мы находимся в контексте сообщения, удаляем его
    if update and update.message:
        try:
            await update.message.delete()
        except Exception as e:
            logger.warning(f"Failed to delete message for user {user_id} with banned nickname: {e}")

    # Ban the user and notify
    return await _ban_for_profile_violation(context, chat_id, user_id, f"запрещенное слово в нике: <code>{matched_banned_word}</code>")

async def check_user_bio(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, update: Optional[Update] = None) -> bool:
    """
    Checks a user's bio against the banned list and bans them if it matches.
    Returns True if the user was banned, False otherwise.
    """
    now = time.time()
    # Check cache to avoid API spam
    last_check = bio_check_cache.get(user_id)
    if last_check and (now - last_check) < BIO_CHECK_COOLDOWN:
        return False

    bio_check_cache[user_id] = now  # Update timestamp

    try:
        # We need get_chat to fetch the bio. This returns ChatFullInfo for users.
        user_chat: ChatFullInfo = await context.bot.get_chat(user_id)
        bio = getattr(user_chat, 'bio', None)

        if not bio:
            return False

        # Check for links in bio only if link banning is enabled for this chat
        if db.is_link_deletion_enabled(chat_id):
            # Check for any links in bio using the regex pattern
            if LINK_IN_BIO_PATTERN.search(bio.lower()):
                # --- Проверка прав бота ---
                try:
                    me = await context.bot.get_me()
                    bot_member = await context.bot.get_chat_member(chat_id, me.id)
                    if not bot_member.can_restrict_members:
                        logger.warning(
                            f"Bot lacks 'Restrict Members' permission in chat {chat_id}. "
                            "Skipping bio link moderation."
                        )
                        return False # Прерываем, если у бота нет прав
                except Exception as e:
                    logger.error(f"Could not verify bot permissions in chat {chat_id}: {e}")
                    return False # Прерываем, если не можем проверить права

                logger.info(f"User {user_id} has a link in bio in chat {chat_id}. Restricting and sending for moderation.")

                # 0. Удаляем все сообщения пользователя (кешированные и триггерное)
                try:
                    # Сначала удаляем все "запомненные" ботом сообщения
                    await delete_cached_messages(context, chat_id, user_id)
                    logger.info(f"Deleted cached messages for user {user_id} due to link in bio.")
                    # Затем удаляем сообщение, которое вызвало проверку
                    if update and update.message:
                        await update.message.delete()
                        logger.info(f"Deleted trigger message for user {user_id} due to link in bio.")
                except Exception as e:
                    logger.warning(
                        f"Failed to delete messages for user {user_id} with link in bio: {e}"
                    )

                # 1. Полностью ограничиваем пользователя
                try:
                    await context.bot.restrict_chat_member(
                        chat_id=chat_id,
                        user_id=user_id,
                        permissions=PERMS_FULL_RESTRICT
                    )
                    logger.info(f"Fully restricted user {user_id} in chat {chat_id} for link in bio.")
                except Exception as e:
                    logger.error(f"Failed to restrict user {user_id} for link in bio: {e}")
                    return False # Если не удалось ограничить, выходим

                # 2. Отправляем уведомление админам
                if ADMIN_IDS:
                    user_mention = user_chat.mention_html()
                    chat = await context.bot.get_chat(chat_id)

                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton("🚫 Забанить", callback_data=f"link_mod_ban_{chat_id}_{user_id}"),
                        InlineKeyboardButton("✅ Вернуть права", callback_data=f"link_mod_unmute_{chat_id}_{user_id}")
                    ]])

                    moderation_text = (
                        f"<b>⚠️ Обнаружена ссылка в профиле. Требуется модерация.</b>\n"
                        f"<b>Чат:</b> {chat.title} (<code>{chat_id}</code>)\n"
                        f"<b>Пользователь:</b> {user_mention} (<code>{user_id}</code>)\n\n"
                        f"<b>Описание профиля:</b>\n"
                        f"<blockquote>{bio}</blockquote>\n\n"
                        f"Пользователь временно ограничен в правах. Выберите действие 👇"
                    )

                    for admin_id in ADMIN_IDS:
                        await context.bot.send_message(admin_id, moderation_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

                return True # Действие предпринято, выходим

        banned_bio_words = db.get_ban_bio_words(chat_id)
        if not banned_bio_words:
            return False

        normalized_bio = normalize_text(bio)
        for word in banned_bio_words:
            # Banned words in DB are already normalized
            if word in normalized_bio:
                logger.info(f"Banning user {user_id} in chat {chat_id} for banned word in bio: '{word}'.")
                # Если мы находимся в контексте сообщения (вызов из check_message_username), удаляем сообщение
                if update and update.message:
                    try:
                        await update.message.delete()
                    except Exception as e:
                        logger.warning(f"Failed to delete message for user {user_id} with banned bio word: {e}")
                return await _ban_for_profile_violation(context, chat_id, user_id, f"запрещенное слово в описании профиля: <code>{word}</code>")

    except Exception as e:
        logger.warning(f"Could not check bio for user {user_id}: {e}")

    return False

async def _ban_for_profile_violation(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, reason_text: str) -> bool:
    """Helper function to ban a user, send a notification, and propose a global ban."""
    try:
        # Get user object for notifications
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            user = member.user
            user_mention = user.mention_html()
        except Exception:
            user = None
            user_mention = f'<a href="tg://user?id={user_id}">пользователь</a>'

        # Сначала удаляем кешированные сообщения, затем баним
        await delete_cached_messages(context, chat_id, user_id)

        # Ban the user
        await context.bot.ban_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            revoke_messages=True
        )

        # Notify the chat
        sent_message = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🚫 {user_mention} был(а) автоматически забанен(а). Причина: {reason_text}.",
            parse_mode=ParseMode.HTML,
        )
        # Schedule deletion of the ban message
        schedule_message_deletion(context.job_queue, chat_id, sent_message.message_id, delay=15)
        logger.info(f"Banned user {user_id} in chat {chat_id} for profile violation: {reason_text}")

        # Propose global ban if we have the user object
        if user:
            chat = await context.bot.get_chat(chat_id)
            await propose_global_ban(
                context=context, user_to_ban=user, chat_where_banned=chat,
                reason=f"нарушение правил профиля ({reason_text})"
            )

        return True

    except Exception as e:
        logger.error(f"Error banning user {user_id} for profile violation: {e}")

    return False

async def check_username_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle event with new chat members via message; track them and check profile."""
    if not update.message or not update.message.new_chat_members:
        return
        
    chat_id = update.effective_chat.id
    
    for user in update.message.new_chat_members:
        db.upsert_member(chat_id, user, is_member=True) # Track all new members

        # --- Admin Check ---
        # Do not perform profile checks on admins.
        if not MODERATE_ADMINS:
            try:
                member = await context.bot.get_chat_member(chat_id, user.id)
                if member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                    continue # Skip checks for this admin
            except Exception:
                pass # Proceed, but ban will likely fail if they are an admin

        if await check_user_bio(chat_id, user.id, context, update):
            continue
        names_to_check = [user.username, getattr(user, 'first_name', None), getattr(user, 'last_name', None)]
        for val in filter(None, names_to_check):
            if await check_username(chat_id, user.id, val, context, update):
                break

async def check_message_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """On each message: track sender in DB and check their profile (avatar, bio, name)."""
    if not update.message or not update.effective_chat or not update.effective_user:
        return
        
    # --- NEW: Check if the message is a comment on a channel post ---
    if (
        update.message.reply_to_message
        and update.message.reply_to_message.sender_chat
        and update.message.reply_to_message.sender_chat.type == ChatType.CHANNEL
    ):
        # This is a comment on a channel post, ignore it for moderation checks.
        # We still track the user as active.
        db.upsert_member(update.effective_chat.id, update.effective_user, is_member=True)
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # If we are not moderating admins, check if the user is a chat admin via API.
    # This makes profile checks consistent with message content checks.
    if not MODERATE_ADMINS:
        try:
            member = await context.bot.get_chat_member(chat_id, user.id)
            if member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                raise ApplicationHandlerStop # Stop processing in this group, but allow other groups
        except Exception as e:
            # If check fails, proceed. Ban will likely fail if they are an admin.
            logger.warning(f"Could not check admin status for user {user.id} in profile check: {e}")

    if user.is_bot and not MODERATE_BOTS:
        logger.debug(f"Ignoring profile check for bot {user.id} in chat {chat_id} based on MODERATE_BOTS setting.")
        raise ApplicationHandlerStop
    
    # Track as active member on any message
    db.upsert_member(chat_id, user, is_member=True)

    # --- Profile Checks ---
    # Only perform these checks if the user is NOT whitelisted.
    # This function has group=-1, so we must not `return` for whitelisted users,
    # otherwise other handlers (like message content checks) will be skipped.
    if not db.is_whitelisted(chat_id, user.id):
        # Check avatar
        if await check_user_avatar(chat_id, user.id, context, update=update):
            raise ApplicationHandlerStop

        # Check bio
        if await check_user_bio(chat_id, user.id, context, update=update):
            raise ApplicationHandlerStop

        # Check username, first_name, and last_name
        usernames_to_check = [user.username, user.first_name, user.last_name]
        
        for username in filter(None, usernames_to_check):
            if await check_username(chat_id, user.id, username, context, update=update):
                raise ApplicationHandlerStop  # Exit after first match
            
    # Optional: check message text for banned words (disabled for now)
    # You can enable content checks here if required.

async def lift_media_restriction_job(context: ContextTypes.DEFAULT_TYPE):
    """Lifts the initial media restriction from a new user after a timeout."""
    job = context.job
    chat_id = job.chat_id
    user_id = job.user_id

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        
        # Only lift restrictions if they are still restricted with limited permissions
        if member.status == 'restricted' and not member.can_send_photos:
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=PERMS_UNRESTRICTED
            )
            logger.info(f"Automatically lifted media restrictions for user {user_id} in chat {chat_id}.")
    except Exception as e:
        # This can happen if the user has already left the chat.
        logger.warning(f"Could not lift media restriction for user {user_id} in chat {chat_id}: {e}")

async def kick_unverified_member(context: ContextTypes.DEFAULT_TYPE):
    """Kicks a user if they fail to solve the captcha in time."""
    job = context.job
    chat_id = job.chat_id
    user_id = job.user_id
    welcome_message_id = job.data.get('welcome_message_id')

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        
        # If user can send messages, they have been verified. Do nothing.
        if member.status != 'restricted' or member.can_send_messages:
            # If the job is still running, it means the welcome message might still be there. Clean it up.
            if welcome_message_id:
                try:
                    await context.bot.delete_message(chat_id, welcome_message_id)
                    logger.info(f"Cleaned up captcha message {welcome_message_id} for already-verified user {user_id}.")
                except Exception:
                    pass # Message might have been deleted already
            logger.info(f"User {user_id} already verified or left chat {chat_id}. Job cancelled.")
            return

        # If still restricted, kick the user.
        # A "kick" is a temporary ban. We ban and immediately unban.
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id, revoke_messages=True)
        await delete_cached_messages(context, chat_id, user_id)
        await context.bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
        
        logger.info(f"Kicked user {user_id} from chat {chat_id} for failing captcha.")

        # Delete the welcome message
        if welcome_message_id:
            try:
                await context.bot.delete_message(chat_id, welcome_message_id)
            except Exception as e:
                logger.warning(f"Could not delete captcha message {welcome_message_id} in chat {chat_id}: {e}")

    except Exception as e:
        # This can happen if the user has already left the chat.
        logger.warning(f"Could not kick unverified user {user_id} in chat {chat_id} (maybe they left?): {e}")

async def greet_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greets a new member and handles initial restrictions and captcha."""
    if not update.chat_member:
        return

    result = update.chat_member
    # Check if a new member joined (not just updated)
    if result.new_chat_member.status == 'member' and result.old_chat_member.status in ['left', 'kicked', 'restricted']:
        chat_id = result.chat.id
        user = result.new_chat_member.user

        # Track member as active
        db.upsert_member(chat_id, user, is_member=True)

        # --- Admin Check ---
        # Do not perform profile checks or restrict admins when they join.
        if not MODERATE_ADMINS:
            try:
                member = await context.bot.get_chat_member(chat_id, user.id)
                if member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                    return # Don't check or restrict admins
            except Exception:
                pass # Proceed, but ban will likely fail if they are an admin

        # --- Global ban check ---
        if db.is_banned(user.id):
            logger.info(f"Globally banned user {user.id} tried to join chat {chat_id}. Banning.")
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id, revoke_messages=True)
            await delete_cached_messages(context, chat_id, user.id)
            # No notification needed, as they are immediately removed.
            return

        # --- Pre-join checks ---
        # 1. Check avatar first
        if await check_user_avatar(chat_id, user.id, context, update):
            return  # User was banned, no need to greet

        # 2. Check bio
        if await check_user_bio(chat_id, user.id, context, update):
            return # User was banned, no need to greet

        # 3. Check nickname
        names_to_check = [user.username, getattr(user, 'first_name', None), getattr(user, 'last_name', None)]
        for val in filter(None, names_to_check):
            if await check_username(chat_id, user.id, val, context, update):
                return  # User was banned, no need to greet

        is_captcha_enabled = db.is_welcome_captcha_enabled(chat_id)

        # --- Restriction Logic ---
        if is_captcha_enabled:
            # Full restriction pending captcha
            try:
                await context.bot.restrict_chat_member(chat_id=chat_id, user_id=user.id, permissions=PERMS_FULL_RESTRICT)
                logger.info(f"Fully restricted new user {user.id} in chat {chat_id} pending captcha.")
            except Exception as e:
                logger.error(f"Failed to restrict new user {user.id} for captcha in chat {chat_id}: {e}")
                return
        else:
            # Media restriction for 30 minutes
            try:
                await context.bot.restrict_chat_member(chat_id=chat_id, user_id=user.id, permissions=PERMS_MEDIA_RESTRICT)
                logger.info(f"Media-restricted new user {user.id} in chat {chat_id} for 30 minutes.")
            except Exception as e:
                logger.error(f"Failed to apply media restriction for new user {user.id} in chat {chat_id}: {e}")
                return

        # --- Prepare Welcome/Captcha Message ---
        message_text = ""
        welcome_settings = db.get_welcome_message(chat_id)
        if welcome_settings and welcome_settings.get("text"):
            message_text = welcome_settings["text"]
            # Replace placeholders
            message_text = message_text.replace("{user_mention}", user.mention_html())
            message_text = message_text.replace("{chat_title}", result.chat.title or "")
            message_text = message_text.replace("{first_name}", user.first_name)

        # Append ad text if it exists
        welcome_ad_text = db.get_welcome_ad(chat_id)
        if welcome_ad_text:
            if message_text:
                message_text += f"\n\n{welcome_ad_text}"

        keyboard = None
        if is_captcha_enabled:
            # If there's a welcome message, append captcha text. Otherwise, use a default.
            if message_text:
                message_text += "\n\nЧтобы получить доступ к чату, пожалуйста, нажмите на кнопку ниже. Сообщение будет удалено через 10 минут."
            else:
                # Default message if no welcome message is set
                message_text = (
                    f"Добро пожаловать, {user.mention_html()}!\n\n"
                    "Чтобы получить доступ к чату, пожалуйста, подтвердите, что вы не бот. Сообщение будет удалено через 10 минут."
                )

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Я не бот", callback_data=f"verify_{user.id}")
            ]])
        elif message_text:
            # If no captcha, but there is a welcome message, add the restriction notice
            message_text += "\n\nℹ️ <b>В течение 30 минут вам ограничена отправка медиа, ссылок и стикеров.</b>"

        # --- Send Message and Schedule Jobs ---
        # Only send a message if there's text to send (either welcome or captcha)
        if message_text:
            try:
                sent_message = await context.bot.send_message(
                    chat_id=chat_id,
                    text=message_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=keyboard
                )

                if is_captcha_enabled:
                    # Schedule kick job for 10 minutes if captcha is not solved
                    job_name = f"kick-unverified-{chat_id}-{user.id}"
                    # Remove any old jobs for this user, just in case
                    existing_jobs = context.job_queue.get_jobs_by_name(job_name)
                    for job in existing_jobs:
                        job.schedule_removal()

                    context.job_queue.run_once(
                        kick_unverified_member,
                        when=timedelta(minutes=10),
                        chat_id=chat_id,
                        user_id=user.id,
                        data={'welcome_message_id': sent_message.message_id},
                        name=job_name
                    )
                    logger.info(f"Scheduled 10-minute kick job '{job_name}' for user {user.id}.")
                else:
                    # Schedule media restriction lift job for 30 minutes
                    job_name = f"lift-media-restriction-{chat_id}-{user.id}"
                    existing_jobs = context.job_queue.get_jobs_by_name(job_name)
                    for job in existing_jobs:
                        job.schedule_removal()

                    context.job_queue.run_once(
                        lift_media_restriction_job,
                        when=timedelta(minutes=30),
                        chat_id=chat_id,
                        user_id=user.id,
                        name=job_name
                    )
                    logger.info(f"Scheduled 30-minute media restriction lift job '{job_name}' for user {user.id}.")
            except Exception as e:
                logger.error(f"Failed to send welcome/captcha message in chat {chat_id}: {e}")

async def verify_member_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 'I am not a bot' button press."""
    query = update.callback_query
    
    callback_data = query.data
    _, user_to_verify_id_str = callback_data.split('_')
    user_to_verify_id = int(user_to_verify_id_str)
    
    clicker_id = query.from_user.id

    if clicker_id != user_to_verify_id:
        await query.answer(text="Это кнопка не для вас!", show_alert=True)
        return

    # User is verified, grant permissions
    chat_id = query.message.chat_id
    try:
        # Use the same permissions as unmute to restore full access
        await context.bot.restrict_chat_member(chat_id=chat_id, user_id=clicker_id, permissions=PERMS_UNRESTRICTED)
        logger.info(f"User {clicker_id} passed verification in chat {chat_id}.")

        # Remove the scheduled kick job
        job_name = f"kick-unverified-{chat_id}-{clicker_id}"
        jobs = context.job_queue.get_jobs_by_name(job_name)
        if jobs:
            for job in jobs:
                job.schedule_removal()
            logger.info(f"Removed scheduled kick job for user {clicker_id} in chat {chat_id}.")

        # Edit the original message to remove the button
        await query.edit_message_text(
            text=query.message.text_html + "\n\n<b>✅ Проверка пройдена. Добро пожаловать!</b>\nОграничение на отправку медиа, ссылок и стикеров снято.",
            reply_markup=None, parse_mode=ParseMode.HTML
        )
        await query.answer(text="Проверка пройдена!", show_alert=False)
    except Exception as e:
        logger.error(f"Error verifying member {clicker_id} in chat {chat_id}: {e}")
        await query.answer(text="Произошла ошибка. Обратитесь к администратору.", show_alert=True)

async def combined_member_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles all ChatMember updates: new members joining, members leaving,
    and profile updates for existing members.
    """
    if not update.chat_member or not isinstance(update.chat_member, ChatMemberUpdated):
        return

    chat_id = update.chat_member.chat.id
    user = update.chat_member.new_chat_member.user
    new_member = update.chat_member.new_chat_member
    old_member = update.chat_member.old_chat_member

    # --- Case 1: User is leaving or was kicked ---
    if new_member.status in ("left", "kicked"):
        db.mark_left(chat_id, user.id)
        logger.info(f"User {user.id} left or was kicked from chat {chat_id}.")
        return

    # --- Case 2: A new member joins the chat ---
    is_new_join = (
        new_member.status == 'member' and
        old_member.status in ['left', 'kicked', 'restricted']
    )
    if is_new_join:
        await greet_new_member(update, context)
        return # greet_new_member handles everything for a new user

    # --- Case 3: An existing member's profile or status changes ---
    # Always track the user as active on any update
    db.upsert_member(chat_id, user, is_member=True)

    profile_changed = (
        old_member.user.username != new_member.user.username or
        old_member.user.first_name != new_member.user.first_name or
        old_member.user.last_name != new_member.user.last_name
    )

    if profile_changed:
        logger.info(f"User {user.id} updated their profile in chat {chat_id}. Re-checking...")
        # Re-use the logic from message-based checks, but without a message to delete
        if not db.is_whitelisted(chat_id, user.id):
            if await check_user_bio(chat_id, user.id, context, update): return
            names_to_check = [user.username, user.first_name, user.last_name]
            for val in filter(None, names_to_check):
                if await check_username(chat_id, user.id, val, context, update): break

def register_member_handlers(application):
    """Register member-related handlers."""
    # A single, combined handler for all membership changes (join, leave, update)
    application.add_handler(ChatMemberHandler(combined_member_update_handler, ChatMemberHandler.CHAT_MEMBER))
    
    # Handle username changes (when a user updates their profile)
    application.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS,
        check_username_update
    ))
    
    # Check usernames in all messages (high priority)
    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            check_message_username
        ),
        group=-1  # Highest priority group to scan before others
    )

    # Captcha callback handler
    application.add_handler(CallbackQueryHandler(verify_member_callback, pattern=r'^verify_'))
