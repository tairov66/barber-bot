import sqlite3

conn = sqlite3.connect("barber_bot.db", check_same_thread=False)
cursor = conn.cursor()

def init_db():
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT
        )
    """)
    conn.commit()

def add_user(user_id, username, full_name):
    cursor.execute("""
        INSERT OR IGNORE INTO users (user_id, username, full_name) 
        VALUES (?, ?, ?)
    """, (user_id, username, full_name))
    conn.commit()

init_db()
