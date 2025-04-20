from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.responses import JSONResponse
import uvicorn
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import uuid
import logging

import database
import security
import vk_api
import scheduler
import models

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Код, выполняемый перед запуском приложения
    logger.info("Starting application lifespan...")
    database.init_db() # Инициализируем БД
    sched = scheduler.init_scheduler() # Инициализируем планировщик
    try:
        sched.start() # Запускаем планировщик
        logger.info("Scheduler started.")
    except Exception as e:
        logger.error(f"Failed to start scheduler: {e}", exc_info=True)
        # Решить, должно ли приложение падать, если планировщик не стартовал
        # raise RuntimeError("Scheduler failed to start") from e
    yield
    # Код, выполняемый при остановке приложения
    logger.info("Shutting down application lifespan...")
    if sched and sched.running:
        sched.shutdown()
        logger.info("Scheduler shut down.")

app = FastAPI(
    title="VK Scheduler API",
    description="API for scheduling VK messages",
    version="1.0.0",
    lifespan=lifespan # Используем новый менеджер контекста lifespan
)

# --- Обработчик ошибок для лучшего ответа клиенту ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred"},
    )

# --- API Эндпоинты ---

@app.post("/api/link_account",
          response_model=models.LinkAccountResponse,
          summary="Link VK Account",
          description="Validates VK token, generates and saves a client secret.",
          status_code=status.HTTP_201_CREATED,
          responses={
              400: {"model": models.ErrorResponse, "description": "Invalid VK Token"},
              500: {"model": models.ErrorResponse, "description": "Database or Internal Error"}
          })
async def link_vk_account(request: models.LinkAccountRequest):
    """
    Принимает VK токен, валидирует его, генерирует client_secret,
    шифрует токен и сохраняет связку в БД.
    """
    vk_user_id = await vk_api.validate_vk_token(request.vk_access_token)
    if vk_user_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired VK token")

    client_secret = security.generate_client_secret()
    encrypted_token = security.encrypt_token(request.vk_access_token)

    try:
        # Используем синхронный доступ, обернув в async thread (не идеально, но работает для SQLite)
        # В продакшене с async DB driver было бы лучше
        def db_insert():
             with closing(sqlite3.connect(database.SYNC_DATABASE_PATH)) as conn:
                 with closing(conn.cursor()) as cursor:
                    # Используем INSERT OR IGNORE или ON CONFLICT для обработки существующих vk_user_id
                    cursor.execute("""
                        INSERT INTO accounts (client_secret, vk_user_id, encrypted_vk_token)
                        VALUES (?, ?, ?)
                        ON CONFLICT(vk_user_id) DO UPDATE SET
                            client_secret=excluded.client_secret,
                            encrypted_vk_token=excluded.encrypted_vk_token
                        """, (client_secret, vk_user_id, encrypted_token))
                    # Можно также обновить client_secret, если vk_user_id уже есть
                    # Или вернуть ошибку 409 Conflict, если vk_user_id уже привязан
                    conn.commit()
        await asyncio.to_thread(db_insert) # Выполняем синхронный код в потоке

    except sqlite3.IntegrityError as e:
        logger.warning(f"Integrity error linking account for vk_user_id {vk_user_id}: {e}")
        # Потенциально гонка потоков или уникальный client_secret уже существует (маловероятно)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database conflict during account linking")
    except sqlite3.Error as e:
        logger.error(f"Database error linking account: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database error")

    return models.LinkAccountResponse(client_secret=client_secret)


@app.post("/api/schedules",
           response_model=models.ScheduleResponse,
           summary="Schedule a Message",
           description="Schedules a new message to be sent via VK.",
           status_code=status.HTTP_202_ACCEPTED, # 202 т.к. задача принята, но не выполнена
           responses={
               400: {"model": models.ErrorResponse, "description": "Invalid input data (time format, etc.)"},
               401: {"model": models.ErrorResponse, "description": "Invalid client secret"},
               500: {"model": models.ErrorResponse, "description": "Scheduler or Internal Error"}
           })
