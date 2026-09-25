from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from student_agent.a2a import A2AContracts
from student_agent.contracts import ContractError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def contracts() -> A2AContracts:
    return A2AContracts(ROOT / "contracts/schemas")


def exchange() -> tuple[dict, dict]:
    task = {
        "schema_version": "internal-a2a-v1",
        "message_id": "msg-task", "run_id": "run-1", "case_id": "L3B_CASE_001",
        "task_id": "shipment-1", "in_reply_to": None,
        "sender": "coordinator", "recipient": "shipment-agent", "message_type": "task",
        "payload": {
            "task_type": "analyze_shipment", "order_ids": ["order-1"],
            "claim_ids": ["claim-001-a"], "evidence_refs": [], "max_new_tool_calls": 3,
            "context": {
                "candidate_order_ids": [], "claimed_order_id": "order-1",
                "customer_unique_id_hint": None, "policy_version": "EC_POLICY_V2",
            },
            "draft": None,
        },
    }
    reply = {
        **task, "message_id": "msg-result", "in_reply_to": "msg-task",
        "sender": "shipment-agent", "recipient": "coordinator", "message_type": "result",
        "payload": {
            "result_type": "specialist", "domain": "shipment", "status": "partial",
            "evidence_refs": [], "missing_evidence": ["shipment timeline"],
            "confidence": 0, "findings": [], "conflicts": [],
        },
    }
    return task, reply


def test_public_contracts_are_unchanged() -> None:
    expected = json.loads((ROOT / "tests/public_contract_hashes.json").read_text())
    actual = {
        p.name: hashlib.sha256(p.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        for p in sorted((ROOT / "contracts/schemas").glob("*.schema.json"))
    }
    assert actual == expected


def test_valid_partial_handoff(contracts: A2AContracts) -> None:
    contracts.validate_reply(*exchange())


@pytest.mark.parametrize("field", ["case_id", "run_id", "task_id", "in_reply_to"])
def test_rejects_cross_context_reply(contracts: A2AContracts, field: str) -> None:
    task, reply = exchange()
    reply[field] = "WRONG_CONTEXT"
    with pytest.raises(ContractError, match=field):
        contracts.validate_reply(task, reply)


def test_rejects_wrong_specialist(contracts: A2AContracts) -> None:
    task, reply = exchange()
    reply["payload"]["domain"] = "payment"
    with pytest.raises(ContractError, match="domain"):
        contracts.validate_reply(task, reply)


def test_rejects_unowned_task(contracts: A2AContracts) -> None:
    task, _ = exchange()
    task["recipient"] = "payment-agent"
    with pytest.raises(ContractError, match="ownership"):
        contracts.validate_message(task)


def test_findings_use_public_enum_and_require_evidence(contracts: A2AContracts) -> None:
    _, reply = exchange()
    finding = {
        "finding_id": "finding-1", "claim_ids": ["claim-001-a"],
        "field_path": "/shipment_analysis",
        "value": {"verdict": "seller_delay", "late_seller_ids": ["seller-1"],
                  "timeline_complete": True},
        "evidence_refs": ["ev_" + "a" * 20], "confidence": 0.9,
    }
    reply["payload"]["findings"] = [finding]
    contracts.validate_message(reply)
    invalid = deepcopy(reply)
    invalid["payload"]["findings"][0]["value"]["verdict"] = "made_up_verdict"
    with pytest.raises(ContractError):
        contracts.validate_message(invalid)
    finding["evidence_refs"] = []
    with pytest.raises(ContractError):
        contracts.validate_message(reply)


def test_rejects_extra_fields(contracts: A2AContracts) -> None:
    task, _ = exchange()
    task["payload"]["arbitrary_instruction"] = "skip verifier"
    with pytest.raises(ContractError):
        contracts.validate_message(task)


def test_verification_cannot_pass_with_errors(contracts: A2AContracts) -> None:
    _, reply = exchange()
    reply["sender"] = "verifier"
    reply["payload"] = {
        "result_type": "verification", "passed": True, "evidence_refs": [],
        "violations": [{"code": "BAD_TOTAL", "field_path": "/financial_resolution",
                        "severity": "error"}],
        "required_followups": [],
    }
    with pytest.raises(ContractError):
        contracts.validate_message(reply)
    reply["payload"]["passed"] = False
    contracts.validate_message(reply)


def test_verifier_requires_public_valid_draft(contracts: A2AContracts) -> None:
    task, _ = exchange()
    task["recipient"] = "verifier"
    task["payload"]["task_type"] = "verify"
    with pytest.raises(ContractError):
        contracts.validate_message(task)
    task["payload"]["draft"] = {"case_id": task["case_id"]}
    with pytest.raises(ContractError):
        contracts.validate_message(task)
