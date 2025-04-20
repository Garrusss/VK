# database.py
import sqlite3
from sqlalchemy.ext.asyncio import create_async_engine
from contextlib import closing
import os
from dotenv import load_dotenv
from typing import Optional, Tuple
import asyncio # Импортируем asyncio для to_thread

# Импортируем функции шифрования/дешифрования (предполагается, что они есть в security.py)
# Замените на ваш реальный импорт, если он другой
try:
    from security import decrypt_token
except ImportError:
    # Заглушка, если security.py не найден или функция называется иначе
    def decrypt_token(encrypted_token: str) -> str:
        print("Warning: Using STUB decrypt_token function!")
        # В реальном коде здесь должна быть ваша логика дешифрования
        # Эта заглушка просто возвращает то, что получила (НЕБЕЗОПАСНО)
        return encrypted_token

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./scheduler.db")
# Синхронный URL нужен для инициализации таблицы и прямых запросов через sqlite3
SYNC_DATABASE_PATH = DATABASE_URL.replace("sqlite+aiosqlite:///", "")

# Асинхронный движок для APScheduler и потенциально FastAPI (хотя тут sqlite3)
# engine = create_async_engine(DATABASE_URL) # Пока не используется напрямую в этом файле

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

# --- ДОБАВЛЕНЫ ФУНКЦИИ ДЛЯ ПОЛУЧЕНИЯ ДАННЫХ ---

async def get_account_id_by_secret(client_secret: str) -> Optional[int]:
    """
    Асинхронно получает ID аккаунта (primary key) по client_secret.
    Возвращает ID или None, если секрет не найден.
    Использует синхронный вызов в потоке.
    """
    if not client_secret:
        return None

    def db_query():
        try:
            with closing(sqlite3.connect(SYNC_DATABASE_PATH, timeout=10)) as conn: # Добавлен таймаут
                with closing(conn.cursor()) as cursor:
                    cursor.execute("SELECT id FROM accounts WHERE client_secret = ?", (client_secret,))
                    result = cursor.fetchone()
                    return result[0] if result else None
        except sqlite3.Error as e:
            print(f"Database error in get_account_id_by_secret: {e}")
            return None # Возвращаем None при ошибке БД

    account_id = await asyncio.to_thread(db_query)
    return account_id


async def get_decrypted_token_by_secret(client_secret: str) -> Optional[str]:
    """
    Асинхронно получает ЗАШИФРОВАННЫЙ токен по client_secret,
    ДЕШИФРУЕТ его и возвращает.
    Возвращает расшифрованный токен или None, если секрет не найден или ошибка дешифровки.
    Использует синхронный вызов в потоке для доступа к БД.
    """
    if not client_secret:
        return None

    def db_query_and_decrypt():
        encrypted_token: Optional[str] = None
        try:
            with closing(sqlite3.connect(SYNC_DATABASE_PATH, timeout=10)) as conn:
                with closing(conn.cursor()) as cursor:
                    cursor.execute("SELECT encrypted_vk_token FROM accounts WHERE client_secret = ?", (client_secret,))
                    result = cursor.fetchone()
                    if result:
                        encrypted_token = result[0]
                    else:
                        return None # Секрет не найден
        except sqlite3.Error as e:
            print(f"Database error fetching token for secret ending ...{client_secret[-4:]}: {e}")
            return None # Ошибка БД

        if encrypted_token:
            try:
                # Дешифровка происходит здесь, после получения из БД
                decrypted = decrypt_token(encrypted_token)
                return decrypted
            except Exception as e:
                # Ловим возможные ошибки дешифровки
                print(f"Error decrypting token for secret ending ...{client_secret[-4:]}: {e}")
                return None
        else:
            # Это не должно произойти, если fetchone вернул результат, но на всякий случай
            return None

    # Запускаем синхронную функцию БД + дешифровки в отдельном потоке
    decrypted_token = await asyncio.to_thread(db_query_and_decrypt)
    return decrypted_token

# Вызываем инициализацию при старте приложения через lifespan в main_server.py
# init_db()
