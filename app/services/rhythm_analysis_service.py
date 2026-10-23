import asyncio
from pathlib import Path
from typing import Any

from app.evaluation.evaluator import (
    analyze_recording,
    compare_recordings,
    evaluate_against_template,
    evaluate_pair,
    template_analysis_to_recording,
)


class RhythmAnalysisService:
    async def analyze(self, path: Path, instrument_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(analyze_recording, path, instrument_id)

    async def analyze_template(self, template: dict[str, Any], instrument_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(template_analysis_to_recording, template, instrument_id)

    async def compare(self, teacher: dict[str, Any], student: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(compare_recordings, teacher, student)

    async def evaluate(self, teacher_path: Path, student_path: Path, instrument_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(evaluate_pair, teacher_path, student_path, instrument_id)

    async def evaluate_against_template(self, template: dict[str, Any], student_path: Path, instrument_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(evaluate_against_template, template, student_path, instrument_id)
