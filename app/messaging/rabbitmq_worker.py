import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import partial

import aio_pika
from aio_pika import IncomingMessage, Message, RobustChannel, RobustConnection
from pydantic import ValidationError

from app.audio.downloader import AudioDownloadError
from app.config import get_settings
from app.database import SessionFactory
from app.repositories.evaluation_repository import EvaluationRepository
from app.repositories.template_analysis_repository import TemplateTaskConflictError
from app.schemas.messages import EvaluationTask
from app.schemas.template_messages import TemplateAnalysisTask
from app.services.audio_quality_service import AudioProcessingError, AudioQualityError
from app.services.evaluation_service import EvaluationService, PermanentEvaluationError
from app.services.template_analysis_service import TemplateAnalysisError, TemplateAnalysisService

logger = logging.getLogger(__name__)


class RabbitMqWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.repository = EvaluationRepository()
        self.evaluation_service = EvaluationService(self.repository, SessionFactory, self.settings.temp_directory)
        self.template_service = TemplateAnalysisService(session_factory=SessionFactory, settings=self.settings)
        self.connection: RobustConnection | None = None
        self._in_flight: set[asyncio.Task] = set()

    async def run(self) -> None:
        self.connection = await aio_pika.connect_robust(self.settings.rabbitmq_url)
        try:
            channel = await self.connection.channel()
            await channel.set_qos(prefetch_count=1)
            evaluation_queue = await self._declare_queues(channel)
            template_queue = await self._declare_template_queue(channel)
            await evaluation_queue.consume(partial(self._tracked, self.handle_message), no_ack=False)
            await template_queue.consume(partial(self._tracked, self.handle_template_message), no_ack=False)
            logger.info("Worker đang nhận job so sánh và phân tích template, xử lý tuần tự.")
            await asyncio.Future()
        finally:
            # Wait for shielded CPU/file work before closing storage and broker resources.
            pending = list(self._in_flight)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await self.connection.close()

    async def _tracked(self, handler: Callable[[IncomingMessage], Awaitable[None]], message: IncomingMessage) -> None:
        current = asyncio.current_task()
        self._in_flight.add(current)
        try:
            await handler(message)
        finally:
            self._in_flight.discard(current)

    async def _declare_queues(self, channel: RobustChannel):
        dead_letter_queue = await channel.declare_queue(self.settings.rhythm_dead_letter_queue, durable=True)
        await dead_letter_queue.consume(self._log_dead_letter, no_ack=True)
        return await channel.declare_queue(
            self.settings.rhythm_task_queue, durable=True,
            arguments={"x-dead-letter-exchange": "", "x-dead-letter-routing-key": self.settings.rhythm_dead_letter_queue},
        )

    async def _declare_template_queue(self, channel: RobustChannel):
        await channel.declare_queue(self.settings.template_dead_letter_queue, durable=True)
        return await channel.declare_queue(
            self.settings.template_task_queue, durable=True,
            arguments={"x-dead-letter-exchange": "", "x-dead-letter-routing-key": self.settings.template_dead_letter_queue},
        )

    async def _log_dead_letter(self, message: IncomingMessage) -> None:
        logger.error("Job so sánh đã chuyển vào dead-letter queue: %s", message.body.decode("utf-8", errors="replace"))

    async def handle_message(self, message: IncomingMessage) -> None:
        try:
            task = EvaluationTask.model_validate_json(message.body)
        except ValidationError:
            logger.error("Payload job so sánh không hợp lệ.")
            await message.reject(requeue=False)
            return
        try:
            await self._process_evaluation(message, task)
        except Exception:
            await self._requeue_after_failure(message)

    async def _process_evaluation(self, message: IncomingMessage, task: EvaluationTask) -> None:
        try:
            await self.evaluation_service.process_task(task)
        except AudioQualityError as error:
            await self._handle_error(message, task, error.code, str(error), False)
        except AudioProcessingError as error:
            await self._handle_error(message, task, error.code, str(error), True)
        except AudioDownloadError as error:
            await self._handle_error(message, task, error.code, str(error), error.retryable)
        except (ValueError, PermanentEvaluationError) as error:
            code = error.code if isinstance(error, PermanentEvaluationError) else "EVALUATION_FAILED"
            await self._handle_error(message, task, code, str(error), False)
        except Exception:
            logger.exception("Lỗi khi xử lý job so sánh %s", task.evaluationId)
            await self._handle_error(message, task, "INTERNAL_ERROR", "Lỗi nội bộ khi phân tích audio.", True)
        else:
            await message.ack()

    async def _handle_error(self, message: IncomingMessage, task: EvaluationTask, code: str,
                            error_message: str, retryable: bool) -> None:
        if retryable and self._retry_count(message) < self.settings.max_retries:
            await self._publish_retry(message, self.settings.rhythm_task_queue)
        else:
            self.evaluation_service.record_failure(task, code, error_message)
        await message.ack()

    async def handle_template_message(self, message: IncomingMessage) -> None:
        try:
            task = TemplateAnalysisTask.model_validate_json(message.body)
        except ValidationError:
            logger.error("Payload job template không hợp lệ.")
            await message.reject(requeue=False)
            return
        try:
            await self._process_template(message, task)
        except Exception:
            await self._requeue_after_failure(message)

    async def _process_template(self, message: IncomingMessage, task: TemplateAnalysisTask) -> None:
        try:
            await self.template_service.process_task(task)
        except TemplateTaskConflictError:
            logger.error("Job template trùng mã nhưng khác đầu vào: %s", task.analysisId)
            await message.reject(requeue=False)
        except AudioQualityError as error:
            await self._handle_template_error(message, task, error.code, str(error), False, error.report)
        except AudioProcessingError as error:
            await self._handle_template_error(message, task, error.code, str(error), True)
        except AudioDownloadError as error:
            await self._handle_template_error(message, task, error.code, str(error), error.retryable)
        except TemplateAnalysisError as error:
            await self._handle_template_error(message, task, error.code, str(error), False)
        except ValueError:
            await self._handle_template_error(message, task, "TEMPLATE_ANALYSIS_FAILED", "Không thể phân tích nội dung audio template.", False)
        except Exception:
            logger.exception("Lỗi khi xử lý template %s", task.analysisId)
            await self._handle_template_error(message, task, "INTERNAL_ERROR", "Lỗi nội bộ khi phân tích template.", True)
        else:
            await message.ack()

    async def _handle_template_error(self, message: IncomingMessage, task: TemplateAnalysisTask, code: str,
                                     error_message: str, retryable: bool, quality_report: dict | None = None) -> None:
        if retryable and self._retry_count(message) < self.settings.max_retries:
            await self._publish_retry(message, self.settings.template_task_queue)
        else:
            self.template_service.record_failure(task, code, error_message, quality_report)
        await message.ack()

    def _retry_count(self, message: IncomingMessage) -> int:
        try:
            return max(0, int((message.headers or {}).get("x-retry-count", 0)))
        except (ValueError, TypeError, OverflowError):
            return self.settings.max_retries

    async def _publish_retry(self, message: IncomingMessage, queue: str) -> None:
        if self.connection is None:
            raise RuntimeError("Chưa có kết nối hàng đợi để gửi lại job.")
        channel = await self.connection.channel(publisher_confirms=True, on_return_raises=True)
        try:
            retry = Message(
                message.body, content_type="application/json", delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                headers={**(message.headers or {}), "x-retry-count": self._retry_count(message) + 1},
                message_id=getattr(message, "message_id", None), correlation_id=getattr(message, "correlation_id", None),
            )
            confirmed = await channel.default_exchange.publish(retry, routing_key=queue, mandatory=True)
            if confirmed is False:
                raise RuntimeError("Broker chưa xác nhận nhận job gửi lại.")
        finally:
            await channel.close()

    async def _requeue_after_failure(self, message: IncomingMessage) -> None:
        logger.exception("Chưa thể xác nhận hoặc lưu trạng thái job; giao lại message.")
        if not getattr(message, "processed", False):
            await message.reject(requeue=True)
