from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts

    async def list_tools(self) -> list[str]:
        return sorted(tool["name"] for tool in await self.discover_tools())

    async def discover_tools(self) -> list[dict[str, Any]]:
        """Preserve argument contracts and follow discovery pagination."""
        descriptors = []
        cursor = None
        seen = set()
        while True:
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            response = await self._session.list_tools(params=params)
            descriptors.extend(
                {"name": tool.name, "description": tool.description,
                 "inputSchema": tool.input_schema}
                for tool in response.tools
            )
            cursor = response.next_cursor
            if not cursor:
                return descriptors
            if cursor in seen:
                raise RuntimeError("Repeated MCP discovery cursor")
            seen.add(cursor)

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        result = await self._session.call_tool(tool_name, arguments=payload)
        if not isinstance(result, types.CallToolResult):
            raise RuntimeError(f"MCP tool {tool_name} did not return a completed tool result")
        if result.is_error:
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
        return evidence


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
