import httpx
import asyncio
import random
from fastapi import HTTPException, status

VK_API_VERSION = "5.199" # Используйте актуальную версию
VK_API_URL = "https://api.vk.com/method/"

async def validate_vk_token(token: str) -> int | None:
    """Проверяет токен VK и возвращает user_id, если валиден."""
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{VK_API_URL}users.get",
                params={"access_token": token, "v": VK_API_VERSION}
            )
            response.raise_for_status()
            data = response.json()
            if "response" in data and data["response"]:
                return data["response"][0]["id"]
            elif "error" in data:
                 print(f"VK API Error (validate_vk_token): {data['error']}")
                 return None
            else:
                 print(f"Unknown VK API response (validate_vk_token): {data}")
                 return None
        except httpx.RequestError as e:
            print(f"HTTP error validating VK token: {e}")
            return None
        except Exception as e:
             print(f"Unexpected error validating VK token: {e}")
             return None


async def send_vk_message(token: str, recipient_id: str, message: str) -> tuple[bool, int | None, str | None]:
    """
    Отправляет сообщение через VK API.
    Возвращает (success: bool, vk_message_id: int | None, error_message: str | None)
    """
    # Генерируем random_id для идемпотентности VK API
    random_id = random.randint(0, 2**31 - 1)
    params = {
        "access_token": token,
        "v": VK_API_VERSION,
        "peer_id": recipient_id,
        "message": message,
        "random_id": random_id,
        "dont_parse_links": 0 # или 1, если не нужно превью ссылок
    }
    async with httpx.AsyncClient(timeout=10.0) as client: # Увеличим таймаут
        try:
            print(f"Sending VK message to {recipient_id}...")
            response = await client.post(f"{VK_API_URL}messages.send", params=params)
            # Логируем ответ VK API для отладки
            print(f"VK messages.send response status: {response.status_code}, content: {response.text[:500]}")

            response.raise_for_status() # Вызовет исключение для 4xx/5xx
            data = response.json()

            if "response" in data:
                message_id = data["response"]
                print(f"VK message sent successfully to {recipient_id}, message_id: {message_id}")
                return True, message_id, None
            elif "error" in data:
                error_info = data["error"]
                error_msg = error_info.get("error_msg", "Unknown VK error")
                error_code = error_info.get("error_code", -1)
                print(f"VK API Error (messages.send) for {recipient_id}: Code {error_code}, Msg: {error_msg}")
                # Особо обрабатываем ошибки токена/авторизации
                if error_code in [5, 7, 10, 15, 113]: # Auth failed, Invalid user, etc.
                    # Можно добавить логику инвалидации токена в БД здесь
                    pass
                return False, None, f"VK Error {error_code}: {error_msg}"
            else:
                print(f"Unknown VK API response (messages.send) for {recipient_id}: {data}")
                return False, None, "Unknown VK API response"

        except httpx.TimeoutException:
             print(f"Timeout error sending VK message to {recipient_id}")
             return False, None, "Timeout sending message to VK"
        except httpx.RequestError as e:
            print(f"HTTP error sending VK message to {recipient_id}: {e}")
            return False, None, f"Network error: {e}"
        except Exception as e:
            print(f"Unexpected error sending VK message to {recipient_id}: {e}")
            return False, None, f"Unexpected error: {e}"