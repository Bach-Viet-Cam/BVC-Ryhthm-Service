import asyncio
import json
import logging

import aio_pika
from aio_pika import IncomingMessage, Message, RobustChannel, RobustConnection

from app.audio.downloader import AudioDownloadError
from app.config import get_settings
from app.database import SessionFactory
from app.repositories.evaluation_repository import EvaluationRepository
from app.schemas.messages import EvaluationTask
from app.services.evaluation_service import EvaluationService, PermanentEvaluationError

logger = logging.getLogger(__name__)


class RabbitMqWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.repository = EvaluationRepository()
        self.evaluation_service = EvaluationService(
            self.repository, SessionFactory, self.settings.temp_directory
        )
        self.connection: RobustConnection | None = None

    async def run(self) -> None:
        self.connection = await aio_pika.connect_robust(self.settings.rabbitmq_url)
        channel = await self.connection.channel()
        await channel.set_qos(prefetch_count=1)
        queue = await self._declare_queues(channel)
        logger.info("Listening on %s", self.settings.rhythm_task_queue)
        await queue.consume(self.handle_message, no_ack=False)
        await asyncio.Future()

    async def _declare_queues(self, channel: RobustChannel):
        dead_letter_queue = await channel.declare_queue(self.settings.rhythm_dead_letter_queue, durable=True)
        await dead_letter_queue.consume(self._log_dead_letter, no_ack=True)
        return await channel.declare_queue(
            self.settings.rhythm_task_queue,
            durable=True,
            arguments={
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": self.settings.rhythm_dead_letter_queue,
            },
        )

    async def _log_dead_letter(self, message: IncomingMessage) -> None:
        logger.error("Task moved to dead-letter queue: %s", message.body.decode("utf-8", errors="replace"))

    async def handle_message(self, message: IncomingMessage) -> None:
        try:
            task = EvaluationTask.model_validate_json(message.body)
        except Exception as error:
            logger.error("Invalid evaluation task: %s", error)
            await message.reject(requeue=False)
            return

        try:
            await self.evaluation_service.process_task(task)
            await message.ack()
        except AudioDownloadError as error:
            await self._handle_error(message, task, error.code, str(error), error.retryable)
        except (ValueError, PermanentEvaluationError) as error:
            code = error.code if isinstance(error, PermanentEvaluationError) else "EVALUATION_FAILED"
            await self._handle_error(message, task, code, str(error), False)
        except Exception:
            logger.exception("Unexpected error while processing %s", task.evaluationId)
            await self._handle_error(message, task, "INTERNAL_ERROR", "Lỗi nội bộ khi phân tích audio", True)

    async def _handle_error(
        self,
        message: IncomingMessage,
        task: EvaluationTask,
        code: str,
        error_message: str,
        retryable: bool,
    ) -> None:
        retry_count = int((message.headers or {}).get("x-retry-count", 0))
        # Practice tasks (with a callback) fail fast: the student retries them (GBR-59)
        if retryable and task.callbackUrl is None and retry_count < self.settings.max_retries:
            assert self.connection is not None
            channel = await self.connection.channel()
            retry_message = Message(
                message.body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                headers={"x-retry-count": retry_count + 1},
            )
            await channel.default_exchange.publish(retry_message, routing_key=self.settings.rhythm_task_queue)
            await channel.close()
            await message.ack()
            return

        await self.evaluation_service.record_failure(task, code, error_message)
        await message.ack()
