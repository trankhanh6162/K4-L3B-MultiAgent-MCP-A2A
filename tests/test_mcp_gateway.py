from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp import ClientSession, types

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway

ROOT = Path(__file__).resolve().parents[1]


def gateway(session):
    return EvidenceGateway(session, Contracts(ROOT / "contracts/schemas"))


def test_discovery_uses_sdk_v2_params_and_snake_case_fields():
    # A real method signature catches cursor= regressions, unlike a generic mock.
    class Session:
        list_tools = ClientSession.list_tools

        def __init__(self):
            self.requests = []

        async def send_request(self, request, result_type):
            self.requests.append(request)
            return types.ListToolsResult(
                tools=[types.Tool(name=f"tool-{len(self.requests)}", input_schema={})],
                next_cursor="page-2" if len(self.requests) == 1 else None,
            )

        def _absorb_tool_listing(self, result, *, complete):
            return result

    session = Session()
    descriptors = asyncio.run(gateway(session).discover_tools())
    assert session.requests[0].params is None
    assert session.requests[1].params.cursor == "page-2"
    assert [t["name"] for t in descriptors] == ["tool-1", "tool-2"]
    assert descriptors[0]["inputSchema"] == {}


def test_discovery_rejects_repeated_cursor():
    session = AsyncMock()
    session.list_tools.return_value = types.ListToolsResult(tools=[], next_cursor="repeat")
    with pytest.raises(RuntimeError, match="Repeated"):
        asyncio.run(gateway(session).discover_tools())


def test_call_reads_sdk_v2_structured_content_without_changing_evidence():
    evidence = {
        "schema_version": "day09-mcp-evidence-v1", "domain": "order",
        "evidence_ref": "ev_" + "a" * 20, "result_hash": "sha256:" + "b" * 64,
        "data": {"order_id": "order-1"},
    }
    session = AsyncMock()
    session.call_tool.return_value = types.CallToolResult(
        content=[], structured_content=evidence, is_error=False,
    )
    result = asyncio.run(gateway(session).call(
        "get_order", case_id="CASE_001", order_id="order-1",
    ))
    assert result == evidence
    session.call_tool.assert_awaited_once_with(
        "get_order", arguments={"case_id": "CASE_001", "order_id": "order-1"},
    )


def test_call_reads_sdk_v2_error_flag():
    session = AsyncMock()
    session.call_tool.return_value = types.CallToolResult(
        content=[types.TextContent(type="text", text="denied")], is_error=True,
    )
    with pytest.raises(RuntimeError, match="denied"):
        asyncio.run(gateway(session).call("get_order", case_id="CASE_001"))
