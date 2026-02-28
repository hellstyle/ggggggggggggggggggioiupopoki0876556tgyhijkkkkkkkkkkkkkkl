import time
import logging
from pathlib import Path
from config import BACKUP_DIR, BACKUP_RETENTION_DAYS

logger = logging.getLogger(__name__)

def cleanup_old_backups():
    """
    Deletes backup files older than BACKUP_RETENTION_DAYS.
    """
    if not BACKUP_DIR.exists():
        logger.info("Backup directory does not exist. Nothing to clean up.")
        return

    now = time.time()
    retention_period_seconds = BACKUP_RETENTION_DAYS * 24 * 60 * 60
    
    logger.info(f"Running cleanup of backups older than {BACKUP_RETENTION_DAYS} days in {BACKUP_DIR}...")
    
    deleted_count = 0
    for filepath in BACKUP_DIR.iterdir():
        if filepath.is_file() and filepath.stat().st_mtime < (now - retention_period_seconds):
            try:
                filepath.unlink()
                logger.info(f"Deleted old backup: {filepath.name}")
                deleted_count += 1
            except Exception as e:
                logger.error(f"Error deleting backup file {filepath}: {e}")

    logger.info(f"Cleanup complete. Deleted {deleted_count} old backup(s).")