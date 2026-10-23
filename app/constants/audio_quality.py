"""Source-controlled recording quality policy; bump its version when behavior changes."""

from typing import Final

POLICY_VERSION: Final[str] = "audio-quality-v1"
LOW_LEVEL_DBFS: Final[float] = -45.0
SILENCE_DBFS: Final[float] = -60.0
SILENCE_RATIO_WARN: Final[float] = 0.80
CLIP_AMPLITUDE: Final[float] = 0.999
CLIP_RATIO_WARN: Final[float] = 0.01
FRAME_DURATION_MS: Final[int] = 50
