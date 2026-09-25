from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

TEAM_KEY_PATTERN = re.compile(r"^sk-team-[A-Za-z0-9_-]{16,128}$")


@dataclass(frozen=True)
class Settings:
    competition_api_url: str
    team_api_key: str
    mcp_endpoint: str
    root: Path

    @classmethod
    def load(cls, root: Path | None = None) -> Settings:
        resolved_root = (root or Path.cwd()).resolve()
        load_dotenv(resolved_root / ".env")
        api_url = os.getenv("COMPETITION_API_URL", "").strip().rstrip("/")
        team_key = os.getenv("COMPETITION_TEAM_API_KEY", "").strip()
        mcp_endpoint = os.getenv("MCP_ENDPOINT", "").strip()
        errors: list[str] = []
        if not api_url.startswith(("http://", "https://")):
            errors.append("COMPETITION_API_URL must be an absolute HTTP(S) URL")
        if not TEAM_KEY_PATTERN.fullmatch(team_key):
            errors.append("COMPETITION_TEAM_API_KEY must use the sk-team-... format")
        if not mcp_endpoint.startswith(("http://", "https://")):
            errors.append("MCP_ENDPOINT must be an absolute HTTP(S) URL")
        if errors:
            raise ValueError("; ".join(errors))
        return cls(api_url, team_key, mcp_endpoint, resolved_root)


@dataclass(frozen=True)
class LLMSettings:
    api_key: str
    model: str
    base_url: str | None

    @classmethod
    def load(cls, root: Path | None = None) -> LLMSettings:
        resolved_root = (root or Path.cwd()).resolve()
        load_dotenv(resolved_root / ".env", override=True)
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
        if not api_key or api_key == "sk-replace_me":
            raise ValueError("OPENAI_API_KEY is missing or still uses the placeholder")
        if model != "gpt-4o-mini":
            raise ValueError("OPENAI_MODEL must be gpt-4o-mini for this workflow")
        if base_url is not None and not base_url.startswith(("http://", "https://")):
            raise ValueError("OPENAI_BASE_URL must be an HTTP(S) URL")
        return cls(api_key, model, base_url)
