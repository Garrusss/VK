import os
import secrets
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
import sqlite3
from contextlib import closing
import logging

from database import SYNC_DATABASE_PATH # Импортируем абсолютным путем

logger = logging.getLogger(__name__)
load_dotenv()

# Загружаем ключ шифрования (КРАЙНЕ ВАЖНО ХРАНИТЬ ЕГО БЕЗОПАСНО!)
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    logger.critical("CRITICAL: ENCRYPTION_KEY not found in .env file! Application cannot run securely.")
    raise ValueError("ENCRYPTION_KEY не найдена в .env файле!")

try:
    # Проверяем, что ключ валидный для Fernet
    fernet = Fernet(ENCRYPTION_KEY.encode())
    logger.info("Encryption key loaded successfully.")
except (ValueError, TypeError) as e:
     logger.critical(f"CRITICAL: Invalid ENCRYPTION_KEY format: {e}. Please generate a valid Fernet key.")
     raise ValueError("Некорректный формат ENCRYPTION_KEY.")

# --- Шифрование ---
def encrypt_token(token: str) -> str:
    """Шифрует токен."""
    if not token:
        raise ValueError("Cannot encrypt an empty token")
    try:
        return fernet.encrypt(token.encode()).decode()
    except Exception as e:
        logger.error(f"Error encrypting token: {e}", exc_info=True)
        raise # Перебрасываем исключение, т.к. это критично

def decrypt_token(encrypted_token: str) -> str | None:
    """Расшифровывает токен. Возвращает None в случае ошибки."""
    if not encrypted_token:
        logger.warning("Attempted to decrypt an empty token string.")
        return None
    try:
        return fernet.decrypt(encrypted_token.encode()).decode()
    except InvalidToken:
        logger.error("Error decrypting token: InvalidToken. The token may be corrupted or the key might have changed.")
        return None
    except Exception as e:
        logger.error(f"Error decrypting token: {e}", exc_info=True)
        return None

# --- Генерация секрета ---
def generate_client_secret() -> str:
    """Генерирует безопасный client_secret."""
    return secrets.token_urlsafe(32) # 32 байта = 43 символа в base64

# --- Аутентификация по заголовку ---
# Ожидаем заголовок типа "Authorization: Secret <your_client_secret>"
SECRET_HEADER_NAME = "Authorization"
SECRET_SCHEME = "Secret"
secret_header = APIKeyHeader(name=SECRET_HEADER_NAME, scheme_name=SECRET_SCHEME, auto_error=False)

async def get_account_id_from_secret(header: str | None = Depends(secret_header)) -> int:
    """
    Зависимость FastAPI для проверки client_secret и получения account_id.
    Использует синхронный доступ к БД SQLite.
    """
    if header is None:
        logger.warning("Authentication failed: Authorization header missing.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated: Authorization header missing",
        )

    # Проверяем схему и извлекаем секрет
    parts = header.split()
    if len(parts) != 2 or parts[0] != SECRET_SCHEME:
        logger.warning(f"Authentication failed: Invalid scheme or format in header: {header}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication scheme. Expected 'Secret <token>'",
        )
    client_secret = parts[1]

    if not client_secret:
         logger.warning("Authentication failed: Client secret is empty.")
         raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid client secret",
        )

    # Ищем секрет в БД (синхронно)
    account_id = None
    try:
        # Используем with closing для гарантии закрытия соединения и курсора
        with closing(sqlite3.connect(SYNC_DATABASE_PATH, timeout=5)) as conn: # Добавим таймаут
            with closing(conn.cursor()) as cursor:
                cursor.execute("SELECT id FROM accounts WHERE client_secret = ?", (client_secret,))
                result = cursor.fetchone()
                if result:
                    account_id = result[0]
                    # logger.debug(f"Authentication successful for client_secret ending in ...{client_secret[-6:]}, account_id: {account_id}")
                # else:
                    # logger.warning(f"Authentication failed: Client secret ending in ...{client_secret[-6:]} not found.")

    except sqlite3.Error as e:
        logger.error(f"Database error during authentication for secret ending ...{client_secret[-6:]}: {e}", exc_info=True)
        # Не раскрываем детали БД клиенту
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Authentication database error")

    if account_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid client secret",
        )
    return account_id

def get_decrypted_vk_token(account_id: int) -> str | None:
    """Получает и расшифровывает VK токен для заданного account_id (синхронно)."""
    if not isinstance(account_id, int) or account_id <= 0:
        logger.warning(f"Attempted to get token for invalid account_id: {account_id}")
        return None

    encrypted_token = None
    try:
        with closing(sqlite3.connect(SYNC_DATABASE_PATH, timeout=5)) as conn:
            with closing(conn.cursor()) as cursor:
                cursor.execute("SELECT encrypted_vk_token FROM accounts WHERE id = ?", (account_id,))
                result = cursor.fetchone()
                if result:
                    encrypted_token = result[0]
    except sqlite3.Error as e:
        logger.error(f"Database error getting encrypted token for account_id {account_id}: {e}", exc_info=True)
        return None # Не можем продолжить без токена

    if not encrypted_token:
        logger.error(f"CRITICAL: No encrypted token found in DB for account_id: {account_id}. Account data might be inconsistent.")
        return None

    decrypted = decrypt_token(encrypted_token)
    if not decrypted:
         logger.error(f"CRITICAL: Failed to decrypt token for account_id: {account_id}. Check encryption key and token integrity.")
         # Возможно, стоит уведомить администратора или пометить аккаунт
    # else:
         # logger.debug(f"Successfully decrypted token for account_id: {account_id}")

    return decrypted
