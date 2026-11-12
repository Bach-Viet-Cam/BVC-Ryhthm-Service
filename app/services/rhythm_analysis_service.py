import asyncio
from pathlib import Path
from typing import Any

from app.schemas.instrument import InstrumentConfig

from app.evaluation.evaluator import (
    analyze_recording,
    compare_recordings,
    evaluate_against_template,
    evaluate_pair,
    template_analysis_to_recording,
)


class RhythmAnalysisService:
    async def analyze(self, path: Path, instrument_id: str, instrument: InstrumentConfig) -> dict[str, Any]:
        return await asyncio.to_thread(analyze_recording, path, instrument_id, instrument)

    async def analyze_template(self, template: dict[str, Any], instrument_id: str, instrument: InstrumentConfig) -> dict[str, Any]:
        return await asyncio.to_thread(template_analysis_to_recording, template, instrument_id, instrument)

    async def compare(self, teacher: dict[str, Any], student: dict[str, Any], origin_frame: int = 0) -> dict[str, Any]:
        return await asyncio.to_thread(compare_recordings, teacher, student, origin_frame)

    async def evaluate(self, teacher_path: Path, student_path: Path, instrument_id: str, instrument: InstrumentConfig) -> dict[str, Any]:
        return await asyncio.to_thread(evaluate_pair, teacher_path, student_path, instrument_id, instrument)

    async def evaluate_against_template(self, template: dict[str, Any], student_path: Path, instrument_id: str, instrument: InstrumentConfig) -> dict[str, Any]:
        return await asyncio.to_thread(evaluate_against_template, template, student_path, instrument_id, instrument)
