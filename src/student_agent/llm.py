from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from .config import Settings


class StructuredLLM:
    """Optional interpretation helper. Business facts and money never originate here."""

    def __init__(self, settings: Settings) -> None:
        self._model = settings.openrouter_model
        self._temperature = settings.openrouter_temperature
        self._client = AsyncOpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            timeout=30.0,
            max_retries=1,
        )

    async def complete_json(self, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.chat.completions.create(
            model=self._model,
            temperature=self._temperature,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        content = response.choices[0].message.content
        value = json.loads(content or "{}")
        if not isinstance(value, dict):
            raise ValueError("model response must be a JSON object")
        return value
