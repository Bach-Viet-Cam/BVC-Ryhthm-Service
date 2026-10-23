import asyncio
import math
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np

from app.evaluation.evaluator import HOP_LENGTH, SAMPLE_RATE, _estimate_tempo, _extract_f0


class TemplateFeatureService:
    async def extract(self, path: Path, instrument: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._extract, path, instrument)

    @staticmethod
    def _extract(path: Path, instrument: dict[str, Any]) -> dict[str, Any]:
        f0 = _extract_f0(path, float(instrument["fmin"]), float(instrument["fmax"]))
        valid = np.isfinite(f0)
        tempo = _estimate_tempo(path)
        return {
            "pitch": {"method": "pyin", "sampleRate": SAMPLE_RATE, "hopLength": HOP_LENGTH,
                      "frameLength": 2048, "center": True,
                      "f0Hz": [float(pitch) if voiced else None for pitch, voiced in zip(f0, valid)],
                      "voicedFlags": valid.tolist()},
            "rhythm": {"tempoBpm": tempo if math.isfinite(tempo) else None},
            "software": {"librosa": version("librosa"), "numpy": version("numpy"), "soundfile": version("soundfile")},
        }
