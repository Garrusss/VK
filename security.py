import os
import secrets
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
import sqlite3
from contextlib import closing
from .database import SYNC_DATABASE_PATH # Путь к БД

load_dotenv()

# Загружаем ключ шифрования (КРАЙНЕ ВАЖНО ХРАНИТЬ ЕГО БЕЗОПАСНО!)
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    raise ValueError("ENCRYPTION_KEY не найдена в .env файле!")
try:
    fernet = Fernet(ENCRYPTION_KEY.encode())
except ValueError:
     raise ValueError("Некорректный формат ENCRYPTION_KEY.")

# --- Шифрование ---
def encrypt_token(token: str) -> str:
    """Шифрует токен."""
    return fernet.encrypt(token.encode()).decode()

def decrypt_token(encrypted_token: str) -> str | None:
    """Расшифровывает токен."""
    try:
        return fernet.decrypt(encrypted_token.encode()).decode()
    except InvalidToken:
        print("Error decrypting token: InvalidToken")
        return None
    except Exception as e:
        print(f"Error decrypting token: {e}")
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
    Использует синхронный доступ к БД, т.к. FastAPI не поддерживает async Depends с DB Pool легко.
    Для продакшена лучше использовать async-совместимый пул или ORM.
    """
    if header is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated: Authorization header missing",
        )

    # Проверяем схему и извлекаем секрет
    parts = header.split()
    if len(parts) != 2 or parts[0] != SECRET_SCHEME:
         raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication scheme",
        )
    client_secret = parts[1]

    # Ищем секрет в БД (синхронно)
    account_id = None
    try:
        with closing(sqlite3.connect(SYNC_DATABASE_PATH)) as conn:
            with closing(conn.cursor()) as cursor:
                cursor.execute("SELECT id FROM accounts WHERE client_secret = ?", (client_secret,))
                result = cursor.fetchone()
                if result:
                    account_id = result[0]
    except sqlite3.Error as e:
        print(f"Database error during authentication: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database error")

    if account_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid client secret",
        )
    return account_id

def get_decrypted_vk_token(account_id: int) -> str | None:
    """Получает и расшифровывает VK токен для заданного account_id (синхронно)."""
    encrypted_token = None
    try:
        with closing(sqlite3.connect(SYNC_DATABASE_PATH)) as conn:
            with closing(conn.cursor()) as cursor:
                cursor.execute("SELECT encrypted_vk_token FROM accounts WHERE id = ?", (account_id,))
                result = cursor.fetchone()
                if result:
                    encrypted_token = result[0]
    except sqlite3.Error as e:
        print(f"Database error getting encrypted token: {e}")
        return None # Не можем продолжить без токена

    if not encrypted_token:
        print(f"No encrypted token found for account_id: {account_id}")
        return None

    decrypted = decrypt_token(encrypted_token)
    if not decrypted:
         print(f"Failed to decrypt token for account_id: {account_id}")
         # Возможно, стоит удалить невалидный токен или пометить аккаунт?
    return decrypted