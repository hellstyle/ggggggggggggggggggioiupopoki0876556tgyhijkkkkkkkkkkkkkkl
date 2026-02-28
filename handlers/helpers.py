import logging
from typing import Optional, Tuple, Dict, List
from telegram import Update, User
from telegram.ext import ContextTypes
from config import ADMIN_IDS

logger = logging.getLogger(__name__)

async def resolve_target_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[User]:
    """
    Resolves the target user from a command.
    The user can be specified by replying to their message or by their user_id.
    Returns the User object or None if not found.
    """
    # 1. Check for a replied-to message
    if update.message and update.message.reply_to_message:
        return update.message.reply_to_message.from_user

    # 2. Check for arguments (user_id)
    if context.args:
        target_arg = context.args[0]
        
        if target_arg.isdigit():
            user_id = int(target_arg)
            try:
                # get_chat can fetch user info by ID. It returns a Chat object.
                chat = await context.bot.get_chat(user_id)
                # We can construct a basic User object from the Chat object.
                # Note: is_bot might not be accurate this way.
                return User(id=chat.id, first_name=chat.first_name or "Unknown", is_bot=False, username=chat.username)
            except Exception as e:
                logger.warning(f"Could not find user by ID {user_id}: {e}")
                return None
        
        if target_arg.startswith('@'):
            await update.message.reply_text("Поиск по @username временно не поддерживается. Пожалуйста, используйте ID пользователя или ответьте на его сообщение.")
            return None

    return None

async def can_moderate_user(initiator: User, target: User, chat_id: int) -> Tuple[bool, str]:
    """
    Checks if the initiator has the right to moderate the target user.
    - Global admins can moderate anyone except other global admins.
    - No one can moderate themselves.
    """
    if initiator.id == target.id:
        return False, "Вы не можете модерировать самого себя."

    if target.id in ADMIN_IDS:
        return False, "Вы не можете модерировать глобального администратора."
            
    if target.is_bot:
        return False, "Модерация ботов отключена."

    return True, ""

# --- Message Cache for Deletion Fallback ---
# chat_id -> user_id -> [message_id]
_user_message_id_cache: Dict[int, Dict[int, List[int]]] = {}
USER_MESSAGE_CACHE_SIZE = 200 # Max messages to store per user per chat

def add_user_message_id(chat_id: int, user_id: int, message_id: int):
    """Adds a message ID to the in-memory cache for a user."""
    chat_cache = _user_message_id_cache.setdefault(chat_id, {})
    user_messages = chat_cache.setdefault(user_id, [])
    user_messages.append(message_id)
    # Keep cache size in check by removing the oldest message
    if len(user_messages) > USER_MESSAGE_CACHE_SIZE:
        user_messages.pop(0)

async def delete_cached_messages(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    """
    Deletes messages for a user from the in-memory cache.
    This serves as a fallback or supplement to `revoke_messages`.
    """
    chat_cache = _user_message_id_cache.get(chat_id, {})
    message_ids = chat_cache.pop(user_id, []) # Get and remove from cache

    if not message_ids:
        return

    logger.info(f"Fallback Deletion: Attempting to delete {len(message_ids)} cached messages for user {user_id} in chat {chat_id}.")

    for i in range(0, len(message_ids), 100):
        chunk = message_ids[i:i+100]
        try:
            await context.bot.delete_messages(chat_id=chat_id, message_ids=chunk)
        except Exception as e:
            logger.warning(f"Could not bulk-delete cached messages for user {user_id}. It's possible they were already deleted. Error: {e}")
