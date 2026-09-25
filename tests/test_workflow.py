from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import ToolSpec
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


class FakeGateway:
    def __init__(self) -> None:
        names = {
            "lookup_order": ("order", "order_id"),
            "get_customer_history": ("customer history", "customer_unique_id"),
            "get_order_items": ("item product seller", "order_id"),
            "get_shipment": ("shipment delivery logistics", "order_id"),
            "get_payments": ("payment charge capture", "order_id"),
            "get_refunds": ("refund", "order_id"),
            "get_policy": ("policy eligibility rule", "policy_version"),
        }
        self.tools = {
            name: ToolSpec(
                name,
                description,
                {
                    "type": "object",
                    "properties": {"case_id": {"type": "string"}, argument: {"type": "string"}},
                    "required": ["case_id", argument],
                },
            )
            for name, (description, argument) in names.items()
        }
        self.count = 0

    async def discover(self) -> dict[str, ToolSpec]:
        return self.tools

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        self.count += 1
        order_id = arguments.get("order_id")
        if tool_name == "lookup_order":
            data = (
                {"order_id": order_id, "exists": False}
                if str(order_id).startswith("candidate-")
                else {
                    "order_id": order_id,
                    "exists": True,
                    "customer_unique_id": "customer-597dc70ef07b",
                    "status": "delivered",
                }
            )
            domain = "order"
        elif tool_name == "get_customer_history":
            data, domain = {"related_order_ids": ["old-order"]}, "customer"
        elif tool_name == "get_order_items":
            data, domain = {"item_id": "item-1", "seller_id": "seller-1"}, "item"
        elif tool_name == "get_shipment":
            data, domain = {"shipment_id": "ship-1", "verdict": "logistics_delay"}, "shipment"
        elif tool_name == "get_payments":
            data, domain = {"payment_id": "pay-1", "captured_total_brl": 100}, "payment"
        elif tool_name == "get_refunds":
            data, domain = {"refunded_total_brl": 0}, "refund"
        else:
            data, domain = {"eligible": True, "version": "EC_POLICY_V2"}, "policy"
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{self.count:020d}",
            "result_hash": "sha256:" + "a" * 64,
            "domain": domain,
            "data": data,
        }


def test_workflow_produces_valid_output_and_trace(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    case = json.loads((root / "inputs" / "L3B_CASE_001.json").read_text(encoding="utf-8"))
    gateway = FakeGateway()

    output = asyncio.run(solve_case(case, gateway, trace))  # type: ignore[arg-type]

    contracts.validate_output(output, "fake output")
    assert output["entity_resolution"]["status"] == "resolved"
    assert output["financial_resolution"]["recommended_refund_brl"] == 100
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert {"task_assigned", "tool_result_consumed", "handoff", "verification_completed"} <= {
        event["event_type"] for event in events
    }


def test_tool_selection_uses_real_gateway_names() -> None:
    from student_agent.workflow import _select_tool

    def spec(name: str, argument: str) -> ToolSpec:
        return ToolSpec(
            name,
            "Return rows belonging to one order.",
            {"properties": {"case_id": {}, argument: {}}, "required": ["case_id", argument]},
        )

    tools = {
        name: spec(name, "order_id")
        for name in (
            "get_order",
            "get_order_items",
            "get_order_payments",
            "get_payment_timeline",
            "get_product_context",
            "get_refund_timeline",
            "get_sellers",
            "get_shipment_summary",
        )
    }
    context = {"order_id": "o-1"}
    expected = {
        "order": "get_order",
        "item": "get_order_items",
        "shipment": "get_shipment_summary",
        "payment": "get_payment_timeline",
        "refund": "get_refund_timeline",
    }
    for domain, name in expected.items():
        selected = _select_tool(tools, domain, context)
        assert selected is not None and selected[0].name == name
    # Without the preferred names, the fallback still must not pick a seller tool for orders.
    fallback = {
        "get_sellers": tools["get_sellers"],
        "fetch_order_row": spec("fetch_order_row", "order_id"),
    }
    selected = _select_tool(fallback, "order", context)
    assert selected is not None and selected[0].name == "fetch_order_row"
