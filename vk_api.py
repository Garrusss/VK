# vk_api.py
import httpx
import asyncio
import random
from fastapi import HTTPException, status
import logging
from typing import List, Dict, Any, Tuple, Optional # Добавлены типы

# Импортируем модель из models.py (убедитесь, что путь правильный)
try:
    from models import ConversationItem
except ImportError:
    # Заглушка на случай проблем с импортом
    class ConversationItem:
        def __init__(self, peer_id: int, title: str):
            self.peer_id = peer_id
            self.title = title

logger = logging.getLogger(__name__)

VK_API_VERSION = "5.199" # Используйте актуальную версию
VK_API_URL = "https://api.vk.com/method/"

async def validate_vk_token(token: str) -> Optional[int]:
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


async def send_vk_message(token: str, recipient_id: str, message: str, job_id: Optional[str] = None) -> Tuple[bool, Optional[int], Optional[str]]:
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

            # Не всегда VK возвращает ошибку с HTTP статусом > 400, иногда ошибка в теле JSON
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
                    logger.warning(f"{log_prefix}Authorization or permission error encountered (Code: {error_code}). Token might be invalid or require action.")
                    pass
                return False, None, f"VK Error {error_code}: {error_msg}"
            else:
                 # Проверяем HTTP статус, если нет ни 'response', ни 'error'
                 response.raise_for_status() # Вызовет исключение для 4xx/5xx, если они есть
                 # Если статус < 400, но формат ответа неизвестен
                 logger.error(f"{log_prefix}Unknown VK API response structure (messages.send) for peer_id {recipient_id}: {data}")
                 return False, None, "Unknown VK API response"

        except httpx.TimeoutException:
             logger.error(f"{log_prefix}Timeout error sending VK message to peer_id {recipient_id}")
             return False, None, "Timeout sending message to VK"
        except httpx.RequestError as e:
            # Обрабатываем ошибки HTTP (включая те, что вызвал raise_for_status)
            # Логируем тело ответа, если оно есть, для диагностики
            response_text = e.response.text[:500] if hasattr(e, 'response') and e.response else "N/A"
            logger.error(f"{log_prefix}HTTP error sending VK message to peer_id {recipient_id}: {e}. Response: {response_text}", exc_info=False) # exc_info=False, т.к. само 'e' содержит инфо
            return False, None, f"Network/HTTP error: {e}"
        except Exception as e:
            logger.error(f"{log_prefix}Unexpected error sending VK message to peer_id {recipient_id}: {e}", exc_info=True)
            return False, None, f"Unexpected error: {e}"


# --- ДОБАВЛЕННАЯ ФУНКЦИЯ ---
async def fetch_conversations(token: str, offset: int, count: int) -> Optional[Tuple[List[Dict[str, Any]], int]]:
    """
    Получает список диалогов пользователя из VK API с пагинацией.
    Возвращает кортеж (список диалогов [словари], общее количество диалогов)
    или None при ошибке (сетевой, API VK, или невалидный токен).
    Формат словаря в списке соответствует модели models.ConversationItem.
    """
    if not token:
        logger.warning("fetch_conversations called with empty token.")
        return None

    params = {
        'access_token': token,
        'v': VK_API_VERSION,
        'offset': offset,
        'count': count,
        'extended': 0, # 0 - не возвращать профили/группы, только основную информацию о беседе
        'filter': 'all' # Получать все типы диалогов (личные, чаты, сообщества)
    }
    url = f"{VK_API_URL}messages.getConversations"
    logger.info(f"Fetching VK conversations: offset={offset}, count={count}")

    async with httpx.AsyncClient(timeout=15.0) as client: # Немного увеличим таймаут
        try:
            response = await client.get(url, params=params)
            logger.debug(f"VK messages.getConversations response status: {response.status_code}, content: {response.text[:500]}")
            data = response.json()

            if 'response' in data:
                vk_response = data['response']
                items = vk_response.get('items', [])
                total_count = vk_response.get('count', 0)

                # Форматируем результат, чтобы он соответствовал ожиданиям клиента (модели ConversationItem)
                formatted_items: List[Dict[str, Any]] = []
                for item in items:
                    conversation = item.get('conversation')
                    if not conversation: continue # Пропускаем, если нет объекта conversation

                    peer = conversation.get('peer')
                    if not peer: continue # Пропускаем, если нет объекта peer

                    peer_id = peer.get('id')
                    if peer_id is None: continue # Пропускаем, если нет peer_id

                    # Получаем title из chat_settings, если это чат
                    chat_settings = conversation.get('chat_settings')
                    title = chat_settings.get('title', f"Диалог ID {peer_id}") if chat_settings else f"Диалог ID {peer_id}"

                    # TODO: Для личных диалогов (peer.type == 'user') можно было бы получить имя пользователя,
                    # сделав 'extended': 1 и парся поле 'profiles'. Но для простоты пока используем ID.
                    # Для сообществ (peer.type == 'group') аналогично с полем 'groups'.

                    formatted_items.append({
                        "peer_id": peer_id,
                        "title": title
                    })

                logger.info(f"Successfully fetched {len(formatted_items)} conversations (offset={offset}, count={count}). Total reported by VK: {total_count}.")
                return formatted_items, total_count

            elif 'error' in data:
                error_info = data['error']
                error_msg = error_info.get('error_msg', 'Unknown VK error')
                error_code = error_info.get('error_code', -1)
                logger.error(f"VK API Error (messages.getConversations): Code {error_code}, Msg: {error_msg}")
                 # Проверяем специфичные ошибки токена или доступа
                if error_code in [5, 7, 10, 15, 17, 113, 28]:
                     logger.warning(f"Authorization or permission error (Code: {error_code}) fetching conversations. Token may be invalid.")
                     # Здесь НЕ возвращаем None сразу, позволяем main_server вернуть 400 Bad Request,
                     # т.к. ошибка произошла на уровне VK API, а не аутентификации нашего сервера.
                     # Если бы код 5 (User authorization failed) был 100% индикатором невалидности НАШЕГО секрета,
                     # то можно было бы вернуть None, чтобы main_server выдал 401.
                return None # Возвращаем None при любой ошибке VK API

            else:
                 # Проверяем HTTP статус, если нет ни 'response', ни 'error'
                 response.raise_for_status()
                 logger.error(f"Unknown VK API response structure (messages.getConversations): {data}")
                 return None

        except httpx.TimeoutException:
             logger.error(f"Timeout error fetching VK conversations.")
             return None
        except httpx.RequestError as e:
             response_text = e.response.text[:500] if hasattr(e, 'response') and e.response else "N/A"
             logger.error(f"HTTP error fetching VK conversations: {e}. Response: {response_text}", exc_info=False)
             return None
        except Exception as e:
            logger.error(f"Unexpected error fetching VK conversations: {e}", exc_info=True)
            return None
