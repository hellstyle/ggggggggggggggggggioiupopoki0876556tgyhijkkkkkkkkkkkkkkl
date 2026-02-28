import logging
from typing import Union, Dict
from collections import deque
from telegram.ext import JobQueue, ContextTypes
from telegram import Update
from telegram.constants import ChatType

from config import ADMIN_IDS
from utils.database import db
from utils.text_utils import normalize_text

logger = logging.getLogger(__name__)

async def _delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Job to delete a message.
    """
    job = context.job
    chat_id = job.data['chat_id']
    message_id = job.data['message_id']
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.debug(f"Successfully deleted message {message_id} in chat {chat_id} via job.")
    except Exception as e:
        logger.warning(f"Could not delete message {message_id} in chat {chat_id} via job: {e}")

def schedule_message_deletion(job_queue: JobQueue, chat_id: Union[int, str], message_id: int, delay: int = 10):
    """Schedules a message to be deleted after a delay using the JobQueue."""
    job_queue.run_once(
        _delete_message_job,
        when=delay,
        data={'chat_id': chat_id, 'message_id': message_id},
        name=f"delete-{chat_id}-{message_id}"
    )

# --- Bot Message Cache for Mimicry Detection ---
bot_message_cache: Dict[int, deque] = {}
BOT_MESSAGE_CACHE_SIZE = 20  # Store last 20 messages per chat

def add_bot_message_to_cache(chat_id: int, text: str):
    """Adds a bot's message to the cache for mimicry detection."""
    if not text:
        return
    if chat_id not in bot_message_cache:
        bot_message_cache[chat_id] = deque(maxlen=BOT_MESSAGE_CACHE_SIZE)
    
    normalized_text = normalize_text(text)
    if normalized_text and normalized_text not in bot_message_cache[chat_id]:
        bot_message_cache[chat_id].append(normalized_text)

async def is_global_admin(user_id: int) -> bool:
    """Checks if a user is a global bot admin."""
    return user_id in ADMIN_IDS

async def is_admin(update: Update) -> bool:
    """
    Checks if a user has admin privileges for the bot in the current context.
    This is true if they are a global admin or a chat-specific admin.
    """
    if not update.effective_user:
        return False
    
    user_id = update.effective_user.id

    # 1. Global admins have access everywhere
    if await is_global_admin(user_id):
        return True
    
    # 2. Check for chat-specific admin in the database if in a group
    if update.effective_chat and update.effective_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if db.is_chat_admin(update.effective_chat.id, user_id):
            return True

    return False
