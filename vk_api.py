import httpx
import asyncio
import random
from fastapi import HTTPException, status
import logging

logger = logging.getLogger(__name__)

VK_API_VERSION = "5.199" # Используйте актуальную версию
VK_API_URL = "https://api.vk.com/method/"

async def validate_vk_token(token: str) -> int | None:
    """
    Проверяет токен VK, обращаясь к users.get.
    Возвращает user_id, если токен валиден, иначе None.
    """
    if not token:
        logger.warning("validate_vk_token called with empty token.")
        return None

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            logger.debug("Validating VK token...")
            response = await client.post(
                f"{VK_API_URL}users.get",
                params={"access_token": token, "v": VK_API_VERSION}
            )
            response.raise_for_status() # Проверка на HTTP ошибки 4xx/5xx
            data = response.json()
            logger.debug(f"VK users.get response: {data}")

            if "response" in data and data["response"] and isinstance(data["response"], list):
                vk_user_id = data["response"][0].get("id")
                if vk_user_id:
                    logger.info(f"VK token validation successful for user_id: {vk_user_id}")
                    return vk_user_id
                else:
                    logger.error("VK token validation error: 'id' not found in response.")
                    return None
            elif "error" in data:
                 error_info = data['error']
                 logger.error(f"VK API Error during token validation: {error_info.get('error_code')} - {error_info.get('error_msg')}")
                 return None
            else:
                 logger.error(f"Unknown VK API response structure during token validation: {data}")
                 return None
        except httpx.TimeoutException:
            logger.error("Timeout error validating VK token.")
            return None
        except httpx.RequestError as e:
            logger.error(f"HTTP error validating VK token: {e}", exc_info=True)
            return None
        except Exception as e:
             logger.error(f"Unexpected error validating VK token: {e}", exc_info=True)
             return None


async def send_vk_message(token: str, recipient_id: str, message: str, job_id: str | None = None) -> tuple[bool, int | None, str | None]:
    """
    Отправляет сообщение через VK API.
    Возвращает (success: bool, vk_message_id: int | None, error_message: str | None)
    """
    log_prefix = f"[Job {job_id}] " if job_id else ""
    if not token or not recipient_id or not message:
        logger.error(f"{log_prefix}send_vk_message called with invalid parameters (empty token, recipient, or message).")
        return False, None, "Internal error: Invalid parameters for sending message."

    # Генерируем random_id для идемпотентности VK API
    random_id = random.randint(0, 2**31 - 1)
    params = {
        "access_token": token,
        "v": VK_API_VERSION,
        "peer_id": recipient_id,
        "message": message,
        "random_id": random_id,
        "dont_parse_links": 0 # 0 - создавать превью ссылок
    }
    logger.info(f"{log_prefix}Attempting to send VK message to peer_id: {recipient_id} (random_id: {random_id})")

    async with httpx.AsyncClient(timeout=15.0) as client: # Увеличим таймаут для отправки
        try:
            response = await client.post(f"{VK_API_URL}messages.send", params=params)
            # Логируем ответ VK API для отладки (первые 500 символов)
            logger.debug(f"{log_prefix}VK messages.send response status: {response.status_code}, content: {response.text[:500]}")

            response.raise_for_status() # Вызовет исключение для 4xx/5xx
            data = response.json()

            if "response" in data:
                # Ответ может быть числом (message_id) или объектом для бесед
                message_id = data["response"]
                logger.info(f"{log_prefix}VK message sent successfully to peer_id: {recipient_id}. VK Response: {message_id}")
                # Вернем сам message_id как число, если это возможно
                numeric_message_id = message_id if isinstance(message_id, int) else None
                return True, numeric_message_id, None
            elif "error" in data:
                error_info = data["error"]
                error_msg = error_info.get("error_msg", "Unknown VK error")
                error_code = error_info.get("error_code", -1)
                logger.error(f"{log_prefix}VK API Error (messages.send) for peer_id {recipient_id}: Code {error_code}, Msg: {error_msg}")
                # Особо обрабатываем ошибки токена/авторизации
                # Коды ошибок взяты из документации VK API: https://dev.vk.com/reference/errors
                if error_code in [5, 7, 10, 15, 17, 113, 28]: # User authorization failed, permission denied, internal server error (captcha?), invalid user id, app needs confirmation
                    # Можно добавить логику инвалидации токена в БД здесь
                    # Например, пометить аккаунт как требующий перепривязки
                    logger.warning(f"{log_prefix}Authorization or permission error encountered (Code: {error_code}). Token might be invalid or require action.")
                    pass
                return False, None, f"VK Error {error_code}: {error_msg}"
            else:
                logger.error(f"{log_prefix}Unknown VK API response structure (messages.send) for peer_id {recipient_id}: {data}")
                return False, None, "Unknown VK API response"

        except httpx.TimeoutException:
             logger.error(f"{log_prefix}Timeout error sending VK message to peer_id {recipient_id}")
             return False, None, "Timeout sending message to VK"
        except httpx.RequestError as e:
            logger.error(f"{log_prefix}HTTP error sending VK message to peer_id {recipient_id}: {e}", exc_info=True)
            return False, None, f"Network error: {e}"
        except Exception as e:
            logger.error(f"{log_prefix}Unexpected error sending VK message to peer_id {recipient_id}: {e}", exc_info=True)
            return False, None, f"Unexpected error: {e}"
