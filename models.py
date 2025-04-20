from pydantic import BaseModel, Field, field_validator, validator, ConfigDict
from datetime import datetime, timezone
import re

# Используем ConfigDict для Pydantic v2
PYDANTIC_V2 = hasattr(BaseModel, 'model_validate')

# --- Модели Запросов ---

class LinkAccountRequest(BaseModel):
    vk_access_token: str = Field(..., min_length=10, description="VK Access Token")

    if PYDANTIC_V2:
         model_config = ConfigDict(extra='forbid') # Запретить лишние поля

class ScheduleRequest(BaseModel):
    recipient_id: str = Field(..., description="VK User ID, Peer ID (e.g., 2000000001), or negative Group ID")
    message: str = Field(..., min_length=1, max_length=4096) # Ограничим длину сообщения
    scheduled_at: str = Field(..., description="ISO 8601 timestamp with timezone (e.g., YYYY-MM-DDTHH:MM:SS.sssZ or YYYY-MM-DDTHH:MM:SS.sss+HH:MM)")

    if PYDANTIC_V2:
         model_config = ConfigDict(extra='forbid')

         @field_validator('recipient_id')
         @classmethod
         def check_recipient_id(cls, v):
             # Простая проверка, что это число (возможно, отрицательное)
             if not re.fullmatch(r"-?\d+", v):
                 raise ValueError('recipient_id must be a valid integer string')
             # Дополнительно можно проверить на максимальное/минимальное значение peer_id VK
             return v

         @field_validator('scheduled_at')
         @classmethod
         def check_datetime_format(cls, v):
             try:
                 # Попытка парсинга для валидации формата и наличия таймзоны
                 dt = datetime.fromisoformat(v.replace('Z', '+00:00'))
                 if dt.tzinfo is None:
                      raise ValueError("scheduled_at must be timezone-aware")
                 # Проверка, что время не слишком далеко в прошлом (например, не более 5 минут)
                 # Это может быть не нужно, т.к. в main_server проверяем, что время в будущем
                 # if dt < datetime.now(timezone.utc) - timedelta(minutes=5):
                 #      raise ValueError("Scheduled time is too far in the past")
             except ValueError as e:
                 raise ValueError(f"Invalid ISO 8601 format or value for scheduled_at: {e}")
             return v
    else: # Pydantic v1 style validators
        @validator('recipient_id')
        def check_recipient_id_v1(cls, v):
             if not re.fullmatch(r"-?\d+", v): raise ValueError('recipient_id must be valid integer string')
             return v
        @validator('scheduled_at')
        def check_datetime_format_v1(cls, v):
            try:
                dt = datetime.fromisoformat(v.replace('Z', '+00:00'))
                if dt.tzinfo is None: raise ValueError("scheduled_at must be timezone-aware")
            except ValueError as e: raise ValueError(f"Invalid ISO 8601 format: {e}")
            return v


# --- Модели Ответов ---

class LinkAccountResponse(BaseModel):
    client_secret: str

class ScheduleResponse(BaseModel):
    job_id: str
    message: str = "Task scheduled successfully"

class ScheduledTaskInfo(BaseModel):
    """Информация о запланированной задаче, возвращаемая клиенту."""
    job_id: str
    recipient_id: str
    message_preview: str = Field(..., description="First 50 characters of the message")
    # scheduled_at_iso: str # Время создания задачи - не всегда полезно
    next_run_time_iso: str | None = Field(..., description="Next execution time in ISO UTC format, or null")
    status: str = "PENDING" # Статус по умолчанию для задач из планировщика

# Модель для списка чатов
class ConversationItem(BaseModel):
    peer_id: int
    title: str # Название чата или имя пользователя

class ConversationListResponse(BaseModel):
    items: list[ConversationItem]
    total_count: int # Общее количество диалогов (важно для пагинации)

# Стандартные модели ошибок
class ErrorResponse(BaseModel):
    detail: str

class HTTPValidationErrorDetail(BaseModel):
    loc: list[str | int]
    msg: str
    type: str

class HTTPValidationError(BaseModel):
     detail: list[HTTPValidationErrorDetail] | None = None
