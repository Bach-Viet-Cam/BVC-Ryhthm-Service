"""Shared harness for the Rhythm integration tests (Report-5.2, sheets RE/RT/RW/RK).

The service under test runs as a real process (`python -m app.main`: API + worker) against a real
PostgreSQL database and a real RabbitMQ broker. One local HTTP stub plays the audio storage, the BE
callback endpoint and the Gemini API (through GOOGLE_GEMINI_BASE_URL), so no external service is called.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import aio_pika
import httpx
import numpy as np
import psycopg
import soundfile as sf
from sqlalchemy.engine import make_url

PROJECT = Path(__file__).resolve().parents[2]
RUN_ID = uuid.uuid4().hex[:8]
INTERNAL_KEY = f"it-internal-key-{RUN_ID}"
CALLBACK_SECRET = f"it-callback-secret-{RUN_ID}"
ENCRYPTION_SECRET = base64.b64encode(os.urandom(32)).decode()
RABBIT_URL = os.getenv("RHYTHM_IT_RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
EVALUATE_QUEUE = "rhythm.evaluate.v1"
EVALUATE_DLQ = "rhythm.evaluate.dlq"
TEMPLATE_QUEUE = "rhythm.template.analyze.v1"
TEMPLATE_DLQ = "rhythm.template.analyze.dlq"
ALL_QUEUES = (EVALUATE_QUEUE, EVALUATE_DLQ, TEMPLATE_QUEUE, TEMPLATE_DLQ)
SAMPLE_RATE = 22050
TRANH = {"name": "Đàn tranh", "fminHz": 146.8, "fmaxHz": 1174.7, "toleranceCents": 50}
GEMINI_MODEL = "it-gemini-model"
FEEDBACK_TEXT = "Bạn giữ nhịp khá đều, hãy chú ý cao độ ở đoạn giữa bài."
FALLBACK_FEEDBACK = "Chưa thể tạo nhận xét AI lúc này. Bạn vẫn có thể xem các chỉ số phân tích để tiếp tục luyện tập."


def python_executable() -> str:
    candidate = PROJECT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(candidate) if candidate.exists() else sys.executable


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(check: Callable[[], Any], timeout: float = 60.0, interval: float = 0.25, message: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"Timed out after {timeout}s waiting for {message}; last value: {last!r}")


# ---------------------------------------------------------------- database
def database_url() -> str:
    explicit = os.getenv("RHYTHM_IT_DATABASE_URL")
    if explicit:
        return explicit
    base = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"
    env_file = PROJECT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("RHYTHM_SERVICE_DATABASE_URL="):
                base = line.split("=", 1)[1].strip()
    return make_url(base).set(database="bvc_rhythm_it").render_as_string(hide_password=False)


def _libpq(url: str, database: str | None = None) -> str:
    parsed = make_url(url).set(drivername="postgresql")
    if database:
        parsed = parsed.set(database=database)
    return parsed.render_as_string(hide_password=False)


def reset_database(url: str) -> None:
    name = make_url(url).database
    with psycopg.connect(_libpq(url, "postgres"), autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        connection.execute(f'CREATE DATABASE "{name}"')


def query(url: str, sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(_libpq(url)) as connection:
        return connection.execute(sql, params).fetchall()


def execute(url: str, sql: str, params: tuple = ()) -> None:
    with psycopg.connect(_libpq(url), autocommit=True) as connection:
        connection.execute(sql, params)


# ---------------------------------------------------------------- RabbitMQ
async def _publish(queue: str, body: bytes, headers: dict | None) -> None:
    connection = await aio_pika.connect_robust(RABBIT_URL)
    async with connection:
        channel = await connection.channel(publisher_confirms=True)
        message = aio_pika.Message(body, content_type="application/json", headers=headers or {},
                                   delivery_mode=aio_pika.DeliveryMode.PERSISTENT)
        await channel.default_exchange.publish(message, routing_key=queue)


def publish(queue: str, payload: dict | str | bytes, headers: dict | None = None) -> None:
    if isinstance(payload, dict):
        body = json.dumps(payload).encode()
    elif isinstance(payload, str):
        body = payload.encode()
    else:
        body = payload
    asyncio.run(_publish(queue, body, headers))


async def _queue_count(queue: str) -> int:
    connection = await aio_pika.connect_robust(RABBIT_URL)
    async with connection:
        channel = await connection.channel()
        declared = await channel.declare_queue(queue, passive=True)
        return declared.declaration_result.message_count or 0


def queue_count(queue: str) -> int:
    return asyncio.run(_queue_count(queue))


async def _drain(queue: str) -> list[bytes]:
    connection = await aio_pika.connect_robust(RABBIT_URL)
    bodies = []
    async with connection:
        channel = await connection.channel()
        declared = await channel.declare_queue(queue, passive=True)
        while True:
            message = await declared.get(no_ack=True, fail=False)
            if message is None:
                return bodies
            bodies.append(message.body)


def drain(queue: str) -> list[bytes]:
    """Remove and return every message of a queue (used to read the dead-letter queues)."""
    return asyncio.run(_drain(queue))


def queue_published(queue: str) -> int:
    """Messages that reached a queue: those still in it plus those delivered from it (management API).

    Dead-lettered messages do not count as 'publish', and the worker may consume them at once."""
    parsed = make_url(RABBIT_URL.replace("amqp://", "postgresql://", 1))
    management = os.getenv("RHYTHM_IT_RABBITMQ_MANAGEMENT_URL", f"http://{parsed.host}:15672")
    response = httpx.get(f"{management}/api/queues/%2F/{queue}", auth=(parsed.username or "guest", parsed.password or "guest"))
    response.raise_for_status()
    body = response.json()
    return int((body.get("message_stats") or {}).get("deliver_get") or 0) + int(body.get("messages") or 0)


async def _purge_all() -> None:
    connection = await aio_pika.connect_robust(RABBIT_URL)
    async with connection:
        channel = await connection.channel()
        for queue in ALL_QUEUES:
            try:
                declared = await channel.declare_queue(queue, passive=True)
                await declared.purge()
            except aio_pika.exceptions.ChannelClosed:
                channel = await connection.channel()


def purge_all() -> None:
    asyncio.run(_purge_all())


# ---------------------------------------------------------------- audio fixtures
def melody(seconds: float = 8.0, gain: float = 0.5) -> np.ndarray:
    notes = [293.66, 329.63, 392.0, 440.0, 523.25, 440.0, 392.0, 329.63]
    note_length = int(0.5 * SAMPLE_RATE)
    total = int(seconds * SAMPLE_RATE)
    out = np.zeros(total)
    envelope = np.minimum(1.0, np.linspace(0, 8, note_length)) * np.exp(-np.linspace(0, 2.5, note_length))
    t = np.arange(note_length) / SAMPLE_RATE
    for index, start in enumerate(range(0, total - note_length + 1, note_length)):
        frequency = notes[index % len(notes)]
        tone = np.sin(2 * np.pi * frequency * t) + 0.4 * np.sin(4 * np.pi * frequency * t) + 0.15 * np.sin(6 * np.pi * frequency * t)
        out[start:start + note_length] = tone * envelope
    return gain * out / np.max(np.abs(out))


class AudioLibrary:
    """Builds the WAV files used by the test cases once per session."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.files: dict[str, bytes] = {}

    def _wav(self, name: str, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
        path = self.directory / name
        sf.write(path, np.clip(samples, -1.0, 1.0), sample_rate, subtype="PCM_16")
        self.files[name] = path.read_bytes()

    def build(self) -> None:
        self._wav("template.wav", melody(8.0))
        self._wav("student.wav", melody(8.0, gain=0.45))
        self._wav("part.wav", melody(8.0)[int(2 * SAMPLE_RATE):int(6.5 * SAMPLE_RATE)])
        self._wav("silent.wav", np.zeros(3 * SAMPLE_RATE))
        clean = melody(8.0)
        rms = float(np.sqrt(np.mean(np.square(clean))))
        self._wav("quiet.wav", clean * (10 ** (-50 / 20)) / rms)
        mostly_silent = np.zeros(10 * SAMPLE_RATE)
        mostly_silent[:SAMPLE_RATE] = melody(1.0)
        self._wav("mostly-silent.wav", mostly_silent)
        self._wav("clipped.wav", melody(8.0) * 4)
        self._wav("long.wav", melody(60.0))
        self._wav("very-long.wav", melody(120.0))
        self.files["fake.wav"] = b"this is a text file, not audio"
        big = self.directory / "big.wav"
        sf.write(big, np.zeros(13_500_000), 44100, subtype="PCM_16")  # about 27 MB > 25 MiB
        self.files["big.wav"] = big.read_bytes()


# ---------------------------------------------------------------- HTTP stub (storage, callback, Gemini)
class StubState:
    def __init__(self, audio: AudioLibrary) -> None:
        self.audio = audio
        self.lock = threading.Lock()
        self.hits: Counter[str] = Counter()
        self.callbacks: list[dict] = []
        self.callback_status = 200
        self.gemini_calls: Counter[str] = Counter()
        self.gemini_modes: dict[str, str] = {}
        self.gemini_default = "ok"

    def reset(self) -> None:
        with self.lock:
            self.hits.clear()
            self.callbacks.clear()
            self.callback_status = 200
            self.gemini_calls.clear()
            self.gemini_modes.clear()
            self.gemini_default = "ok"

    def callbacks_for(self, evaluation_id: str) -> list[dict]:
        with self.lock:
            return [item for item in self.callbacks if item["body"].get("evaluationId") == evaluation_id]


def _make_handler(state: StubState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            return None

        def _send(self, status: int, body: bytes = b"", content_type: str = "application/octet-stream") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            with state.lock:
                state.hits[path] += 1
            if path.startswith("/media/"):
                body = state.audio.files.get(path.removeprefix("/media/"))
                return self._send(200, body, "audio/wav") if body is not None else self._send(404, b"missing")
            if path.startswith("/status/"):
                return self._send(int(path.split("/")[2]), b"stub status")
            return self._send(404, b"missing")

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            path = self.path.split("?", 1)[0]
            if path == "/callback":
                with state.lock:
                    state.callbacks.append({"headers": dict(self.headers.items()), "body": json.loads(raw or b"{}"),
                                            "at": time.monotonic()})
                    status = state.callback_status
                return self._send(status, b"{}", "application/json")
            if ":generateContent" in path:
                return self._gemini()
            return self._send(404, b"missing")

        def _gemini(self) -> None:
            key = self.headers.get("x-goog-api-key", "")
            with state.lock:
                state.gemini_calls[key] += 1
                mode = state.gemini_modes.get(key, state.gemini_default)
            if mode.startswith("slow"):
                time.sleep(float(mode.split(":")[1]))
                mode = "ok"
            if mode == "ok":
                body = {"candidates": [{"content": {"role": "model", "parts": [{"text": FEEDBACK_TEXT}]},
                                        "finishReason": "STOP", "index": 0}]}
                return self._send(200, json.dumps(body).encode(), "application/json")
            code = int(mode)
            status = {401: "UNAUTHENTICATED", 429: "RESOURCE_EXHAUSTED", 503: "UNAVAILABLE"}.get(code, "UNKNOWN")
            body = {"error": {"code": code, "message": f"stub {code}", "status": status}}
            return self._send(code, json.dumps(body).encode(), "application/json")

    return Handler


class StubServer:
    def __init__(self, state: StubState) -> None:
        self.state = state
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> "StubServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()

    def media(self, name: str) -> str:
        return f"{self.base}/media/{name}"

    def status_url(self, code: int, name: str = "a.wav") -> str:
        return f"{self.base}/status/{code}/{name}"

    @property
    def callback_url(self) -> str:
        return f"{self.base}/callback"


# ---------------------------------------------------------------- service process
class RhythmProcess:
    """One `python -m app.main` process (API + worker) with its own settings."""

    def __init__(self, base_env: dict[str, str], log_directory: Path, overrides: dict[str, str] | None = None,
                 name: str = "rhythm") -> None:
        self.port = free_port()
        self.env = {**base_env, **(overrides or {}), "RHYTHM_SERVICE_APP_PORT": str(self.port)}
        self.log_path = log_directory / f"{name}-{self.port}.log"
        self.process: subprocess.Popen | None = None
        self.base = f"http://127.0.0.1:{self.port}"

    def start(self, wait: bool = True) -> "RhythmProcess":
        self.log_file = self.log_path.open("wb")
        self.process = subprocess.Popen([python_executable(), "-m", "app.main"], cwd=PROJECT, env=self.env,
                                        stdout=self.log_file, stderr=subprocess.STDOUT)
        if wait:
            wait_for(self._ready, timeout=120, interval=0.5, message=f"service on port {self.port}")
        return self

    def _ready(self) -> bool:
        if self.process and self.process.poll() is not None:
            raise RuntimeError(f"Rhythm service exited early:\n{self.log()[-3000:]}")
        try:
            return httpx.get(f"{self.base}/health", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    def wait_exit(self, timeout: float = 120) -> int:
        assert self.process is not None
        return self.process.wait(timeout=timeout)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=30)
        if getattr(self, "log_file", None):
            self.log_file.close()

    def log(self) -> str:
        if getattr(self, "log_file", None):
            self.log_file.flush()
        return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""

    def client(self, key: str | None = INTERNAL_KEY) -> httpx.Client:
        headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
        # No keep-alive: while the worker is busy the server may drop an idle connection before reuse.
        return httpx.Client(base_url=self.base, headers=headers, timeout=60,
                            limits=httpx.Limits(max_keepalive_connections=0))


def base_environment(db_url: str, stub: StubServer, work: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("RHYTHM_SERVICE_") and key not in {"GOOGLE_API_KEY", "GEMINI_API_KEY"}}
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "RHYTHM_SERVICE_APP_HOST": "127.0.0.1",
        "RHYTHM_SERVICE_DATABASE_URL": db_url,
        "RHYTHM_SERVICE_RABBITMQ_URL": RABBIT_URL,
        "RHYTHM_SERVICE_RHYTHM_TASK_QUEUE": EVALUATE_QUEUE,
        "RHYTHM_SERVICE_RHYTHM_DEAD_LETTER_QUEUE": EVALUATE_DLQ,
        "RHYTHM_SERVICE_TEMPLATE_TASK_QUEUE": TEMPLATE_QUEUE,
        "RHYTHM_SERVICE_TEMPLATE_DEAD_LETTER_QUEUE": TEMPLATE_DLQ,
        "RHYTHM_SERVICE_ARTIFACT_ROOT": str(work / "artifacts"),
        "RHYTHM_SERVICE_TEMP_DIRECTORY": str(work / "tmp"),
        "RHYTHM_SERVICE_INTERNAL_API_KEY": INTERNAL_KEY,
        "RHYTHM_SERVICE_CALLBACK_SECRET": CALLBACK_SECRET,
        "RHYTHM_SERVICE_CALLBACK_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "RHYTHM_SERVICE_CALLBACK_MAX_RETRIES": "2",
        "RHYTHM_SERVICE_GEMINI_API_KEY": "",
        "RHYTHM_SERVICE_OPENROUTER_API_KEY": "",
        "RHYTHM_SERVICE_GEMINI_MODEL": GEMINI_MODEL,
        "RHYTHM_SERVICE_GEMINI_TIMEOUT_SECONDS": "20",
        "RHYTHM_SERVICE_KEY_ENCRYPTION_SECRET": ENCRYPTION_SECRET,
        "RHYTHM_SERVICE_MAX_FILE_BYTES": "26214400",
        "RHYTHM_SERVICE_DOWNLOAD_TIMEOUT_SECONDS": "60",
        "RHYTHM_SERVICE_MAX_RETRIES": "3",
        "RHYTHM_SERVICE_ALLOWED_HOSTS": "",
        "GOOGLE_GEMINI_BASE_URL": stub.base,
    })
    return env


