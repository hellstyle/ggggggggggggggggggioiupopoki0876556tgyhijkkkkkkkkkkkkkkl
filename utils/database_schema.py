import sqlite3
import os
from pathlib import Path
import logging
from typing import List, Dict, Tuple, Any, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class DatabaseSchema:
    def __init__(self, db_path: str = 'bot_database1.db'):
        """Initialize the database connection and create tables if they don't exist."""
        # Allow overriding DB location via env (useful for Railway volumes)
        env_db_path = os.getenv('DB_PATH')
        self.db_path = Path(env_db_path) if env_db_path else Path(db_path)
        self.conn = None
        self._initialize_database()
        
    def _initialize_database(self):
        """Create database tables if they don't exist."""
        try:
            self.conn = sqlite3.connect(self.db_path)
            cursor = self.conn.cursor()
            
            # Create ban_words table (now with chat_id)
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS ban_words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                added_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, word)
            )
            ''')
            
            # First, check if ban_nickname_words exists and needs migration
            cursor.execute("PRAGMA table_info(ban_nickname_words)")
            columns = [col[1] for col in cursor.fetchall()]
            
            if not columns:  # Table doesn't exist, create new
                cursor.execute('''
                CREATE TABLE ban_nickname_words (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL DEFAULT 0,
                    word TEXT NOT NULL,
                    added_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, word)
                )
                ''')
            elif 'chat_id' not in columns:  # Table exists but needs migration
                logger.info("Migrating 'ban_nickname_words' table to add 'chat_id' column.")
                # 1. Переименовываем старую таблицу для бэкапа на время миграции
                cursor.execute('ALTER TABLE ban_nickname_words RENAME TO ban_nickname_words_old')

                # 2. Создаем новую таблицу с правильной схемой
                cursor.execute('''
                CREATE TABLE ban_nickname_words (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL DEFAULT 0,
                    word TEXT NOT NULL,
                    added_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, word)
                )''')

                # 3. Копируем данные из старой таблицы в новую
                cursor.execute('''
                INSERT OR IGNORE INTO ban_nickname_words (word, created_at)
                SELECT word, COALESCE(created_at, CURRENT_TIMESTAMP) 
                FROM ban_nickname_words_old
                ''')

                # 4. Удаляем старую таблицу после успешного переноса данных
                cursor.execute('DROP TABLE ban_nickname_words_old')
                logger.info("'ban_nickname_words' table migrated successfully.")

            # Create ban_bio_words table
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS ban_bio_words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                added_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, word)
            )
            ''')

            # Create ban_logs table for word operations
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS ban_word_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                word_type TEXT NOT NULL,  -- 'word' or 'nickname'
                word TEXT NOT NULL,
                action TEXT NOT NULL,     -- 'add' or 'remove'
                admin_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            # Create known_members table to track members the bot has seen
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS known_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_member BOOLEAN DEFAULT 1,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, user_id)
            )
            ''')
            
            # Create ban_patterns table
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS ban_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL UNIQUE,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            # Create banned_users table
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                reason TEXT,
                admin_id INTEGER,
                banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                unbanned_at TIMESTAMP NULL,
                is_active BOOLEAN DEFAULT 1
            )
            ''')
            
            # Create ban_logs table for history
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS ban_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL,  -- 'ban' or 'unban'
                reason TEXT,
                admin_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            # Create chat_settings table
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
            # Add delete_links_enabled column to chat_settings if it doesn't exist
            cursor.execute("PRAGMA table_info(chat_settings)")
            chat_settings_columns = [col[1] for col in cursor.fetchall()]
            if 'delete_links_enabled' not in chat_settings_columns:
                cursor.execute('ALTER TABLE chat_settings ADD COLUMN delete_links_enabled BOOLEAN DEFAULT 0')
            
            if 'welcome_captcha_enabled' not in chat_settings_columns:
                cursor.execute('ALTER TABLE chat_settings ADD COLUMN welcome_captcha_enabled BOOLEAN DEFAULT 0')
            
            # Create bannable_link_domains table
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS bannable_link_domains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                domain TEXT NOT NULL,
                added_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, domain)
            )
            ''')

            # Create chat_admins table
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                added_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, user_id)
            )
            ''')

            # Create whitelisted_users table
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS whitelisted_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                added_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, user_id)
            )
            ''')

            # Create moderation_logs table for all actions
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS moderation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                reason TEXT,
                duration_seconds INTEGER,
                admin_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')

            # Create banned_avatars table
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS banned_avatars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_unique_id TEXT NOT NULL UNIQUE,
                phash TEXT,
                added_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')

            # Add phash column to banned_avatars if it doesn't exist
            cursor.execute("PRAGMA table_info(banned_avatars)")
            banned_avatars_columns = [col[1] for col in cursor.fetchall()]
            if 'phash' not in banned_avatars_columns:
                cursor.execute('ALTER TABLE banned_avatars ADD COLUMN phash TEXT')
            if 'file_id' not in banned_avatars_columns:
                cursor.execute('ALTER TABLE banned_avatars ADD COLUMN file_id TEXT')

            # Create new triggers table with chat_id
            # Migrate triggers to include chat_id ONLY if old schema lacks chat_id
            cursor.execute("PRAGMA table_info(triggers)")
            trig_columns = [col[1] for col in cursor.fetchall()]
            if 'chat_id' not in trig_columns:
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS triggers_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL DEFAULT 0,
                    trigger TEXT NOT NULL,
                    response TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, trigger)
                )''')
                # Migrate existing data (assign default chat_id = 0)
                cursor.execute('''
                INSERT OR IGNORE INTO triggers_new (id, trigger, response, created_at)
                SELECT id, trigger, response, created_at FROM triggers
                ''')
                # Replace old table
                cursor.execute('DROP TABLE IF EXISTS triggers_old')
                cursor.execute('ALTER TABLE triggers RENAME TO triggers_old')
                cursor.execute('ALTER TABLE triggers_new RENAME TO triggers')
            
            # Migrate ban_words to include chat_id ONLY if old schema lacks chat_id
            cursor.execute("PRAGMA table_info(ban_words)")
            bw_columns = [col[1] for col in cursor.fetchall()]
            if 'chat_id' not in bw_columns:
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS ban_words_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL DEFAULT 0,
                    word TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, word)
                )''')
                # Migrate existing data (assign default chat_id = 0)
                cursor.execute('''
                INSERT OR IGNORE INTO ban_words_new (word, created_at)
                SELECT word, created_at FROM ban_words
                ''')
                # Replace old table
                cursor.execute('DROP TABLE IF EXISTS ban_words_old')
                cursor.execute('ALTER TABLE ban_words RENAME TO ban_words_old')
                cursor.execute('ALTER TABLE ban_words_new RENAME TO ban_words')
            
            # Create indexes
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_triggers_chat_id ON triggers(chat_id)')
            
            # Create indexes for better performance
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ban_words_chat_word ON ban_words(chat_id, word)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ban_nickname_words_chat_word ON ban_nickname_words(chat_id, word)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ban_bio_words_chat_word ON ban_bio_words(chat_id, word)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ban_word_logs_chat ON ban_word_logs(chat_id, word_type, word)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ban_word_logs_created ON ban_word_logs(created_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_banned_users_user_id ON banned_users(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_banned_users_is_active ON banned_users(is_active)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_triggers_trigger ON triggers(trigger)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_known_members_chat_user ON known_members(chat_id, user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_bannable_domains_chat_domain ON bannable_link_domains(chat_id, domain)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_chat_admins_chat_user ON chat_admins(chat_id, user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_whitelisted_users_chat_user ON whitelisted_users(chat_id, user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_moderation_logs_action_time ON moderation_logs(action, created_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_banned_avatars_unique_id ON banned_avatars(file_unique_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_known_members_last_seen ON known_members(last_seen)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_banned_avatars_phash ON banned_avatars(phash)')
            
            self.conn.commit()
            logger.info("Database schema initialized successfully")
            
        except sqlite3.Error as e:
            logger.error(f"Error initializing database: {e}")
            if self.conn:
                self.conn.rollback()
            raise
    
    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None
    
    def __del__(self):
        """Ensure the database connection is closed when the object is destroyed."""
        self.close()

# Create a global instance of the database schema
db_schema = DatabaseSchema()
