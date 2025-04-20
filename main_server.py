# main_server.py
from fastapi import FastAPI, Depends, HTTPException, status, Request, Header, Query # Добавлены Header, Query
from fastapi.responses import JSONResponse
import uvicorn
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import uuid
import logging
import sqlite3 # Нужен для обработки ошибок IntegrityError
from contextlib import closing
import asyncio # Нужен для asyncio.to_thread
from typing import Optional, List # Добавлена List

# --- Модули вашего проекта ---
import database
import security # Предполагается наличие get_account_id_from_secret, generate_client_secret, encrypt_token
import vk_api # Предполагается наличие validate_vk_token, fetch_conversations
import scheduler # Предполагается наличие init_scheduler, get_scheduler, schedule_vk_message_job
import models # Предполагается наличие всех моделей Pydantic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Зависимость для аутентификации по заголовку "Authorization: Secret <token>" ---
# Эта зависимость получает ДЕШИФРОВАННЫЙ токен VK
async def get_vk_token_from_secret(authorization: Optional[str] = Header(None)) -> str:
    """
    Проверяет заголовок Authorization, извлекает client_secret,
    получает соответствующий зашифрованный токен VK из БД, дешифрует его и возвращает.
    """
    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Secret"},
        )

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "secret":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format. Expected 'Secret <your_client_secret>'",
            headers={"WWW-Authenticate": "Secret"},
        )

    client_secret = parts[1]
    decrypted_token = await database.get_decrypted_token_by_secret(client_secret)

    if decrypted_token is None:
        logger.warning(f"Authentication failed: Invalid or unknown client secret provided (ends with ...{client_secret[-4:]})")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid client secret",
            headers={"WWW-Authenticate": "Secret"},
        )
    # Можно добавить проверку валидности токена здесь, если нужно быть уверенным
    # user_id = await vk_api.validate_vk_token(decrypted_token)
    # if user_id is None:
    #     raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="VK token associated with secret is invalid or expired")

    logger.info(f"Authentication successful for secret ending with ...{client_secret[-4:]}")
    return decrypted_token

# --- Менеджер контекста Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Код, выполняемый перед запуском приложения
    logger.info("Starting application lifespan...")
    database.init_db() # Инициализируем БД
    sched = scheduler.init_scheduler() # Инициализируем планировщик
    try:
        if not sched.running: # Проверяем, не запущен ли уже (на случай HMR)
            sched.start() # Запускаем планировщик
            logger.info("Scheduler started.")
        else:
            logger.info("Scheduler already running.")
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

# --- Инициализация FastAPI ---
app = FastAPI(
    title="VK Scheduler API",
    description="API for scheduling VK messages",
    version="1.0.0",
    lifespan=lifespan # Используем новый менеджер контекста lifespan
)

