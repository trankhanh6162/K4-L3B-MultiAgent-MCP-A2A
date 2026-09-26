from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from student_agent import workflow
from student_agent.config import LLMSettings
from student_agent.contracts import Contracts


class FakeGateway:
    def __init__(self, contracts: Contracts) -> None:
        self.contracts = contracts
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        if tool_name == "get_order" and arguments["order_id"] == "candidate-001":
            raise RuntimeError("order not found")
        suffix = f"{len(self.calls):02d}" + "x" * 20
        data: dict[str, Any] = {"case_id": case_id, **arguments}
        if tool_name == "get_order":
            data["customer_unique_id"] = "customer-001"
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{suffix}",
            "result_hash": "sha256:" + "a" * 64,
            "domain": {
                "get_order": "order",
                "get_order_items": "item",
                "get_product_context": "product",
                "get_shipment_summary": "shipment",
                "get_payment_timeline": "payment",
                "get_refund_timeline": "refund",
                "get_policy": "policy",
                "get_customer_history": "customer",
            }[tool_name],
            "data": data,
        }

    def validate_output(self, output: dict[str, Any], source: str) -> None:
        self.contracts.validate_output(output, source)


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


def test_solve_case_uses_bounded_evidence_plan(monkeypatch: Any) -> None:
    root = Path(__file__).resolve().parents[1]
    gateway = FakeGateway(Contracts(root / "contracts" / "schemas"))
    trace = FakeTrace()
    case = {
        "case_id": "L3B_CASE_001",
        "customer_request": {
            "claimed_order_id": "order-001",
            "claims": [
                {"claim_id": "claim-001-a", "topic": "late_delivery_logistics"},
                {"claim_id": "claim-001-b", "topic": "requested_full_refund"},
            ],
        },
        "candidate_order_ids": ["order-001", "candidate-001"],
        "customer_unique_id_hint": "customer-hint",
        "policy_version": "EC_POLICY_V2",
    }

    monkeypatch.setattr(
        workflow.LLMSettings,
        "load",
        lambda: LLMSettings("test-key", "gpt-4o-mini", None),
    )

    async def fake_generate_output(
        settings: LLMSettings,
        *,
        case: dict[str, Any],
        facts: dict[str, Any],
        output_schema: dict[str, Any],
        shared_schema: dict[str, Any],
    ) -> dict[str, Any]:
        del settings, output_schema, shared_schema
        refs = list(facts["evidence_refs_by_domain"].values())
        return {
            "schema_version": "day09-l3b-output-v2",
            "case_id": case["case_id"],
            "assessment": {
                "primary_issue": "late_delivery_logistics",
                "secondary_issues": ["requested_full_refund"],
                "case_status": "no_action",
                "confidence": 0.8,
            },
            "affected_entities": {
                "order_ids": ["order-001"],
                "item_ids": [],
                "seller_ids": [],
                "payment_references": [],
                "shipment_ids": [],
            },
            "claim_assessments": [
                {
                    "claim_id": claim["claim_id"],
                    "verdict": "unsupported",
                    "confidence": 0.8,
                    "evidence_refs": refs,
                }
                for claim in case["customer_request"]["claims"]
            ],
            "entity_resolution": {
                "status": "resolved",
                "resolved_order_ids": ["order-001"],
                "rejected_candidates": ["candidate-001"],
                "confidence": 0.99,
            },
            "customer_context": {
                "customer_unique_id": "customer-001",
                "related_order_ids": ["order-001"],
            },
            "shipment_analysis": {
                "verdict": "on_time",
                "late_seller_ids": [],
                "timeline_complete": True,
            },
            "payment_analysis": {
                "verdict": "reconciled",
                "captured_total_brl": 10,
                "refunded_total_brl": 0,
                "refundable_total_brl": 0,
            },
            "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
            "evidence_refs": refs,
            "data_conflicts": [],
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": 0,
                "refund_lines": [],
            },
            "resolution_actions": [],
        }

    monkeypatch.setattr(workflow, "generate_output", fake_generate_output)
    output = asyncio.run(workflow.solve_case(case, gateway, trace))

    assert output["case_id"] == case["case_id"]
    assert len(gateway.calls) == 7
    assert all(
        len(claim["evidence_refs"]) < len(output["evidence_refs"])
        for claim in output["claim_assessments"]
    )
    assert {event["event_type"] for event in trace.events} >= {
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "policy_decided",
        "verification_completed",
    }
