"""Source-controlled LLM provider priority, highest first. A 5xx from one provider moves on to the next."""

from typing import Final

PROVIDER_ORDER: Final[tuple[str, ...]] = ("gemini", "openrouter")
