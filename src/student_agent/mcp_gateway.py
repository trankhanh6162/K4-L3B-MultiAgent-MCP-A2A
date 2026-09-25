from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._tools: dict[str, ToolSpec] | None = None
        self._cache: dict[str, dict[str, Any]] = {}

    async def list_tools(self) -> list[str]:
        await self.discover()
        return sorted(self._tools or {})

    async def discover(self) -> dict[str, ToolSpec]:
        if self._tools is None:
            response = await self._session.list_tools()
            self._tools = {
                tool.name: ToolSpec(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=getattr(tool, "inputSchema", None)
                    or getattr(tool, "input_schema", None)
                    or {},
                )
                for tool in response.tools
            }
        return self._tools

    async def find_tool(self, domain: str, required_arguments: set[str]) -> ToolSpec | None:
        tools = await self.discover()
        domain_tokens = {
            "order": ("order", "pedido"),
            "customer": ("customer", "history", "cliente"),
            "item": ("item", "product", "seller"),
            "shipment": ("shipment", "shipping", "delivery", "freight", "logistics"),
            "payment": ("payment", "charge", "capture"),
            "refund": ("refund", "reimburse"),
            "policy": ("policy", "eligibility", "rule"),
        }.get(domain, (domain,))
        ranked: list[tuple[int, ToolSpec]] = []
        for spec in tools.values():
            haystack = f"{spec.name} {spec.description}".lower()
            properties = set(spec.input_schema.get("properties", {}))
            required = set(spec.input_schema.get("required", [])) - {"case_id"}
            if not required.issubset(required_arguments):
                continue
            score = sum(3 for token in domain_tokens if token in haystack)
            score += len(properties & required_arguments)
            if score:
                ranked.append((score, spec))
        return max(ranked, key=lambda item: (item[0], item[1].name))[1] if ranked else None

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        cache_key = json.dumps([case_id, tool_name, payload], sort_keys=True, separators=(",", ":"))
        if cache_key in self._cache:
            return self._cache[cache_key]
        result = None
        for attempt in range(2):
            try:
                result = await self._session.call_tool(tool_name, arguments=payload)
                break
            except (TimeoutError, OSError):
                if attempt:
                    raise
                await asyncio.sleep(0)
        assert result is not None
        is_error = getattr(result, "is_error", getattr(result, "isError", False))
        if is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        self._cache[cache_key] = evidence
        return evidence


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
