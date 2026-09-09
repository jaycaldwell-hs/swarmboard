from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_RUN_MAX_TOKENS = 200_000
DEFAULT_AGENT_OUTPUT_TOKENS = 4_096
DEFAULT_PERSONA_DIR = Path(__file__).resolve().parent.parent / "persona 1"


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    openai_compat_base_url: str = "https://openrouter.ai/api/v1"
    idle_seconds: float = 45.0
    dormant_seconds: float = 300.0
    model_timeout_seconds: float = 60.0
    scheduler_poll_seconds: float = 0.35
    context_post_limit: int = 40
    persona_dir: Path = DEFAULT_PERSONA_DIR

    @classmethod
    def from_env(cls, *, database_url: str | None = None) -> "Settings":
        load_dotenv(override=False)
        db_path = Path(os.getenv("SWARMBOARD_DB_PATH", "./swarmboard.db")).expanduser()
        resolved_url = database_url or f"sqlite:///{db_path.resolve()}"
        return cls(
            database_url=resolved_url,
            openai_compat_base_url=os.getenv(
                "OPENAI_COMPAT_BASE_URL", "https://openrouter.ai/api/v1"
            ).rstrip("/"),
            idle_seconds=float(os.getenv("SWARMBOARD_IDLE_SECONDS", "45")),
            dormant_seconds=float(os.getenv("SWARMBOARD_DORMANT_SECONDS", "300")),
            model_timeout_seconds=float(os.getenv("SWARMBOARD_MODEL_TIMEOUT_SECONDS", "60")),
            persona_dir=Path(os.getenv("SWARMBOARD_PERSONA_DIR", str(DEFAULT_PERSONA_DIR))).expanduser().resolve(),
        )