# ---------------------------------------------------------------- payload builders
def evaluation_task(stub: StubServer, evaluation_id: str, *, student: str = "student.wav",
                    template: str = "template.wav", **extra: Any) -> dict:
    task = {
        "messageId": str(uuid.uuid4()), "evaluationId": evaluation_id,
        "templateUrl": stub.media(template), "studentUrl": stub.media(student),
        "instrumentId": "TRANH", "instrument": dict(TRANH),
    }
    task.update(extra)
    return task


def template_task(stub: StubServer, *, analysis_id: str | None = None, media: str = "template.wav",
                  sheet_music_id: str | None = None, media_file_id: str | None = None, **extra: Any) -> dict:
    task = {
        "messageId": str(uuid.uuid4()), "analysisId": analysis_id or str(uuid.uuid4()),
        "sheetMusicId": sheet_music_id or str(uuid.uuid4()), "mediaFileId": media_file_id or str(uuid.uuid4()),
        "mediaUrl": stub.media(media), "instrumentCode": "TRANH", "instrument": dict(TRANH),
        "requestedAt": "2026-11-19T08:00:00Z",
    }
    task.update(extra)
    return task


def wait_evaluation(client: httpx.Client, evaluation_id: str, statuses: set[str] | None = None,
                    timeout: float = 180) -> dict:
    wanted = statuses or {"completed", "failed", "rejected"}

    def check() -> dict | None:
        response = client.get(f"/api/v1/evaluations/{evaluation_id}")
        if response.status_code == 200 and response.json()["status"] in wanted:
            return response.json()
        return None

    return wait_for(check, timeout=timeout, interval=0.5, message=f"evaluation {evaluation_id}")


def wait_template(client: httpx.Client, analysis_id: str, statuses: set[str] | None = None,
                  timeout: float = 180) -> dict:
    wanted = statuses or {"completed", "failed"}

    def check() -> dict | None:
        response = client.get(f"/api/v1/template-analyses/{analysis_id}")
        if response.status_code == 200 and response.json()["status"] in wanted:
            return response.json()
        return None

    return wait_for(check, timeout=timeout, interval=0.5, message=f"template analysis {analysis_id}")
