from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from student_agent.contracts import ContractError, Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import CaseWorkflow


async def solve_case(case, gateway, trace):
    """Explicit offline baseline; production solve_case requires an LLM client."""
    return await CaseWorkflow(case, gateway, trace).run()


ROOT = Path(__file__).resolve().parents[1]


class Gateway:
    def __init__(self, unavailable=False):
        self.calls = []
        self.unavailable = unavailable

    async def discover_tools(self):
        return [
            {
                "name": name,
                "inputSchema": {
                    "type": "object",
                    "required": ["case_id", argument],
                    "properties": {"case_id": {"type": "string"}, argument: {"type": "string"}},
                    "additionalProperties": False,
                },
            }
            for name, argument in [
                ("get_order", "order_id"),
                ("get_customer_history", "customer_unique_id"),
                ("get_order_items", "order_id"),
                ("get_shipment_summary", "order_id"),
                ("get_payment_timeline", "order_id"),
                ("get_refund_timeline", "order_id"),
                ("get_policy", "policy_version"),
                ("get_product_context", "order_id"),
            ]
        ]

    async def call(self, name, **arguments):
        self.calls.append((name, arguments))
        if self.unavailable:
            raise RuntimeError("Tool unavailable")
        oid = arguments.get("order_id", "order-1")
        domain, data = {
            "get_order": (
                "order",
                {"order_id": oid, "customer_unique_id": "customer-1", "order_total_brl": 100},
            ),
            "get_customer_history": (
                "customer",
                {"customer_unique_id": "customer-1", "orders": [{"order_id": "order-1"}]},
            ),
            "get_order_items": (
                "item",
                [
                    {
                        "order_id": oid,
                        "order_item_id": "item-1",
                        "seller_id": "seller-1",
                        "product_id": "product-1",
                        "price": "90",
                        "freight_value": "10",
                    }
                ],
            ),
            "get_product_context": (
                "product",
                [{"product_id": "product-1", "order_item_id": "item-1"}],
            ),
            "get_shipment_summary": (
                "shipment",
                {
                    "order_id": oid,
                    "delivered_carrier_at": "2018-01-01T12:00:00Z",
                    "delivered_customer_at": "2018-01-02T12:00:00Z",
                    "estimated_delivery_at": "2018-01-03T12:00:00Z",
                    "shipping_limits": [
                        {"seller_id": "seller-1", "shipping_limit_at": "2018-01-02T00:00:00Z"}
                    ],
                    "events": [],
                },
            ),
            "get_payment_timeline": (
                "payment",
                {
                    "order_id": oid,
                    "payments": [],
                    "events": [
                        {
                            "order_id": oid,
                            "event_type": "captured",
                            "status": "confirmed",
                            "event_at": "2018-01-01T00:00:00Z",
                            "amount_brl": "120",
                        }
                    ],
                },
            ),
            "get_refund_timeline": ("refund", {"order_id": oid, "events": []}),
            "get_policy": ("policy", {"policy_version": "EC_POLICY_V2"}),
        }[name]
        if name == "get_order" and oid == "wrong-order":
            data = {"found": False}
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "domain": domain,
            "data": data,
            "evidence_ref": "ev_" + str(len(self.calls)).zfill(20),
            "result_hash": "sha256:" + "0" * 64,
        }


@pytest.fixture
def setup(tmp_path):
    case = {
        "case_id": "TEST_CASE_001",
        "policy_version": "EC_POLICY_V2",
        "customer_unique_id_hint": "customer-1",
        "candidate_order_ids": ["order-1", "wrong-order"],
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [{"claim_id": "claim-1", "topic": "payment_mismatch"}],
        },
        "investigation_scope": {
            "include_customer_history": True,
            "include_product_context": True,
            "require_independent_verification": True,
        },
    }
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts/schemas"))
    return case, trace


def test_workflow_runs_handoffs_and_verifies_without_mutating_input(setup):
    case, trace = setup
    original = copy.deepcopy(case)
    gateway = Gateway()
    output = asyncio.run(solve_case(case, gateway, trace))
    assert case == original
    trace.contracts.validate_output(output, "test")
    assert output["entity_resolution"]["resolved_order_ids"] == ["order-1"]
    assert output["entity_resolution"]["rejected_candidates"] == ["wrong-order"]
    assert output["assessment"]["primary_issue"] == "payment_mismatch"
    assert output["payment_analysis"]["captured_total_brl"] == 120
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["claim_assessments"][0]["verdict"] == "supported"
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    assert sum(e["event_type"] == "task_assigned" for e in events) == 6
    assert sum(e["event_type"] == "handoff" for e in events) == 6
    assert any(e["event_type"] == "verification_completed" for e in events)
    assert all(args["case_id"] == case["case_id"] for _, args in gateway.calls)
    assert len(gateway.calls) <= 20


def test_unavailable_evidence_is_not_fabricated(setup):
    case, trace = setup
    output = asyncio.run(solve_case(case, Gateway(unavailable=True), trace))
    assert output["entity_resolution"]["status"] == "ambiguous"
    assert output["evidence_refs"] == []
    assert output["payment_analysis"]["captured_total_brl"] is None
    assert output["assessment"]["case_status"] == "needs_investigation"


def test_cache_and_tool_permission(setup):
    case, trace = setup
    gateway = Gateway()
    workflow = CaseWorkflow(case, gateway, trace)

    async def run():
        workflow.deadline = asyncio.get_running_loop().time() + 30
        workflow.tools = {t["name"]: t for t in await gateway.discover_tools()}
        first = await workflow.read("entity-agent", "order", order_id="order-1")
        workflow.orders = ["order-1"]
        second = await workflow.read("order-agent", "order", order_id="order-1")
        assert first == second
        with pytest.raises(PermissionError):
            await workflow.read("shipment-agent", "payment", order_id="order-1")

    asyncio.run(run())
    assert len(gateway.calls) == 1


def test_unknown_ref_fails_independent_verification(setup):
    case, trace = setup
    workflow = CaseWorkflow(case, Gateway(), trace)
    output = asyncio.run(workflow.run())
    output["evidence_refs"].append("ev_" + "x" * 20)
    report = asyncio.run(workflow.verify(output))
    assert report["passed"] is False
    assert report["violations"][0]["code"] == "UNKNOWN_EVIDENCE"


def test_invalid_envelope_is_not_swallowed(setup):
    class InvalidGateway(Gateway):
        async def call(self, name, **arguments):
            return {"data": {}}

    case, trace = setup
    with pytest.raises(ContractError):
        asyncio.run(solve_case(case, InvalidGateway(), trace))
