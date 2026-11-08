import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

# Resolve the effective planner mode once so every os.getenv("AGENT_MODE")
# reader sees the same value: an explicit llm without a complete LLM_* triple
# degrades to rules at startup (health and the UI badge stay honest); an
# explicit llm/rules wins when valid; otherwise a complete LLM_* triple
# enables llm mode and anything else falls back to rules.
_mode = os.environ.get("AGENT_MODE", "").strip().lower()
_llm_ready = all(os.environ.get(key, "").strip() for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"))
if _mode == "llm" and not _llm_ready:
    os.environ["AGENT_MODE"] = "rules"
elif _mode not in {"rules", "llm"}:
    os.environ["AGENT_MODE"] = "llm" if _llm_ready else "rules"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")
    data_dir: Path = PROJECT_ROOT / "data"
    source_root: Path = PROJECT_ROOT.parent
    agent_mode: Literal["rules", "llm"] = "rules"
    llm_base_url: str = ""
    llm_model: str = ""
    query_timeout_seconds: float = 45
    run_timeout_seconds: float = 120
    allowed_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    @model_validator(mode="after")
    def absolute_paths(self):
        if not self.data_dir.is_absolute():
            self.data_dir=(PROJECT_ROOT/self.data_dir).resolve()
        if not self.source_root.is_absolute():
            self.source_root=(PROJECT_ROOT/self.source_root).resolve()
        return self


settings = Settings()
