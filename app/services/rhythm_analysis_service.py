import asyncio
from pathlib import Path
from typing import Any

from app.evaluation.evaluator import evaluate_pair


class RhythmAnalysisService:
    async def evaluate(self, teacher_path: Path, student_path: Path, instrument_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(evaluate_pair, teacher_path, student_path, instrument_id)
