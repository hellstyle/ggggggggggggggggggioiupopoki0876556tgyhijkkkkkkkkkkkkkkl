import sqlite3
import os
from pathlib import Path
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def migrate_database(db_path: str = 'bot_database1.db') -> None:
    """
    Migrate the database to the latest schema.
    
    Args:
        db_path: Path to the SQLite database file
    """
    db_path = Path(db_path)
    backup_path = db_path.with_suffix('.bak' + db_path.suffix)
    
    try:
        # Connect to the database
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        
        logger.info("Starting database migration...")
        
        # Check if ban_nickname_words table exists and needs migration
        cursor.execute("PRAGMA table_info(ban_nickname_words)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'chat_id' not in columns:
            logger.info("Migrating ban_nickname_words table...")
            
            # Create backup of the database
            logger.info("Creating database backup...")
            if backup_path.exists():
                backup_path.unlink()
            db_path.rename(backup_path)
            
            # Reconnect to the new database
            conn.close()
            backup_path.rename(db_path)
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            
            # Create new table with chat_id
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS ban_nickname_words_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL DEFAULT 0,
                word TEXT NOT NULL,
                added_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, word)
            )''')
            
            # Migrate existing data
            cursor.execute('''
            INSERT OR IGNORE INTO ban_nickname_words_new (word, added_by, created_at)
            SELECT word, NULL, COALESCE(created_at, CURRENT_TIMESTAMP) 
            FROM ban_nickname_words
            ''')
            
            # Drop old table and rename new one
            cursor.execute('DROP TABLE IF EXISTS ban_nickname_words_old')
            cursor.execute('ALTER TABLE ban_nickname_words RENAME TO ban_nickname_words_old')
            cursor.execute('ALTER TABLE ban_nickname_words_new RENAME TO ban_nickname_words')
            
            # Create indexes
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ban_nickname_words_chat_word ON ban_nickname_words(chat_id, word)')
            
            logger.info("Successfully migrated ban_nickname_words table")
        
        # Commit changes
        conn.commit()
        logger.info("Database migration completed successfully")
        
    except Exception as e:
        logger.error(f"Error during migration: {e}")
        if 'conn' in locals() and conn:
            conn.rollback()
        raise
    finally:
        if 'conn' in locals() and conn:
            conn.close()

if __name__ == "__main__":
    migrate_database()
