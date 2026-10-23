import asyncio

import pytest

from app.services.job_execution_service import JobExecutionService


@pytest.fixture(autouse=True)
def isolated_process_gate(monkeypatch):
    # Each test owns an event loop; production runs one loop for the whole service.
    monkeypatch.setattr(JobExecutionService, "lock", asyncio.Lock())
