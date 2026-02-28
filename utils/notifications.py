import logging
from telegram import User, Chat, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import ADMIN_IDS

logger = logging.getLogger(__name__)

async def propose_global_ban(
    context: ContextTypes.DEFAULT_TYPE,
    user_to_ban: User,
    chat_where_banned: Chat,
    reason: str,
):
    """Sends a message to all global admins to propose a global ban."""
    if not ADMIN_IDS:
        logger.warning("Cannot propose global ban. No ADMIN_IDS configured.")
        return

    user_mention = user_to_ban.mention_html()
    text = (
        f"Пользователь {user_mention} (ID: <code>{user_to_ban.id}</code>) был "
        f"автоматически забанен в чате «{chat_where_banned.title}».\n"
        f"<b>Причина:</b> {reason}\n\n"
        "Добавить этого пользователя в глобальный черный список?"
    )

    keyboard = [[
        InlineKeyboardButton("✅ Да, добавить глобально", callback_data=f"global_ban_confirm_{user_to_ban.id}"),
        InlineKeyboardButton("❌ Нет", callback_data="global_ban_reject"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id, text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Failed to send global ban proposal to admin {admin_id}: {e}")