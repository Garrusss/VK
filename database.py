import sqlite3
from sqlalchemy.ext.asyncio import create_async_engine
from contextlib import closing
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./scheduler.db")
# Синхронный URL нужен для инициализации таблицы напрямую через sqlite3
SYNC_DATABASE_PATH = DATABASE_URL.replace("sqlite+aiosqlite:///", "")

# Асинхронный движок для APScheduler и потенциально FastAPI (хотя тут sqlite3)
engine = create_async_engine(DATABASE_URL)

def init_db():
    """Инициализирует таблицу для хранения client_secret и токенов."""
    db_path = SYNC_DATABASE_PATH
    print(f"Initializing database at: {db_path}")
    # Убедимся, что директория существует
    os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            with closing(conn.cursor()) as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        client_secret TEXT UNIQUE NOT NULL,
                        vk_user_id INTEGER UNIQUE NOT NULL,
                        encrypted_vk_token TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # Индексы для ускорения поиска
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_client_secret ON accounts (client_secret)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_vk_user_id ON accounts (vk_user_id)")
                conn.commit()
        print("Database 'accounts' table initialized successfully.")
    except sqlite3.Error as e:
        print(f"Error initializing database: {e}")
        raise

# Вызываем инициализацию при импорте модуля
# init_db() # Лучше вызывать при старте приложения в main_server.py