import json
import logging
import time
import sqlite3
from pathlib import Path
from datetime import timedelta
from typing import List, Dict, Set, Optional, Any, Tuple

# Robust import of config when running this file directly
try:
    from config import TRIGGERS_FILE, BANNED_USERS_FILE
except ModuleNotFoundError:
    # Add project root to sys.path
    import sys, os
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from config import TRIGGERS_FILE, BANNED_USERS_FILE

# Import db_schema in a way that works both as package and script
try:
    from .database_schema import db_schema  # when imported as package
except Exception:
    # Fallback for direct script execution
    from utils.database_schema import db_schema

logger = logging.getLogger(__name__)

class Database:
    def __init__(self):
        # Initialize the database schema first
        self.conn = db_schema.conn
        self.cursor = self.conn.cursor()
        
        # Initialize in-memory data structures
        self.triggers: Set[str] = set()
        self.banned_users: Dict[int, Dict] = {}
        self.ban_patterns: List[str] = []
        self.ban_words: Set[str] = set()
        self.ban_nickname_words: Dict[int, Set[str]] = {}  # chat_id -> set of words
        
        # Create tables and load data
        self._create_tables()
        self._load_data()

    def _create_tables(self):
        # Ensure the connection is good
        self.conn.execute("PRAGMA foreign_keys = ON")
        
        # Create warnings table if it doesn't exist
        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            warned_by INTEGER NOT NULL,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, chat_id)
        )
        ''')

        # Create chat rules table
        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_rules (
            chat_id INTEGER PRIMARY KEY,
            rules_text TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES chat_settings(chat_id) ON DELETE CASCADE
        )
        ''')
        # Add ad text column if it doesn't exist
        self.cursor.execute("PRAGMA table_info(chat_rules)")
        chat_rules_columns = [col[1] for col in self.cursor.fetchall()]
        if 'rules_ad_text' not in chat_rules_columns:
            self.cursor.execute('ALTER TABLE chat_rules ADD COLUMN rules_ad_text TEXT')

        # Create welcome settings table
        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS welcome_settings (
            chat_id INTEGER PRIMARY KEY,
            message_text TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES chat_settings(chat_id) ON DELETE CASCADE
        )
        ''')
        # Add ad text column if it doesn't exist
        self.cursor.execute("PRAGMA table_info(welcome_settings)")
        welcome_settings_columns = [col[1] for col in self.cursor.fetchall()]
        if 'welcome_ad_text' not in welcome_settings_columns:
            self.cursor.execute('ALTER TABLE welcome_settings ADD COLUMN welcome_ad_text TEXT')

        # Create profile checks table
        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS profile_checks (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            last_check_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, user_id)
        )
        ''')

        self.conn.commit()

    def close(self):
        """Closes the database connection via the schema object and nullifies local references."""
        if self.conn:
            db_schema.close()  # This closes the actual connection and sets db_schema.conn to None
            self.conn = None
            self.cursor = None
            logger.info("Database connection has been closed and references nullified.")

    def __del__(self):
        """Ensure the database connection is closed when the object is destroyed."""
        # The connection is managed by db_schema, which has its own __del__.
        # No need to do anything here to prevent double-closing.
        pass

    def warn_user(self, user_id: int, chat_id: int, warned_by: int, reason: str = None) -> bool:
        """Add a warning for a user.
        
        Args:
            user_id: ID of the user to warn
            chat_id: ID of the chat where the warning was issued
            warned_by: ID of the admin who issued the warning
            reason: Reason for the warning (optional)
            
        Returns:
            bool: True if warning was added, False if user already has a warning
        """
        try:
            self.cursor.execute(
                """
                INSERT OR IGNORE INTO user_warnings (user_id, chat_id, warned_by, reason)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, chat_id, warned_by, reason)
            )
            self.conn.commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error warning user {user_id}: {e}")
            return False
            
    def unwarn_user(self, user_id: int, chat_id: int) -> bool:
        """Remove a warning from a user.
        
        Args:
            user_id: ID of the user to remove warning from
            chat_id: ID of the chat where the warning was issued
            
        Returns:
            bool: True if warning was removed, False if no warning was found
        """
        try:
            self.cursor.execute(
                """
                DELETE FROM user_warnings
                WHERE user_id = ? AND chat_id = ?
                """,
                (user_id, chat_id)
            )
            self.conn.commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error unwarning user {user_id}: {e}")
            return False
            
    def get_user_warning(self, user_id: int, chat_id: int) -> dict:
        """Get warning information for a user.
        
        Args:
            user_id: ID of the user
            chat_id: ID of the chat
            
        Returns:
            dict: Warning information or None if no warning found
        """
        try:
            self.cursor.execute(
                """
                SELECT * FROM user_warnings
                WHERE user_id = ? AND chat_id = ?
                """,
                (user_id, chat_id)
            )
            row = self.cursor.fetchone()
            if row:
                columns = [col[0] for col in self.cursor.description]
                return dict(zip(columns, row))
            return None
        except Exception as e:
            logger.error(f"Error getting warning for user {user_id}: {e}")
            return None
    
    def _load_data(self):
        """Load data from database into memory."""
        try:
            # Load triggers from database
            cursor = self._execute("SELECT trigger FROM triggers", commit=False)
            self.triggers = {row[0] for row in cursor.fetchall()}
            
            # Load ban patterns from database
            cursor = self._execute("SELECT pattern, description FROM ban_patterns", commit=False)
            self.ban_patterns = [{'pattern': row[0], 'description': row[1]} for row in cursor.fetchall()]
            
            # Load banned users from database
            cursor = self._execute(
                """
                SELECT user_id, username, first_name, last_name, reason, admin_id, banned_at 
                FROM banned_users 
                WHERE is_active = 1
                """,
                commit=False
            )
            for row in cursor.fetchall():
                self.banned_users[row[0]] = {
                    'username': row[1],
                    'first_name': row[2],
                    'last_name': row[3],
                    'reason': row[4],
                    'admin_id': row[5],
                    'banned_at': row[6]
                }
            
            # Load banned nickname words from database
            cursor = self._execute(
                "SELECT chat_id, word FROM ban_nickname_words ORDER BY chat_id, word",
                commit=False
            )
            for chat_id, word in cursor.fetchall():
                if chat_id not in self.ban_nickname_words:
                    self.ban_nickname_words[chat_id] = set()
                self.ban_nickname_words[chat_id].add(word)
                
        except sqlite3.Error as e:
            logger.error(f"Error loading data from database: {e}")
            # Initialize empty data structures in case of error
            self.triggers = set()
            self.ban_patterns = []
            self.banned_users = {}
            self.ban_nickname_words = {}

    def migrate_old_data(self):
        """Migrate data from JSON files to SQLite database."""
        try:
            # Migrate triggers (if needed)
            if TRIGGERS_FILE.exists():
                with open(TRIGGERS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                    if isinstance(data, dict):
                        # Migrate ban words
                        for word in data.get('ban_words', []):
                            self.add_ban_word(0, word)
                            
                        # Migrate ban nickname words
                        for word in data.get('ban_nickname_words', []):
                            self.add_ban_nickname_word(0, word)
                            
                        # Migrate ban patterns
                        for pattern in data.get('ban_patterns', []):
                            self.add_ban_pattern(pattern)
                            
                        # Migrate word patterns
                        for pattern in data.get('ban_word_patterns', []):
                            self.add_ban_pattern(pattern)
                    
                    # After migration, rename the old file
                    old_file = TRIGGERS_FILE.rename(f"{TRIGGERS_FILE}.old")
                    logger.info(f"Migrated old data from {TRIGGERS_FILE} to SQLite")
            
            # Migrate banned users
            if BANNED_USERS_FILE.exists():
                with open(BANNED_USERS_FILE, 'r', encoding='utf-8') as f:
                    banned_users = json.load(f)
                    for user_id, data in banned_users.items():
                        try:
                            self.ban_user(
                                user_id=int(user_id),
                                reason=data.get('reason', 'No reason provided'),
                                admin_id=data.get('admin_id', 0),
                                username=data.get('username'),
                                first_name=data.get('first_name'),
                                last_name=data.get('last_name')
                            )
                        except (ValueError, KeyError) as e:
                            logger.error(f"Error migrating banned user {user_id}: {e}")
                
                # After migration, rename the old file
                old_file = BANNED_USERS_FILE.rename(f"{BANNED_USERS_FILE}.old")
                logger.info(f"Migrated banned users from {BANNED_USERS_FILE} to SQLite")
                
        except Exception as e:
            logger.error(f"Error during data migration: {e}")
            # Don't raise, continue with empty database if migration fails

    def _execute(self, query: str, params: Tuple[Any, ...] = (), commit: bool = True) -> sqlite3.Cursor:
        self.cursor.execute(query, params)
        if commit:
            self.conn.commit()
        return self.cursor

    # Known members management
    def upsert_member(self, chat_id: int, user: Any, is_member: bool = True) -> bool:
        """Insert or update a known member record.
        Expects user to have attributes or dict keys: id, username, first_name, last_name.
        """
        try:
            user_id = getattr(user, 'id', None) if not isinstance(user, dict) else user.get('id')
            if not user_id:
                return False
            username = getattr(user, 'username', None) if not isinstance(user, dict) else user.get('username')
            first_name = getattr(user, 'first_name', None) if not isinstance(user, dict) else user.get('first_name')
            last_name = getattr(user, 'last_name', None) if not isinstance(user, dict) else user.get('last_name')

            self._execute(
                """
                INSERT INTO known_members (chat_id, user_id, username, first_name, last_name, is_member, last_seen, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    is_member=excluded.is_member,
                    last_seen=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (chat_id, user_id, username, first_name, last_name, 1 if is_member else 0)
            )
            return True
        except sqlite3.Error as e:
            logger.error(f"Error upserting known member {chat_id}:{user_id}: {e}")
            return False

    def mark_left(self, chat_id: int, user_id: int) -> bool:
        """Mark a member as left the chat."""
        try:
            self._execute(
                """
                INSERT INTO known_members (chat_id, user_id, is_member, last_seen, updated_at)
                VALUES (?, ?, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    is_member=0,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (chat_id, user_id)
            )
            return True
        except sqlite3.Error as e:
            logger.error(f"Error marking member left {chat_id}:{user_id}: {e}")
            return False

    def get_known_members(self, chat_id: int, only_active: bool = True) -> List[Dict[str, Any]]:
        """Return known members for the chat."""
        try:
            if only_active:
                cursor = self._execute(
                    """
                    SELECT user_id, username, first_name, last_name, is_member, last_seen
                    FROM known_members
                    WHERE chat_id = ? AND is_member = 1
                    ORDER BY last_seen DESC
                    """,
                    (chat_id,),
                    commit=False
                )
            else:
                cursor = self._execute(
                    """
                    SELECT user_id, username, first_name, last_name, is_member, last_seen
                    FROM known_members
                    WHERE chat_id = ?
                    ORDER BY last_seen DESC
                    """,
                    (chat_id,),
                    commit=False
                )
            rows = cursor.fetchall()
            return [
                {
                    'user_id': r[0],
                    'username': r[1],
                    'first_name': r[2],
                    'last_name': r[3],
                    'is_member': bool(r[4]),
                    'last_seen': r[5],
                }
                for r in rows
            ]
        except sqlite3.Error as e:
            logger.error(f"Error fetching known members for chat {chat_id}: {e}")
            return []

    # Chat settings management
    def get_or_create_chat(self, chat_id: int, title: str = None) -> bool:
        """Get or create chat settings."""
        try:
            self._execute(
                """
                INSERT OR IGNORE INTO chat_settings (chat_id, title) 
                VALUES (?, ?)
                """,
                (chat_id, title or f"Chat {chat_id}")
            )
            if title:
                self._execute(
                    """
                    UPDATE chat_settings 
                    SET title = ?, updated_at = CURRENT_TIMESTAMP 
                    WHERE chat_id = ?
                    """,
                    (title, chat_id)
                )
            return True
        except sqlite3.Error as e:
            logger.error(f"Error in get_or_create_chat: {e}")
            return False

    def set_link_deletion(self, chat_id: int, enabled: bool) -> bool:
        """Enable or disable automatic link deletion for a chat."""
        try:
            self.get_or_create_chat(chat_id)
            self._execute(
                "UPDATE chat_settings SET delete_links_enabled = ? WHERE chat_id = ?",
                (1 if enabled else 0, chat_id)
            )
            return True
        except sqlite3.Error as e:
            logger.error(f"Error setting link deletion for chat {chat_id}: {e}")
            return False

    def is_link_deletion_enabled(self, chat_id: int) -> bool:
        """Check if automatic link deletion is enabled for a chat."""
        try:
            cursor = self._execute(
                "SELECT delete_links_enabled FROM chat_settings WHERE chat_id = ?",
                (chat_id,),
                commit=False
            )
            result = cursor.fetchone()
            # Ensure we return a boolean
            return bool(result[0]) if result else False
        except sqlite3.Error as e:
            logger.error(f"Error checking link deletion for chat {chat_id}: {e}")
            return False

    def set_welcome_captcha(self, chat_id: int, enabled: bool) -> bool:
        """Enable or disable welcome captcha for a chat."""
        try:
            self.get_or_create_chat(chat_id)
            self._execute(
                "UPDATE chat_settings SET welcome_captcha_enabled = ? WHERE chat_id = ?",
                (1 if enabled else 0, chat_id)
            )
            return True
        except sqlite3.Error as e:
            logger.error(f"Error setting welcome captcha for chat {chat_id}: {e}")
            return False

    def is_welcome_captcha_enabled(self, chat_id: int) -> bool:
        """Check if welcome captcha is enabled for a chat."""
        try:
            cursor = self._execute(
                "SELECT welcome_captcha_enabled FROM chat_settings WHERE chat_id = ?",
                (chat_id,),
                commit=False
            )
            result = cursor.fetchone()
            # Ensure we return a boolean
            return bool(result[0]) if result else False
        except sqlite3.Error as e:
            logger.error(f"Error checking welcome captcha for chat {chat_id}: {e}")
            return False

    def set_chat_active_status(self, chat_id: int, is_active: bool) -> bool:
        """Sets the active status of a chat."""
        try:
            self.get_or_create_chat(chat_id)
            self._execute(
                "UPDATE chat_settings SET is_active = ? WHERE chat_id = ?",
                (1 if is_active else 0, chat_id)
            )
            return True
        except sqlite3.Error as e:
            logger.error(f"Error setting active status for chat {chat_id}: {e}")
            return False

    # Chat Admins Management
    def add_chat_admin(self, chat_id: int, user_id: int, added_by: int) -> bool:
        """Add a user as an admin for a specific chat."""
        try:
            self.get_or_create_chat(chat_id)
            self._execute(
                """
                INSERT OR IGNORE INTO chat_admins (chat_id, user_id, added_by)
                VALUES (?, ?, ?)
                """,
                (chat_id, user_id, added_by)
            )
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error adding chat admin {user_id} for chat {chat_id}: {e}")
            return False

    def remove_chat_admin(self, chat_id: int, user_id: int) -> bool:
        """Remove a user as an admin for a specific chat."""
        try:
            self._execute(
                "DELETE FROM chat_admins WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id)
            )
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error removing chat admin {user_id} for chat {chat_id}: {e}")
            return False

    def is_chat_admin(self, chat_id: int, user_id: int) -> bool:
        """Check if a user is a specific admin for a chat."""
        try:
            cursor = self._execute(
                "SELECT 1 FROM chat_admins WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
                commit=False
            )
            return cursor.fetchone() is not None
        except sqlite3.Error as e:
            logger.error(f"Error checking chat admin status for {user_id} in {chat_id}: {e}")
            return False

    def get_chat_admins(self, chat_id: int) -> List[int]:
        """Get all specific admin user IDs for a chat."""
        try:
            cursor = self._execute(
                "SELECT user_id FROM chat_admins WHERE chat_id = ?",
                (chat_id,),
                commit=False
            )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting chat admins for chat {chat_id}: {e}")
            return []

    # Whitelist Management
    def add_whitelist_user(self, chat_id: int, user_id: int, added_by: int) -> bool:
        """Add a user to the whitelist for a specific chat."""
        try:
            self.get_or_create_chat(chat_id)
            self._execute(
                """
                INSERT OR IGNORE INTO whitelisted_users (chat_id, user_id, added_by)
                VALUES (?, ?, ?)
                """,
                (chat_id, user_id, added_by)
            )
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error adding whitelisted user {user_id} for chat {chat_id}: {e}")
            return False

    def remove_whitelist_user(self, chat_id: int, user_id: int) -> bool:
        """Remove a user from the whitelist for a specific chat."""
        try:
            self._execute(
                "DELETE FROM whitelisted_users WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id)
            )
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error removing whitelisted user {user_id} for chat {chat_id}: {e}")
            return False

    def is_whitelisted(self, chat_id: int, user_id: int) -> bool:
        """Check if a user is whitelisted in a specific chat."""
        try:
            cursor = self._execute(
                "SELECT 1 FROM whitelisted_users WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
                commit=False
            )
            return cursor.fetchone() is not None
        except sqlite3.Error as e:
            logger.error(f"Error checking whitelist status for {user_id} in {chat_id}: {e}")
            return False

    def get_whitelisted_users(self, chat_id: int) -> List[int]:
        """Get all whitelisted user IDs for a chat."""
        try:
            cursor = self._execute(
                "SELECT user_id FROM whitelisted_users WHERE chat_id = ?",
                (chat_id,),
                commit=False
            )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting whitelisted users for chat {chat_id}: {e}")
            return []

    # Profile check management
    def mark_user_profile_checked(self, chat_id: int, user_id: int) -> bool:
        """Marks a user's profile as checked in a given chat."""
        try:
            self._execute(
                """
                INSERT INTO profile_checks (chat_id, user_id, last_check_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    last_check_at=CURRENT_TIMESTAMP
                """,
                (chat_id, user_id)
            )
            return True
        except sqlite3.Error as e:
            logger.error(f"Error marking user profile as checked for {user_id} in {chat_id}: {e}")
            return False

    def get_unchecked_known_members(self, chat_id: int, only_active_chat: bool = False) -> List[Dict[str, Any]]:
        """Return known active members for the chat who have not been checked yet."""
        query = """
            SELECT km.user_id, km.username, km.first_name, km.last_name
            FROM known_members as km
            LEFT JOIN profile_checks as pc ON km.chat_id = pc.chat_id AND km.user_id = pc.user_id
            WHERE km.chat_id = ? AND km.is_member = 1 AND pc.user_id IS NULL
        """
        if only_active_chat:
            query += " AND km.chat_id IN (SELECT chat_id FROM chat_settings WHERE is_active = 1)"

        try:
            cursor = self._execute(
                query,
                (chat_id,),
                commit=False
            )
            rows = cursor.fetchall()
            return [
                {
                    'user_id': r[0],
                    'username': r[1],
                    'first_name': r[2],
                    'last_name': r[3],
                }
                for r in rows
            ]
        except sqlite3.Error as e:
            logger.error(f"Error fetching unchecked known members for chat {chat_id}: {e}")
            return []

    def get_all_known_chat_ids(self) -> List[int]:
        """Gets all unique chat_ids from the known_members table."""
        try:
            cursor = self._execute(
                "SELECT DISTINCT chat_id FROM known_members",
                commit=False
            )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting all known chat IDs: {e}")
            return []

    # Avatar Ban Management
    def add_banned_avatar(self, file_unique_id: str, file_id: str, phash: str, admin_id: int) -> bool:
        """Adds a profile photo's unique ID and file ID to the banned list."""
        try:
            self._execute(
                """
                INSERT OR IGNORE INTO banned_avatars (file_unique_id, file_id, phash, added_by)
                VALUES (?, ?, ?, ?)
                """,
                (file_unique_id, file_id, phash, admin_id)
            )
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error adding banned avatar {file_unique_id}: {e}")
            return False

    def remove_banned_avatar(self, file_unique_id: str) -> bool:
        """Removes a profile photo from the banned list."""
        try:
            self._execute(
                "DELETE FROM banned_avatars WHERE file_unique_id = ?",
                (file_unique_id,)
            )
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error removing banned avatar {file_unique_id}: {e}")
            return False

    def is_avatar_banned(self, file_unique_id: str) -> bool:
        """Checks if a profile photo's unique ID is in the banned list."""
        try:
            cursor = self._execute(
                "SELECT 1 FROM banned_avatars WHERE file_unique_id = ?",
                (file_unique_id,),
                commit=False
            )
            return cursor.fetchone() is not None
        except sqlite3.Error as e:
            logger.error(f"Error checking banned avatar status for {file_unique_id}: {e}")
            return False

    def get_banned_avatars(self) -> List[Dict[str, Any]]:
        """Gets all banned avatar records."""
        try:
            cursor = self._execute(
                "SELECT id, file_unique_id, file_id, phash, added_by, created_at FROM banned_avatars ORDER BY created_at DESC",
                commit=False
            )
            return [
                {
                    "id": row[0],
                    "file_unique_id": row[1],
                    "file_id": row[2],
                    "phash": row[3],
                    "added_by": row[4],
                    "created_at": row[5],
                }
                for row in cursor.fetchall()
            ]
        except sqlite3.Error as e:
            logger.error(f"Error getting banned avatars: {e}")
            return []

    def get_all_banned_avatar_hashes(self) -> List[str]:
        """Gets all perceptual hashes from the banned avatars table."""
        try:
            cursor = self._execute(
                "SELECT phash FROM banned_avatars WHERE phash IS NOT NULL",
                commit=False
            )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting banned avatar hashes: {e}")
            return []

    # Bannable link domains management
    def add_bannable_domain(self, chat_id: int, domain: str, admin_id: int) -> bool:
        """Add a domain to the auto-ban list for a chat."""
        domain = domain.lower().strip()
        try:
            self.get_or_create_chat(chat_id)
            self._execute(
                """
                INSERT OR IGNORE INTO bannable_link_domains (chat_id, domain, added_by)
                VALUES (?, ?, ?)
                """,
                (chat_id, domain, admin_id)
            )
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error adding bannable domain {domain} for chat {chat_id}: {e}")
            return False

    def remove_bannable_domain(self, chat_id: int, domain: str) -> bool:
        """Remove a domain from the auto-ban list."""
        domain = domain.lower().strip()
        try:
            self._execute(
                "DELETE FROM bannable_link_domains WHERE chat_id = ? AND domain = ?",
                (chat_id, domain)
            )
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error removing bannable domain {domain} for chat {chat_id}: {e}")
            return False

    def get_bannable_domains(self, chat_id: int) -> List[str]:
        """Get all bannable domains for a chat."""
        try:
            cursor = self._execute(
                "SELECT domain FROM bannable_link_domains WHERE chat_id = ? ORDER BY domain",
                (chat_id,),
                commit=False
            )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting bannable domains for chat {chat_id}: {e}")
            return []

    # Rules management
    def set_chat_rules(self, chat_id: int, rules_text: str) -> bool:
        """Set or update the rules for a specific chat."""
        try:
            # Ensure chat exists in chat_settings
            self.get_or_create_chat(chat_id)
            
            self._execute(
                """
                INSERT INTO chat_rules (chat_id, rules_text, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET
                    rules_text=excluded.rules_text,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (chat_id, rules_text)
            )
            return True
        except sqlite3.Error as e:
            logger.error(f"Error setting rules for chat {chat_id}: {e}")
            return False

    def get_chat_rules(self, chat_id: int) -> Optional[str]:
        """Get the rules for a specific chat."""
        try:
            cursor = self._execute(
                "SELECT rules_text FROM chat_rules WHERE chat_id = ?",
                (chat_id,),
                commit=False
            )
            result = cursor.fetchone()
            return result[0] if result else None
        except sqlite3.Error as e:
            logger.error(f"Error getting rules for chat {chat_id}: {e}")
            return None

    def delete_chat_rules(self, chat_id: int) -> bool:
        """Delete the rules for a specific chat."""
        try:
            self._execute("DELETE FROM chat_rules WHERE chat_id = ?", (chat_id,))
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error deleting rules for chat {chat_id}: {e}")
            return False

    # --- Rules Ad Management ---

    def set_rules_ad(self, chat_id: int, ad_text: str) -> bool:
        """Sets or updates the ad text for rules for a specific chat."""
        try:
            self.get_or_create_chat(chat_id) # Ensures foreign key constraint is met
            self._execute(
                """
                INSERT INTO chat_rules (chat_id, rules_text, rules_ad_text)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    rules_ad_text=excluded.rules_ad_text
                """,
                (chat_id, "Правила для этого чата не установлены.", ad_text)
            )
            return True
        except sqlite3.Error as e:
            logger.error(f"Error setting rules ad for chat {chat_id}: {e}")
            return False

    def get_rules_ad(self, chat_id: int) -> Optional[str]:
        """Gets the ad text for rules for a specific chat."""
        try:
            cursor = self._execute(
                "SELECT rules_ad_text FROM chat_rules WHERE chat_id = ?", (chat_id,), commit=False
            )
            result = cursor.fetchone()
            return result[0] if result and result[0] else None
        except sqlite3.Error as e:
            logger.error(f"Error getting rules ad for chat {chat_id}: {e}")
            return None

    def delete_rules_ad(self, chat_id: int) -> bool:
        """Deletes the ad text for rules for a specific chat."""
        try:
            self._execute(
                "UPDATE chat_rules SET rules_ad_text = NULL WHERE chat_id = ?",
                (chat_id,)
            )
            # We check changes because the row might not exist, which is not an error.
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error deleting rules ad for chat {chat_id}: {e}")
            return False

    # --- Welcome Ad Management ---

    def set_welcome_ad(self, chat_id: int, ad_text: str) -> bool:
        """Sets or updates the ad text for the welcome message."""
        try:
            self.get_or_create_chat(chat_id) # Ensures foreign key constraint is met
            self._execute(
                """
                INSERT INTO welcome_settings (chat_id, message_text, welcome_ad_text)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    welcome_ad_text=excluded.welcome_ad_text
                """,
                (chat_id, "Добро пожаловать!", ad_text)
            )
            return True
        except sqlite3.Error as e:
            logger.error(f"Error setting welcome ad for chat {chat_id}: {e}")
            return False

    def get_welcome_ad(self, chat_id: int) -> Optional[str]:
        """Gets the ad text for the welcome message."""
        try:
            cursor = self._execute(
                "SELECT welcome_ad_text FROM welcome_settings WHERE chat_id = ?", (chat_id,), commit=False
            )
            result = cursor.fetchone()
            return result[0] if result and result[0] else None
        except sqlite3.Error as e:
            logger.error(f"Error getting welcome ad for chat {chat_id}: {e}")
            return None

    def delete_welcome_ad(self, chat_id: int) -> bool:
        """Deletes the ad text for the welcome message."""
        try:
            self._execute(
                "UPDATE welcome_settings SET welcome_ad_text = NULL WHERE chat_id = ?",
                (chat_id,)
            )
            # We check changes because the row might not exist, which is not an error.
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error deleting welcome ad for chat {chat_id}: {e}")
            return False

    # Welcome message management
    def set_welcome_message(self, chat_id: int, message_text: str) -> bool:
        """Set or update the welcome message for a chat."""
        try:
            self.get_or_create_chat(chat_id)
            self._execute(
                """
                INSERT INTO welcome_settings (chat_id, message_text, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET
                    message_text=excluded.message_text,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (chat_id, message_text)
            )
            return True
        except sqlite3.Error as e:
            logger.error(f"Error setting welcome message for chat {chat_id}: {e}")
            return False

    def get_welcome_message(self, chat_id: int) -> Optional[Dict[str, Any]]:
        """Get the welcome message for a chat."""
        try:
            cursor = self._execute(
                "SELECT message_text FROM welcome_settings WHERE chat_id = ?",
                (chat_id,),
                commit=False
            )
            result = cursor.fetchone()
            if result:
                return {"text": result[0]}
            return None
        except sqlite3.Error as e:
            logger.error(f"Error getting welcome message for chat {chat_id}: {e}")
            return None

    def delete_welcome_message(self, chat_id: int) -> bool:
        """Delete the welcome message for a chat."""
        try:
            self._execute("DELETE FROM welcome_settings WHERE chat_id = ?", (chat_id,))
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error deleting welcome message for chat {chat_id}: {e}")
            return False

    # Trigger management
    def add_trigger(self, chat_id: int, word: str, response: str) -> bool:
        """Add a trigger for a specific chat."""
        try:
            # Ensure chat exists
            self.get_or_create_chat(chat_id)
            
            self._execute(
                """
                INSERT OR REPLACE INTO triggers (chat_id, trigger, response) 
                VALUES (?, ?, ?)
                """,
                (chat_id, word, response)
            )
            return True
        except sqlite3.Error as e:
            logger.error(f"Error adding trigger: {e}")
            return False

    def remove_trigger(self, chat_id: int, word: str) -> bool:
        """Remove a trigger from a specific chat."""
        try:
            self._execute(
                "DELETE FROM triggers WHERE chat_id = ? AND trigger = ?",
                (chat_id, word)
            )
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error removing trigger: {e}")
            return False

    def get_trigger_response(self, chat_id: int, trigger: str) -> Optional[str]:
        """Get the response for a trigger in a specific chat if it exists."""
        try:
            cursor = self._execute(
                """
                SELECT response FROM triggers 
                WHERE chat_id = ? AND ? LIKE '%' || trigger || '%'
                ORDER BY LENGTH(trigger) DESC
                LIMIT 1
                """, 
                (chat_id, trigger),
                commit=False
            )
            result = cursor.fetchone()
            return result[0] if result else None
        except sqlite3.Error as e:
            logger.error(f"Error getting trigger response: {e}")
            return None
            
    def get_chat_triggers(self, chat_id: int) -> List[tuple]:
        """Get all triggers for a specific chat."""
        try:
            cursor = self._execute(
                "SELECT trigger, response FROM triggers WHERE chat_id = ?", 
                (chat_id,),
                commit=False
            )
            return cursor.fetchall()
        except sqlite3.Error as e:
            logger.error(f"Error getting chat triggers: {e}")
            return []

    # Moderation Logging
    def log_moderation_action(self, chat_id: Optional[int], user_id: int, action: str, admin_id: Optional[int], reason: str = None, duration: Optional[timedelta] = None) -> bool:
        """Log a moderation action like ban, mute, warn."""
        try:
            duration_seconds = int(duration.total_seconds()) if duration else None
            self._execute(
                """
                INSERT INTO moderation_logs (chat_id, user_id, action, admin_id, reason, duration_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_id, user_id, action, admin_id, reason, duration_seconds)
            )
            return True
        except sqlite3.Error as e:
            logger.error(f"Error logging moderation action: {e}")
            return False

    def get_daily_moderation_stats(self) -> Dict[str, int]:
        """Get the count of bans and mutes in the last 24 hours."""
        stats = {'bans': 0, 'mutes': 0}
        try:
            # Get bans count
            cursor = self._execute(
                "SELECT COUNT(*) FROM moderation_logs WHERE action = 'ban' AND created_at >= datetime('now', '-24 hours')",
                commit=False
            )
            ban_count = cursor.fetchone()
            if ban_count:
                stats['bans'] = ban_count[0]

            # Get mutes count
            cursor = self._execute(
                "SELECT COUNT(*) FROM moderation_logs WHERE action = 'mute' AND created_at >= datetime('now', '-24 hours')",
                commit=False
            )
            mute_count = cursor.fetchone()
            if mute_count:
                stats['mutes'] = mute_count[0]
        except sqlite3.Error as e:
            logger.error(f"Error getting daily moderation stats: {e}")
        return stats

    def _save_triggers(self):
        # This method is kept for backward compatibility
        pass

    # Banned users management
    def ban_user(self, user_id: int, reason: str, admin_id: int, 
                username: str = None, first_name: str = None, last_name: str = None) -> bool:
        """Ban a user."""
        try:
            # First, check if user is already banned
            cursor = self._execute(
                "SELECT 1 FROM banned_users WHERE user_id = ? AND is_active = 1",
                (user_id,),
                commit=False
            )
            if cursor.fetchone():
                return False  # Already banned
                
            # Add to banned_users
            self._execute(
                """
                INSERT INTO banned_users 
                (user_id, username, first_name, last_name, reason, admin_id, is_active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (user_id, username, first_name, last_name, reason, admin_id)
            )
            
            self.log_moderation_action(chat_id=None, user_id=user_id, action='ban', admin_id=admin_id, reason=reason)
            
            return True
            
        except sqlite3.Error as e:
            logger.error(f"Error banning user {user_id}: {e}")
            return False
        
    def unban_user(self, user_id: int, admin_id: int = None) -> bool:
        """Unban a user."""
        try:
            # Mark as inactive in banned_users
            self._execute(
                "UPDATE banned_users SET is_active = 0, unbanned_at = CURRENT_TIMESTAMP WHERE user_id = ? AND is_active = 1",
                (user_id,)
            )
            
            if self._execute("SELECT changes()").fetchone()[0] > 0:
                self.log_moderation_action(chat_id=None, user_id=user_id, action='unban', admin_id=admin_id, reason="User unbanned by admin")
                return True
            return False
            
        except sqlite3.Error as e:
            logger.error(f"Error unbanning user {user_id}: {e}")
            return False
        
    def is_banned(self, user_id: int) -> bool:
        """Check if a user is currently banned."""
        try:
            cursor = self._execute(
                "SELECT 1 FROM banned_users WHERE user_id = ? AND is_active = 1",
                (user_id,),
                commit=False
            )
            return cursor.fetchone() is not None
        except sqlite3.Error as e:
            logger.error(f"Error checking if user {user_id} is banned: {e}")
            return False
        
    # Ban patterns management
    def add_ban_pattern(self, pattern: str, description: str = None) -> bool:
        """Add a ban pattern."""
        try:
            self._execute(
                """
                INSERT OR IGNORE INTO ban_patterns (pattern, description)
                VALUES (?, ?)
                """,
                (pattern, description)
            )
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error adding ban pattern: {e}")
            return False
        
    def remove_ban_pattern(self, pattern: str) -> bool:
        """Remove a ban pattern."""
        try:
            self._execute("DELETE FROM ban_patterns WHERE pattern = ?", (pattern,))
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error removing ban pattern: {e}")
            return False
        
    def get_ban_patterns(self) -> List[Dict[str, Any]]:
        """Get all ban patterns with their IDs and descriptions."""
        try:
            cursor = self._execute(
                "SELECT id, pattern, description FROM ban_patterns ORDER BY id",
                commit=False
            )
            return [{"id": row[0], "pattern": row[1], "description": row[2]} 
                   for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting ban patterns: {e}")
            return []
            
    def get_ban_patterns_list(self) -> List[str]:
        """Get just the list of ban patterns (for backward compatibility)."""
        return [p["pattern"] for p in self.get_ban_patterns()]
        
    # Ban words management
    def add_ban_word(self, chat_id: int, word: str) -> bool:
        """Add a pre-normalized word to ban list for a specific chat."""
        try:
            # Ensure chat exists
            self.get_or_create_chat(chat_id)
            
            self._execute(
                """
                INSERT OR IGNORE INTO ban_words (chat_id, word) 
                VALUES (?, ?)
                """,
                (chat_id, word)
            )
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error adding ban word: {e}")
            return False
        
    def remove_ban_word(self, chat_id: int, word: str) -> bool:
        """Remove a pre-normalized word from ban list for a specific chat."""
        try:
            self._execute(
                "DELETE FROM ban_words WHERE chat_id = ? AND word = ?", 
                (chat_id, word)
            )
            return self._execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"Error removing ban word: {e}")
            return False
        
    def get_chat_ban_words(self, chat_id: int) -> List[str]:
        """Get all banned words for a specific chat."""
        try:
            cursor = self._execute(
                "SELECT word FROM ban_words WHERE chat_id = ? ORDER BY word", 
                (chat_id,),
                commit=False
            )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting chat ban words: {e}")
            return []
            
    def check_banned_word(self, chat_id: int, text: str) -> Optional[str]:
        """Check if text contains any banned word for the chat."""
        try:
            cursor = self._execute(
                """
                SELECT word FROM ban_words 
                WHERE chat_id = ? AND ? LIKE '%' || word || '%'
                LIMIT 1
                """,
                (chat_id, text.lower()),
                commit=False
            )
            result = cursor.fetchone()
            return result[0] if result else None
        except sqlite3.Error as e:
            logger.error(f"Error checking banned word: {e}")
            return None
            
    def add_ban_nickname_word(self, chat_id: int, word: str, admin_id: int = None) -> bool:
        """Add a pre-normalized word to nickname ban list for a specific chat"""
        try:
            # Ensure chat exists
            self.get_or_create_chat(chat_id)
            
            # Add to database
            self._execute(
                """
                INSERT OR IGNORE INTO ban_nickname_words (chat_id, word, added_by) 
                VALUES (?, ?, ?)
                """,
                (chat_id, word, admin_id)
            )
            
            changes = self._execute("SELECT changes()").fetchone()[0] > 0
            
            if changes:
                # Update in-memory cache
                if chat_id not in self.ban_nickname_words:
                    self.ban_nickname_words[chat_id] = set()
                self.ban_nickname_words[chat_id].add(word)
                
                # Log the action
                self._log_ban_word_action(chat_id, 'nickname', word, 'add', admin_id)
                
            return changes
            
        except sqlite3.Error as e:
            logger.error(f"Error adding ban nickname word: {e}")
            return False
            
    def remove_ban_nickname_word(self, chat_id: int, word: str, admin_id: int = None) -> bool:
        """Remove a pre-normalized word from nickname ban list for a specific chat"""
        try:
            self._execute(
                "DELETE FROM ban_nickname_words WHERE chat_id = ? AND word = ?", 
                (chat_id, word)
            )
            changes = self._execute("SELECT changes()").fetchone()[0] > 0
            
            if changes and chat_id in self.ban_nickname_words and word in self.ban_nickname_words[chat_id]:
                self.ban_nickname_words[chat_id].remove(word)
                # Log the action
                self._log_ban_word_action(chat_id, 'nickname', word, 'remove', admin_id)
                
            return changes
            
        except sqlite3.Error as e:
            logger.error(f"Error removing ban nickname word: {e}")
            return False
            
    def get_ban_nickname_words(self, chat_id: int = None) -> List[str]:
        """Get all banned nickname words for a specific chat or globally if chat_id is None"""
        try:
            if chat_id is not None:
                cursor = self._execute(
                    "SELECT word FROM ban_nickname_words WHERE chat_id = ? ORDER BY word",
                    (chat_id,),
                    commit=False
                )
            else:
                cursor = self._execute(
                    "SELECT DISTINCT word FROM ban_nickname_words ORDER BY word",
                    commit=False
                )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting ban nickname words: {e}")
            return []
            
    def _log_ban_word_action(self, chat_id: int, word_type: str, word: str, action: str, admin_id: int = None) -> None:
        """Log ban word actions"""
        try:
            self._execute(
                """
                INSERT INTO ban_word_logs (chat_id, word_type, word, action, admin_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, word_type, word, action, admin_id)
            )
        except sqlite3.Error as e:
            logger.error(f"Error logging ban word action: {e}")

    def check_banned_nickname(self, chat_id: int, username: str) -> Optional[str]:
        """
        Check if username contains any banned word for the chat.
        Performs case-insensitive check and handles different word boundaries.
        """
        if not username:
            return None
            
        # Convert both the username and banned words to lowercase for case-insensitive comparison
        username_lower = username.lower()
        
        # Check in memory cache first
        if chat_id in self.ban_nickname_words:
            for word in self.ban_nickname_words[chat_id]:
                word_lower = word.lower()
                # Check for word as substring
                if word_lower in username_lower:
                    return word
                # Also check for word with non-word characters
                if not word_lower.isalnum() and word_lower in username_lower:
                    return word
        
        # If not found in cache, check database and update cache
        try:
            cursor = self._execute(
                """
                SELECT word FROM ban_nickname_words 
                WHERE chat_id = ? AND LOWER(?) LIKE '%' || LOWER(word) || '%' 
                LIMIT 1
                """,
                (chat_id, username_lower),
                commit=False
            )
            result = cursor.fetchone()
            if result:
                # Update cache
                if chat_id not in self.ban_nickname_words:
                    self.ban_nickname_words[chat_id] = set()
                self.ban_nickname_words[chat_id].add(result[0])
                return result[0]
            return None
            
        except sqlite3.Error as e:
            logger.error(f"Error checking banned nickname: {e}")
            return None

    # Ban bio words management
    def add_ban_bio_word(self, chat_id: int, word: str, admin_id: int = None) -> bool:
        """Add a pre-normalized word to bio ban list for a specific chat."""
        try:
            self.get_or_create_chat(chat_id)
            self._execute(
                """
                INSERT OR IGNORE INTO ban_bio_words (chat_id, word, added_by)
                VALUES (?, ?, ?)
                """,
                (chat_id, word, admin_id)
            )
            changes = self._execute("SELECT changes()").fetchone()[0] > 0
            if changes:
                self._log_ban_word_action(chat_id, 'bio', word, 'add', admin_id)
            return changes
        except sqlite3.Error as e:
            logger.error(f"Error adding ban bio word: {e}")
            return False

    def remove_ban_bio_word(self, chat_id: int, word: str, admin_id: int = None) -> bool:
        """Remove a pre-normalized word from bio ban list for a specific chat."""
        try:
            self._execute(
                "DELETE FROM ban_bio_words WHERE chat_id = ? AND word = ?",
                (chat_id, word)
            )
            changes = self._execute("SELECT changes()").fetchone()[0] > 0
            if changes:
                self._log_ban_word_action(chat_id, 'bio', word, 'remove', admin_id)
            return changes
        except sqlite3.Error as e:
            logger.error(f"Error removing ban bio word: {e}")
            return False

    def get_ban_bio_words(self, chat_id: int) -> List[str]:
        """Get all banned bio words for a specific chat."""
        try:
            cursor = self._execute(
                "SELECT word FROM ban_bio_words WHERE chat_id = ? ORDER BY word",
                (chat_id,),
                commit=False
            )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting ban bio words for chat {chat_id}: {e}")
            return []

    def get_user_global_punishment_stats(self, user_id: int) -> Dict[str, int]:
        """Gets the total count of punishments (bans, mutes, warns) for a user across all chats."""
        stats = {'bans': 0, 'mutes': 0, 'warns': 0, 'total': 0}
        try:
            cursor = self._execute(
                """
                SELECT action, COUNT(*) as count
                FROM moderation_logs
                WHERE user_id = ? AND action IN ('ban', 'mute', 'warn')
                GROUP BY action
                """,
                (user_id,),
                commit=False
            )
            for action, count in cursor.fetchall():
                stats[f"{action}s"] = count
            
            stats['total'] = stats['bans'] + stats['mutes'] + stats['warns']
            
        except sqlite3.Error as e:
            logger.error(f"Error getting global punishment stats for user {user_id}: {e}")
        return stats

    def get_user_join_date(self, chat_id: int, user_id: int) -> Optional[str]:
        """Gets the join date (created_at) for a user in a specific chat."""
        try:
            cursor = self._execute(
                "SELECT created_at FROM known_members WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
                commit=False
            )
            result = cursor.fetchone()
            return result[0] if result else None
        except sqlite3.Error as e:
            logger.error(f"Error getting join date for user {user_id} in chat {chat_id}: {e}")
            return None

# Global database instance
db = Database()

# Run data migration on import
try:
    db.migrate_old_data()
except Exception as e:
    logger.error(f"Error during initial data migration: {e}")
    # Continue with empty database if migration fails
