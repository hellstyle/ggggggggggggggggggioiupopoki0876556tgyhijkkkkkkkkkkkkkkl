import sqlite3

def check_schema():
    try:
        conn = sqlite3.connect('bot_database1.db')
        cursor = conn.cursor()
        
        # Check if ban_nickname_words table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ban_nickname_words'")
        table_exists = cursor.fetchone() is not None
        
        if table_exists:
            print("Table 'ban_nickname_words' exists!")
            # Get table info
            cursor.execute("PRAGMA table_info(ban_nickname_words)")
            columns = cursor.fetchall()
            print("\nTable columns:")
            for col in columns:
                print(f"- {col[1]} ({col[2]})")
        else:
            print("Table 'ban_nickname_words' does not exist!")
            
        conn.close()
        
    except Exception as e:
        print(f"Error checking schema: {e}")

if __name__ == "__main__":
    check_schema()
