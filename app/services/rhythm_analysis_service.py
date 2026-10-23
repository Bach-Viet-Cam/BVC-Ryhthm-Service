import asyncio
from pathlib import Path
from typing import Any

from app.evaluation.evaluator import evaluate_against_template, evaluate_pair


class RhythmAnalysisService:
    async def evaluate(self, teacher_path: Path, student_path: Path, instrument_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(evaluate_pair, teacher_path, student_path, instrument_id)

    async def evaluate_against_template(self, template: dict[str, Any], student_path: Path, instrument_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(evaluate_against_template, template, student_path, instrument_id)
