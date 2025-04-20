from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.asyncio import AsyncIOExecutor
from pytz import utc
import logging

from .database import DATABASE_URL
from .vk_api import send_vk_message
from .security import get_decrypted_vk_token

logging.basicConfig()
logging.getLogger('apscheduler').setLevel(logging.INFO) # Уровень логгирования планировщика

scheduler = None

async def schedule_vk_message_job(account_id: int, recipient_id: str, message: str, job_id: str):
    """
    Функция, которую выполняет APScheduler.
    Отправляет сообщение VK.
    """
    print(f"[Job {job_id}] Running for account {account_id}, recipient {recipient_id}")
    decrypted_token = get_decrypted_vk_token(account_id) # Получаем токен синхронно

    if not decrypted_token:
        print(f"[Job {job_id}] Error: Could not get decrypted VK token for account {account_id}. Skipping job.")
        # Можно попытаться удалить задачу или пометить ее как ошибочную
        # scheduler.remove_job(job_id) # Осторожно: может вызвать проблемы с блокировками
        return

    success, vk_message_id, error_message = await send_vk_message(
        token=decrypted_token,
        recipient_id=recipient_id,
        message=message
    )

    if success:
        print(f"[Job {job_id}] VK message sent successfully. VK Message ID: {vk_message_id}")
        # Здесь можно добавить логирование успеха в отдельную таблицу, если нужно
    else:
        print(f"[Job {job_id}] Failed to send VK message: {error_message}")
        # Здесь можно добавить логирование ошибки

def init_scheduler():
    """Инициализирует и запускает планировщик."""
    global scheduler
    if scheduler and scheduler.running:
        print("Scheduler already running.")
        return scheduler

    jobstores = {
        # Используем SQLAlchemyJobStore для персистентности задач в БД
        'default': SQLAlchemyJobStore(url=DATABASE_URL.replace("sqlite+aiosqlite", "sqlite")) # SQLAlchemyJobStore нужен синхронный URL
    }
    executors = {
        'default': AsyncIOExecutor()
    }
    job_defaults = {
        'coalesce': False, # Не объединять одинаковые задачи, если сервер перезапускался
        'max_instances': 3 # Ограничить количество одновременно выполняемых экземпляров задачи
    }
    scheduler = AsyncIOScheduler(
        jobstores=jobstores,
        executors=executors,
        job_defaults=job_defaults,
        timezone=utc # Работаем в UTC
    )
    try:
         # Важно: Запуск планировщика должен быть после инициализации FastAPI приложения
         # scheduler.start() # Перенесем запуск в main_server.py
         print("Scheduler initialized.")
    except Exception as e:
         print(f"Error starting scheduler: {e}")
         # Обработка ошибок, например, если БД недоступна
         raise
    return scheduler

def get_scheduler():
    """Возвращает инициализированный экземпляр планировщика."""
    if scheduler is None:
         raise RuntimeError("Scheduler not initialized. Call init_scheduler first.")
    return scheduler