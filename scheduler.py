from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_MISSED
from pytz import utc
import logging

from database import DATABASE_URL # Абсолютный импорт
from vk_api import send_vk_message # Абсолютный импорт
from security import get_decrypted_vk_token # Абсолютный импорт

# Настраиваем логгер APScheduler
logging.basicConfig(level=logging.INFO) # Устанавливаем общий уровень INFO
logging.getLogger('apscheduler').setLevel(logging.INFO) # Устанавливаем уровень для APScheduler
logger = logging.getLogger(__name__) # Логгер для нашего модуля

scheduler = None

async def schedule_vk_message_job(account_id: int, recipient_id: str, message: str, job_id: str):
    """
    Функция, которую выполняет APScheduler для отправки сообщения VK.
    """
    log_prefix = f"[Job {job_id}]"
    logger.info(f"{log_prefix} Running for account_id={account_id}, recipient_id={recipient_id}")

    # Получаем и расшифровываем токен синхронно (т.к. security использует sqlite3)
    # В идеале, если бы security был async, можно было бы использовать await asyncio.to_thread
    decrypted_token = get_decrypted_vk_token(account_id)

    if not decrypted_token:
        logger.error(f"{log_prefix} Error: Could not get/decrypt VK token for account_id {account_id}. Skipping job.")
        # Здесь можно предпринять действия:
        # 1. Попытаться удалить эту задачу, чтобы она не выполнялась снова (если это не повторяющаяся задача)
        #    try:
        #        global scheduler
        #        if scheduler and scheduler.running:
        #            scheduler.remove_job(job_id)
        #            logger.info(f"{log_prefix} Removed job due to token error.")
        #    except Exception as e:
        #        logger.error(f"{log_prefix} Failed to remove job {job_id} after token error: {e}")
        # 2. Залогировать ошибку в специальную таблицу для последующего анализа.
        # 3. Ничего не делать, задача просто не выполнится в этот раз.
        return # Прекращаем выполнение задачи

    success, vk_message_id, error_message = await send_vk_message(
        token=decrypted_token,
        recipient_id=recipient_id,
        message=message,
        job_id=job_id # Передаем ID для логирования внутри send_vk_message
    )

    if success:
        logger.info(f"{log_prefix} VK message sent successfully. VK Response/ID: {vk_message_id}")
        # Опционально: Записать результат в БД (например, статус "SENT", vk_message_id)
        # Это потребует добавления логики обновления статуса задачи в БД
    else:
        logger.error(f"{log_prefix} Failed to send VK message: {error_message}")
        # Опционально: Записать результат в БД (например, статус "ERROR", error_message)

# --- Обработчики событий APScheduler ---
def job_listener(event):
    """Слушает события выполнения, ошибок и пропусков задач."""
    log_prefix = f"[Job {event.job_id}]"
    if event.exception:
        logger.error(f"{log_prefix} Execution ERROR: {event.exception}", exc_info=event.exception)
        # Здесь можно добавить дополнительную логику обработки ошибок,
        # например, отправку уведомлений администратору
    elif isinstance(event, logging.LogRecord) and event.levelname == 'ERROR': # Перехват внутренних ошибок APScheduler
         logger.error(f"[APScheduler Internal] {event.getMessage()}")
    elif hasattr(event, 'scheduled_run_time'): # Для EVENT_JOB_MISSED
        logger.warning(f"{log_prefix} MISSED execution. Scheduled at: {event.scheduled_run_time}")
    else: # Успешное выполнение (EVENT_JOB_EXECUTED)
         logger.info(f"{log_prefix} Executed successfully. Return value: {event.retval}")

def init_scheduler():
    """Инициализирует и возвращает экземпляр планировщика."""
    global scheduler
    if scheduler and scheduler.running:
        logger.warning("Scheduler already initialized and running.")
        return scheduler
    elif scheduler: # Если есть экземпляр, но не запущен
         logger.info("Scheduler instance exists but not running. Reinitializing.")
         # Можно попытаться его запустить или пересоздать
         # pass

    # Используем синхронный URL для SQLAlchemyJobStore
    sync_db_url = DATABASE_URL.replace("sqlite+aiosqlite", "sqlite")
    logger.info(f"Using database URL for APScheduler JobStore: {sync_db_url}")

    jobstores = {
        'default': SQLAlchemyJobStore(url=sync_db_url)
    }
    executors = {
        'default': AsyncIOExecutor()
    }
    job_defaults = {
        'coalesce': False, # Не объединять задачи при пропущенном запуске
        'max_instances': 5, # Увеличим количество одновременных запусков, если нужно
        'misfire_grace_time': 300 # 5 минут - время, в течение которого задача может быть запущена после просрочки
    }

    try:
        scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults=job_defaults,
            timezone=utc # Явно указываем UTC
        )
        # Добавляем слушателей событий
        scheduler.add_listener(job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED)
        logger.info("APScheduler initialized successfully.")
        # Запуск планировщика будет выполнен в lifespan FastAPI
    except Exception as e:
        logger.critical(f"CRITICAL: Error initializing APScheduler: {e}", exc_info=True)
        # Без планировщика приложение бесполезно
        raise RuntimeError("Failed to initialize APScheduler") from e

    return scheduler

def get_scheduler() -> AsyncIOScheduler | None:
    """Возвращает инициализированный экземпляр планировщика или None."""
    # Не вызываем ошибку, если не инициализирован, main_server проверит
    return scheduler
