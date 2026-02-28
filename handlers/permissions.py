from telegram import ChatPermissions

# --- Permission Constants ---

# Allows everything a normal user can do. Used for unmuting or after verification.
PERMS_UNRESTRICTED = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True, can_invite_users=True
)

# Restricts a user to sending only text messages. Used for new members without captcha.
PERMS_MEDIA_RESTRICT = ChatPermissions(
    can_send_messages=True,
    can_send_audios=False, can_send_documents=False, can_send_photos=False,
    can_send_videos=False, can_send_video_notes=False, can_send_voice_notes=False,
    can_send_polls=False, can_send_other_messages=False,  # this blocks stickers/gifs
    can_add_web_page_previews=False,  # this blocks links
    can_invite_users=True
)

# Restricts a user from sending anything. Used for new members pending captcha.
PERMS_FULL_RESTRICT = ChatPermissions(
    can_send_messages=False, can_send_audios=False, can_send_documents=False,
    can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
    can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
    can_add_web_page_previews=False
)