async def schedule_message(
    request: models.ScheduleRequest,
    account_id: int = Depends(security.get_account_id_from_secret) # Зависимость для аутентификации
):
    """Планирует отправку сообщения."""
    sched = scheduler.get_scheduler()
    try:
        # Парсим время из ISO строки и конвертируем в UTC datetime object
        run_date_dt = datetime.fromisoformat(request.scheduled_at.replace('Z', '+00:00'))
        # Убедимся, что время в будущем
        if run_date_dt <= datetime.now(timezone.utc):
             raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Scheduled time must be in the future")

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid scheduled_at format: {e}")

    job_id = str(uuid.uuid4()) # Генерируем уникальный ID для задачи

    try:
        sched.add_job(
            scheduler.schedule_vk_message_job,
            trigger='date', # Запуск один раз в указанную дату
            run_date=run_date_dt, # Передаем datetime объект
            id=job_id,
            name=f"VKMsg_{account_id}_{request.recipient_id}_{job_id[:8]}", # Имя для логов
            replace_existing=False, # Не заменять, если ID уже есть (маловероятно)
            misfire_grace_time=60, # Время (в сек), в течение которого задача может быть запущена после просрочки
            # Передаем аргументы в функцию задачи
            kwargs={
                'account_id': account_id,
                'recipient_id': request.recipient_id,
                'message': request.message,
                'job_id': job_id # Передаем ID самой задаче для логирования
            }
        )
        logger.info(f"Scheduled job {job_id} for account {account_id} at {run_date_dt}")
    except Exception as e:
         logger.error(f"Error adding job to scheduler: {e}", exc_info=True)
         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to schedule task")

    return models.ScheduleResponse(job_id=job_id)


@app.get("/api/schedules",
          response_model=list[models.ScheduledTaskInfo],
          summary="Get Scheduled Tasks",
          description="Retrieves a list of pending scheduled tasks for the authenticated user.",
           responses={
               401: {"model": models.ErrorResponse, "description": "Invalid client secret"},
               500: {"model": models.ErrorResponse, "description": "Scheduler or Internal Error"}
           })
async def get_scheduled_tasks(
    account_id: int = Depends(security.get_account_id_from_secret)
):
    """Возвращает список запланированных задач для пользователя."""
    sched = scheduler.get_scheduler()
    tasks_info = []
    try:
        jobs = sched.get_jobs()
        for job in jobs:
            # Фильтруем задачи по account_id, который мы сохранили в kwargs
            if job.kwargs.get('account_id') == account_id:
                scheduled_at = job.trigger.run_date if hasattr(job.trigger, 'run_date') else None
                next_run = job.next_run_time
                message_preview = job.kwargs.get('message', '')[:50] + "..."

                tasks_info.append(models.ScheduledTaskInfo(
                    job_id=job.id,
                    recipient_id=job.kwargs.get('recipient_id', '?'),
                    message_preview=message_preview,
                    # Время хранится и возвращается в UTC ISO формате
                    scheduled_at_iso=scheduled_at.isoformat() if scheduled_at else "N/A",
                    next_run_time_iso=next_run.isoformat() if next_run else "N/A"
                ))
    except Exception as e:
        logger.error(f"Error retrieving jobs from scheduler: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve scheduled tasks")

    return tasks_info

@app.delete("/api/schedules/{job_id}",
            status_code=status.HTTP_204_NO_CONTENT,
            summary="Delete Scheduled Task",
            description="Deletes a specific scheduled task by its ID.",
            responses={
               204: {"description": "Task deleted successfully"},
               401: {"model": models.ErrorResponse, "description": "Invalid client secret"},
               403: {"model": models.ErrorResponse, "description": "Forbidden to delete this task"},
               404: {"model": models.ErrorResponse, "description": "Task not found"},
               500: {"model": models.ErrorResponse, "description": "Scheduler or Internal Error"}
           })
async def delete_scheduled_task(
    job_id: str,
    account_id: int = Depends(security.get_account_id_from_secret)
):
    """Удаляет запланированную задачу."""
    sched = scheduler.get_scheduler()
    try:
        job = sched.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled task not found")

        # Проверка владения задачей
        if job.kwargs.get('account_id') != account_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to delete this task")

        sched.remove_job(job_id)
        logger.info(f"Removed job {job_id} for account {account_id}")
        # Успешное удаление, возвращаем 204 без тела ответа
        return None # FastAPI автоматически вернет 204

    except Exception as e:
         logger.error(f"Error removing job {job_id}: {e}", exc_info=True)
         # Различаем ошибку 'не найдено' от других ошибок планировщика
         if "No job by the id" in str(e): # Проверка может быть ненадежной
              raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled task not found")
         else:
              raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to remove scheduled task")

# --- Запуск сервера (если файл запускается напрямую) ---
if __name__ == "__main__":
    # Запуск с авто-перезагрузкой для разработки: uvicorn main_server:app --reload
    # Для продакшена используется Gunicorn (см. инструкции по развертыванию)
    print("Starting server with Uvicorn for development...")
    print("NOTE: For production, use Gunicorn as described in deployment steps.")
    uvicorn.run("main_server:app", host="127.0.0.1", port=8000, reload=True) # reload=True для разработки
