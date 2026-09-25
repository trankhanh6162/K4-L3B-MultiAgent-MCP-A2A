from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway


def evidence() -> dict[str, object]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_12345678901234567890",
        "result_hash": "sha256:" + "a" * 64,
        "domain": "order",
        "data": {"order_id": "order-1"},
    }


class FakeSession:
    def __init__(self, result: object) -> None:
        self.result = result

    async def call_tool(self, _name: str, *, arguments: dict[str, str]) -> object:
        return self.result


@pytest.mark.parametrize("error_field", ["is_error", "isError"])
def test_gateway_accepts_both_mcp_error_field_names(
    tmp_path: Path, error_field: str
) -> None:
    root = Path(__file__).resolve().parents[1]
    result = SimpleNamespace(
        content=[SimpleNamespace(text=json.dumps(evidence()))],
        structured_content=evidence(),
    )
    setattr(result, error_field, False)
    gateway = EvidenceGateway(
        FakeSession(result),  # type: ignore[arg-type]
        Contracts(root / "contracts" / "schemas"),
    )

    import asyncio

    actual = asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="order-1"))
    assert actual["domain"] == "order"
