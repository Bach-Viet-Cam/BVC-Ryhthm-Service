"""Fixtures for the Rhythm integration tests. Run them with RHYTHM_IT=1 (see README.md in this folder)."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from . import support

INTEGRATION_DIR = Path(__file__).parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.getenv("RHYTHM_IT") == "1":
        return
    skip = pytest.mark.skip(reason="Integration tests need RHYTHM_IT=1, PostgreSQL and RabbitMQ.")
    for item in items:
        if INTEGRATION_DIR in Path(str(item.fspath)).parents:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def isolated_process_gate():
    """The unit-test gate fixture is not needed here: the service runs in its own process."""
    yield


@pytest.fixture(scope="session")
def work_dir() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="rhythm-it-"))
    print(f"\nRhythm integration work directory: {directory}")
    yield directory
    if os.getenv("RHYTHM_IT_KEEP") != "1":  # keep the service logs for debugging
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(scope="session")
def db_url() -> str:
    url = support.database_url()
    support.reset_database(url)
    return url


@pytest.fixture(scope="session")
def audio(work_dir: Path) -> support.AudioLibrary:
    library = support.AudioLibrary(work_dir / "media")
    library.build()
    return library


@pytest.fixture(scope="session")
def stub(audio: support.AudioLibrary) -> support.StubServer:
    server = support.StubServer(support.StubState(audio)).start()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def base_env(db_url: str, stub: support.StubServer, work_dir: Path) -> dict[str, str]:
    return support.base_environment(db_url, stub, work_dir)


class SharedService:
    """The service with the default settings, shared by most tests and paused while a custom one runs."""

    def __init__(self, base_env: dict[str, str], work_dir: Path) -> None:
        self.process = support.RhythmProcess(base_env, work_dir, name="shared")
        self.running = False

    def ensure(self) -> support.RhythmProcess:
        if not self.running:
            self.process.start()
            self.running = True
        return self.process

    def pause(self) -> None:
        if self.running:
            self.process.stop()
            self.running = False


@pytest.fixture(scope="session")
def shared(base_env: dict[str, str], work_dir: Path) -> SharedService:
    service = SharedService(base_env, work_dir)
    service.ensure()  # applies the migrations once
    support.purge_all()
    yield service
    service.pause()


@pytest.fixture()
def rhythm(shared: SharedService) -> support.RhythmProcess:
    return shared.ensure()


@pytest.fixture()
def api(rhythm: support.RhythmProcess):
    with rhythm.client() as client:
        yield client


@pytest.fixture(autouse=True)
def clean_stub(request: pytest.FixtureRequest):
    if "stub" in request.fixturenames:
        request.getfixturevalue("stub").state.reset()
    yield


@pytest.fixture()
def spawn(base_env: dict[str, str], work_dir: Path, shared: SharedService):
    """Start a service process with changed settings (the 'restart with new settings' steps).

    The shared service is stopped first so that only one worker consumes the queues."""
    started: list[support.RhythmProcess] = []

    def factory(overrides: dict[str, str], *, wait: bool = True) -> support.RhythmProcess:
        shared.pause()
        process = support.RhythmProcess(base_env, work_dir, overrides, name="custom")
        started.append(process)
        return process.start(wait=wait)

    yield factory
    for process in started:
        process.stop()
