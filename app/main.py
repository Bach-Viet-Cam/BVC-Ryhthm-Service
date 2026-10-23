import asyncio
import logging

import uvicorn

from app.api import create_app
from app.config import get_settings
from app.database import initialize_database
from app.messaging.rabbitmq_worker import RabbitMqWorker

logging.basicConfig(level=logging.INFO)
app = create_app()


async def run_worker() -> None:
    await RabbitMqWorker().run()


async def run_service() -> None:
    settings = get_settings()
    for attempt in range(1, 31):
        try:
            await asyncio.to_thread(initialize_database)
            break
        except Exception:
            if attempt == 30:
                raise
            logging.warning("Database chưa sẵn sàng, thử lại lần %s/30", attempt)
            await asyncio.sleep(2)

    server_config = uvicorn.Config(app, host=settings.app_host, port=settings.app_port, log_level="info")
    server = uvicorn.Server(server_config)
    worker_task = asyncio.create_task(run_worker())
    server_task = asyncio.create_task(server.serve())
    try:
        completed, _ = await asyncio.wait({worker_task, server_task}, return_when=asyncio.FIRST_COMPLETED)
        if worker_task in completed:
            await worker_task  # Propagate worker failures so the container can restart.
            raise RuntimeError("Worker đã dừng ngoài dự kiến.")
        await server_task
    finally:
        server.should_exit = True
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        await asyncio.gather(server_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(run_service())
