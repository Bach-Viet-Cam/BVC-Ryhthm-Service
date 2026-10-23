import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.config import Settings


ROOT = Path(__file__).resolve().parents[1]


def test_upgrade_preserves_evaluations_and_downgrade_restores_previous_schema(tmp_path):
    url = "sqlite+pysqlite:///" + (tmp_path / "migration.sqlite").as_posix()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "20260926_0001")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO rhythm_evaluations(evaluation_id,status,template_url,student_url,instrument_id,created_at) VALUES ('existing','completed','https://example.com/t.wav','https://example.com/s.wav','dan_bau',CURRENT_TIMESTAMP)"))
    command.upgrade(config, "head")
    inspector = inspect(engine)
    assert inspector.has_table("template_analyses")
    assert "student_quality_report" in {column["name"] for column in inspector.get_columns("rhythm_evaluations")}
    assert {"sheet_music_id", "media_file_id", "artifact_key"} <= {column["name"] for column in inspector.get_columns("template_analyses")}
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from app.database import Base
    with engine.connect() as schema_connection:
        assert compare_metadata(MigrationContext.configure(schema_connection), Base.metadata) == []
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT status FROM rhythm_evaluations WHERE evaluation_id='existing'")) == "completed"
    command.downgrade(config, "20260926_0001")
    assert not inspect(engine).has_table("template_analyses")
    assert "student_quality_report" not in {column["name"] for column in inspect(engine).get_columns("rhythm_evaluations")}
    command.upgrade(config, "head")
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_failure", [True, False])
async def test_worker_failure_or_unexpected_exit_stops_http_server(monkeypatch, worker_failure):
    from app import main
    server_closed = asyncio.Event()
    class Server:
        def __init__(self, config):
            self.should_exit = False
        async def serve(self):
            try:
                while not self.should_exit:
                    await asyncio.sleep(0)
            finally:
                server_closed.set()
    async def worker():
        await asyncio.sleep(0)
        if worker_failure:
            raise RuntimeError("Worker gặp lỗi.")
    monkeypatch.setattr(main, "run_worker", worker)
    monkeypatch.setattr(main, "initialize_database", lambda: None)
    monkeypatch.setattr(main.uvicorn, "Server", Server)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(main.run_service(), timeout=1)
    assert server_closed.is_set()


@pytest.mark.asyncio
async def test_http_exit_cancels_worker(monkeypatch):
    from app import main
    started, closed = asyncio.Event(), asyncio.Event()
    class Server:
        def __init__(self, config):
            self.should_exit = False
        async def serve(self):
            await started.wait()
    async def worker():
        started.set()
        try:
            await asyncio.Future()
        finally:
            closed.set()
    monkeypatch.setattr(main, "run_worker", worker)
    monkeypatch.setattr(main, "initialize_database", lambda: None)
    monkeypatch.setattr(main.uvicorn, "Server", Server)
    await asyncio.wait_for(main.run_service(), timeout=1)
    assert closed.is_set()


@pytest.mark.parametrize("overrides", [
    {"template_task_queue": ""},
    {"artifact_root": "   "},
    {"template_task_queue": "rhythm.evaluate.v1"},
    {"template_dead_letter_queue": "rhythm.template.analyze.v1"},
    {"max_retries": -1},
])
def test_invalid_worker_configuration_is_rejected(overrides):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **overrides)



def test_postgresql_migration_sql_renders_without_connecting_to_database():
    from io import StringIO
    output = StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://test:test@localhost/test")
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE template_analyses" in sql
    assert "ADD COLUMN student_quality_report JSON" in sql
    assert "sheet_music_id VARCHAR(36) NOT NULL" in sql
    assert "20261023_0002" in sql