# --- Обработчики ошибок ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception during request to {request.url}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred. Please check server logs."},
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
    Принимает VK токен, валидирует его, получает vk_user_id,
    генерирует client_secret, шифрует токен и сохраняет/обновляет связку в БД.
    """
    vk_user_id = await vk_api.validate_vk_token(request.vk_access_token)
    if vk_user_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired VK token")

    client_secret = security.generate_client_secret()
    encrypted_token = security.encrypt_token(request.vk_access_token) # Шифруем перед сохранением

    try:
        # Используем синхронный доступ к SQLite в отдельном потоке
        def db_insert_or_update():
            try:
                with closing(sqlite3.connect(database.SYNC_DATABASE_PATH, timeout=10)) as conn:
                    with closing(conn.cursor()) as cursor:
                        # Используем INSERT ... ON CONFLICT для атомарного обновления или вставки
                        cursor.execute("""
                            INSERT INTO accounts (client_secret, vk_user_id, encrypted_vk_token)
                            VALUES (?, ?, ?)
                            ON CONFLICT(vk_user_id) DO UPDATE SET
                                client_secret=excluded.client_secret,
                                encrypted_vk_token=excluded.encrypted_vk_token,
                                created_at=CURRENT_TIMESTAMP
                            """, (client_secret, vk_user_id, encrypted_token))
                        conn.commit()
                        logger.info(f"Successfully linked/updated account for vk_user_id: {vk_user_id}")
            except sqlite3.Error as e:
                # Ловим ошибки SQLite внутри потока и пробрасываем их
                logger.error(f"Database error during insert/update for vk_user_id {vk_user_id}: {e}")
                raise # Пробросить ошибку, чтобы ее поймал внешний try...except

        await asyncio.to_thread(db_insert_or_update) # Выполняем синхронный код в потоке

    except sqlite3.Error as e: # Ловим ошибки SQLite, проброшенные из потока
        logger.error(f"Database operation failed for vk_user_id {vk_user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Database error during account linking: {e}")
    except Exception as e: # Ловим другие возможные ошибки (шифрования и т.д.)
         logger.error(f"Unexpected error linking account for vk_user_id {vk_user_id}: {e}", exc_info=True)
         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Internal server error: {e}")


    return models.LinkAccountResponse(client_secret=client_secret)


@app.post("/api/schedules",
          response_model=models.ScheduleResponse, # Должна быть определена в models.py
          summary="Schedule a Message",
          description="Schedules a new message to be sent via VK.",
          status_code=status.HTTP_202_ACCEPTED, # 202 т.к. задача принята, но не выполнена
          responses={
              400: {"model": models.ErrorResponse, "description": "Invalid input data (time format, etc.)"},
              401: {"model": models.ErrorResponse, "description": "Invalid client secret"},
              500: {"model": models.ErrorResponse, "description": "Scheduler or Internal Error"}
          })
async def schedule_message(
    request: models.ScheduleRequest, # Должна быть определена в models.py
    # Используем зависимость, которая получает ID аккаунта из БД по секрету
    account_id: int = Depends(security.get_account_id_from_secret) # Должна быть определена в security.py
):
    """Планирует отправку сообщения."""
    sched = scheduler.get_scheduler()
    if not sched or not sched.running:
         logger.error("Scheduler is not running or not initialized.")
         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Scheduler service is unavailable")

    try:
        # Парсим время из ISO строки и конвертируем в UTC datetime object
        run_date_dt = datetime.fromisoformat(request.scheduled_at.replace('Z', '+00:00'))
        run_date_dt = run_date_dt.astimezone(timezone.utc) # Убедимся, что timezone=UTC

        # Убедимся, что время в будущем (с небольшим запасом)
        # Добавляем пару секунд, чтобы избежать гонки состояний при мгновенном планировании
        if run_date_dt <= datetime.now(timezone.utc) + timedelta(seconds=2):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Scheduled time must be at least a few seconds in the future")

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid scheduled_at format: {e}")

    job_id = str(uuid.uuid4()) # Генерируем уникальный ID для задачи

    try:
        sched.add_job(
            scheduler.schedule_vk_message_job, # Функция, которая будет выполнена
            trigger='date', # Запуск один раз в указанную дату
            run_date=run_date_dt, # Передаем datetime объект (уже в UTC)
            id=job_id,
            name=f"VKMsg_{account_id}_{request.recipient_id}_{job_id[:8]}", # Имя для логов
            replace_existing=False, # Не заменять, если ID уже есть (маловероятно)
            misfire_grace_time=60, # Время (в сек), в течение которого задача может быть запущена после просрочки
            # Передаем аргументы в функцию задачи schedule_vk_message_job
            kwargs={
                'account_id': account_id, # ID аккаунта из нашей БД
                'recipient_id': request.recipient_id,
                'message': request.message,
                'job_id': job_id # Передаем ID самой задаче для логирования
            }
        )
        logger.info(f"Scheduled job {job_id} for account {account_id} at {run_date_dt}")
    except Exception as e:
        logger.error(f"Error adding job to scheduler: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to schedule task: {e}")

    # Возвращаем ID задачи клиенту
    return models.ScheduleResponse(job_id=job_id, message="Task scheduled successfully") # Модель ответа должна поддерживать message


@app.get("/api/schedules",
         response_model=List[models.ScheduledTaskInfo], # Должна быть определена в models.py
         summary="Get Scheduled Tasks",
         description="Retrieves a list of pending scheduled tasks for the authenticated user.",
         responses={
             401: {"model": models.ErrorResponse, "description": "Invalid client secret"},
             500: {"model": models.ErrorResponse, "description": "Scheduler or Internal Error"}
         })
async def get_scheduled_tasks(
    account_id: int = Depends(security.get_account_id_from_secret) # Зависимость для аутентификации
):
    """Возвращает список запланированных задач для пользователя."""
    sched = scheduler.get_scheduler()
    if not sched:
        logger.error("Scheduler is not initialized.")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Scheduler service is unavailable")

    tasks_info = []
    try:
        jobs = sched.get_jobs()
        for job in jobs:
            # Фильтруем задачи по account_id, который мы сохранили в kwargs при создании задачи
            if job.kwargs.get('account_id') == account_id:
                # scheduled_at = job.trigger.run_date if hasattr(job.trigger, 'run_date') else None # Для trigger='date'
                next_run = job.next_run_time # Это время уже должно быть в UTC, т.к. APScheduler работает с UTC

                # Превью сообщения для ответа
                message_preview = str(job.kwargs.get('message', '')) # Преобразуем в строку на всякий случай
                if len(message_preview) > 50:
                    message_preview = message_preview[:50] + "..."

                # Собираем информацию о задаче
                tasks_info.append(models.ScheduledTaskInfo(
                    job_id=job.id,
                    recipient_id=str(job.kwargs.get('recipient_id', '?')), # Преобразуем в строку
                    message_preview=message_preview,
                    # Время возвращается в UTC ISO формате, клиент его преобразует
                    next_run_time_iso=next_run.isoformat(timespec='milliseconds').replace('+00:00', 'Z') if next_run else "N/A",
                    status="PENDING" # APScheduler сам не хранит статус PENDING/SENT/ERROR, это нужно реализовывать отдельно, если требуется
                ))
    except Exception as e:
        logger.error(f"Error retrieving jobs from scheduler: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to retrieve scheduled tasks: {e}")

    return tasks_info


@app.delete("/api/schedules/{job_id}",
            status_code=status.HTTP_204_NO_CONTENT,
            summary="Delete Scheduled Task",
            description="Deletes a specific scheduled task by its ID.",
            responses={
                204: {"description": "Task deleted successfully or already gone"},
                401: {"model": models.ErrorResponse, "description": "Invalid client secret"},
                403: {"model": models.ErrorResponse, "description": "Forbidden to delete this task"},
                404: {"model": models.ErrorResponse, "description": "Task not found (deprecated by 204)"}, # Технически 404 возможно, но 204 покрывает 'не найдено'
                500: {"model": models.ErrorResponse, "description": "Scheduler or Internal Error"}
            })
async def delete_scheduled_task(
    job_id: str,
    account_id: int = Depends(security.get_account_id_from_secret) # Зависимость для аутентификации
):
    """Удаляет запланированную задачу."""
    sched = scheduler.get_scheduler()
    if not sched:
         logger.error("Scheduler is not initialized during delete request.")
         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Scheduler service is unavailable")

    try:
        job = sched.get_job(job_id) # Попытка получить задачу

        if job is not None:
            # Задача найдена, проверяем владение
            if job.kwargs.get('account_id') != account_id:
                logger.warning(f"Account {account_id} attempted to delete job {job_id} owned by account {job.kwargs.get('account_id')}")
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to delete this task")

            # Владение подтверждено, удаляем задачу
            sched.remove_job(job_id)
            logger.info(f"Removed job {job_id} for account {account_id}")
        else:
             # Задача не найдена, это не ошибка для DELETE (идемпотентность)
             logger.info(f"Attempted to delete non-existent job {job_id} (account {account_id}). Treating as success (204).")

        # Успешное удаление или отсутствие задачи -> возвращаем 204
        return None # FastAPI автоматически вернет 204

    except Exception as e: # Ловим прочие ошибки планировщика
        logger.error(f"Error removing job {job_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to remove scheduled task: {e}")


# --- НОВЫЙ ЭНДПОИНТ ДЛЯ ЗАГРУЗКИ ЧАТОВ ---
@app.get("/api/vk/conversations",
         response_model=models.GetConversationsResponse, # Должна быть определена в models.py
         summary="Get VK Conversations",
         description="Retrieves a paginated list of user's VK conversations.",
         responses={
             401: {"model": models.ErrorResponse, "description": "Invalid client secret"},
             400: {"model": models.ErrorResponse, "description": "VK API Error (e.g., token invalid, permissions)"},
             500: {"model": models.ErrorResponse, "description": "Internal Server Error"}
         })
async def get_vk_conversations_paginated(
    offset: int = Query(0, ge=0, description="Number of conversations to skip"),
    count: int = Query(10, ge=1, le=50, description="Number of conversations to return (max 50)"), # Ограничиваем count
    # Используем новую зависимость для получения токена
    vk_token: str = Depends(get_vk_token_from_secret)
):
    """
    Возвращает постраничный список диалогов/бесед пользователя VK.
    """
    try:
        # Вызываем функцию из модуля vk_api для получения данных
        # Эта функция должна обрабатывать ошибки VK API внутри себя
        result = await vk_api.fetch_conversations(
            token=vk_token,
            offset=offset,
            count=count
        )

        if result is None:
            # Функция fetch_conversations вернула None, что указывает на ошибку VK API
            # (например, невалидный токен, который прошел первичную проверку, или нехватка прав)
            # Логирование ошибки должно быть внутри fetch_conversations
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, # Или 401, если ошибка связана с токеном
                detail="Failed to fetch conversations from VK. Check token validity and permissions."
            )

        # Распаковываем результат (список чатов и общее количество)
        conversation_items, total_count = result

        # Формируем ответ в соответствии с моделью Pydantic
        return models.GetConversationsResponse(
            items=conversation_items,
            total_count=total_count
        )

    except HTTPException as e:
         # Пробрасываем HTTP ошибки, которые могли возникнуть ранее (например, 401 от зависимости)
         raise e
    except Exception as e:
        # Ловим любые другие неожиданные ошибки
        logger.error(f"Error fetching VK conversations: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Internal error fetching conversations: {e}")


# --- Запуск сервера (если файл запускается напрямую) ---
if __name__ == "__main__":
    # Запуск с авто-перезагрузкой для разработки: uvicorn main_server:app --reload
    # Для продакшена используется Gunicorn или другой ASGI сервер
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    reload_flag = os.getenv("RELOAD", "true").lower() == "true" # Перезагрузка по умолчанию включена

    print(f"Starting server with Uvicorn on {host}:{port}...")
    if reload_flag:
        print("Reloading is enabled.")
        uvicorn.run("main_server:app", host=host, port=port, reload=True)
    else:
         print("Reloading is disabled.")
         uvicorn.run(app, host=host, port=port) # Для запуска без reload нужно передать сам app объект
