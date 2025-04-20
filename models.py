from pydantic import BaseModel, Field, field_validator, validator
from datetime import datetime
import re

class LinkAccountRequest(BaseModel):
    vk_access_token: str = Field(..., min_length=10, description="VK Access Token")

class LinkAccountResponse(BaseModel):
    client_secret: str

class ScheduleRequest(BaseModel):
    recipient_id: str = Field(..., description="VK User ID, Peer ID, or negative Group ID")
    message: str = Field(..., min_length=1)
    scheduled_at: str = Field(..., description="ISO 8601 timestamp with timezone (e.g., YYYY-MM-DDTHH:MM:SS.sssZ or YYYY-MM-DDTHH:MM:SS.sss+HH:MM)")

    @field_validator('recipient_id')
    def check_recipient_id(cls, v):
        # Простая проверка, что это число (возможно, отрицательное)
        if not re.fullmatch(r"-?\d+", v):
            raise ValueError('recipient_id must be a valid integer string')
        return v

    @field_validator('scheduled_at')
    def check_datetime_format(cls, v):
        try:
            # Попытка парсинга для валидации формата и наличия таймзоны
            dt = datetime.fromisoformat(v.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                 raise ValueError("scheduled_at must be timezone-aware")
        except ValueError as e:
            raise ValueError(f"Invalid ISO 8601 format for scheduled_at: {e}")
        return v


class ScheduleResponse(BaseModel):
    job_id: str
    message: str = "Task scheduled successfully"

class ScheduledTaskInfo(BaseModel):
    job_id: str
    recipient_id: str
    message_preview: str
    scheduled_at_iso: str # Время в ISO UTC
    next_run_time_iso: str # Следующее время запуска по UTC

class ErrorResponse(BaseModel):
    detail: str