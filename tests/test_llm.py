import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from student_agent.contracts import ContractError, Contracts
from student_agent.llm import OpenAIReviewer, validate_review
from student_agent.trace import TraceWriter
from student_agent.workflow import CaseWorkflow


def review_result(case_id="CASE_001", refs=None):
    return {
        "case_id": case_id,
        "proposal_valid": True,
        "verdict": "approve",
        "confidence": 0.8,
        "evidence_refs": refs or [],
        "concern_codes": [],
        "proposal_error_codes": [],
        "selected_primary_issue": None,
        "investigation_order": "payment_first",
    }


def test_openai_request_uses_requested_model_and_strict_output():
    result = review_result()
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(
                    return_value=SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                finish_reason="stop",
                                message=SimpleNamespace(refusal=None, content=json.dumps(result)),
                            )
                        ],
                        model="gpt-4o-mini-2024-07-18",
                        usage=SimpleNamespace(prompt_tokens=20, completion_tokens=30),
                    )
                )
            )
        )
    )
    reviewer = OpenAIReviewer("test-only-key", client=client)
    returned = asyncio.run(
        reviewer.review(
            role="coordinator",
            case={"case_id": "CASE_001"},
            proposal={},
            evidence=[],
            allowed_issues=[],
        )
    )
    assert returned == result
    args = client.chat.completions.create.call_args.kwargs
    assert args["model"] == "gpt-4o-mini"
    assert args["response_format"]["json_schema"]["strict"] is True
    evidence_schema = args["response_format"]["json_schema"]["schema"]["properties"][
        "evidence_refs"
    ]
    assert evidence_schema["maxItems"] == 30
    assert "uniqueItems" not in evidence_schema
    assert args["store"] is False
    assert "test-only-key" not in json.dumps(args)


@pytest.mark.parametrize(
    "field,value",
    [
        ("case_id", "FOREIGN_CASE"),
        ("evidence_refs", ["ev_foreign"]),
        ("confidence", 1.1),
        ("selected_primary_issue", "duplicate_charge"),
    ],
)
def test_rejects_unsupported_llm_results(field, value):
    result = review_result()
    result[field] = value
    with pytest.raises(ContractError):
        validate_review(
            result, case_id="CASE_001", role="payment-agent", refs=set(), allowed_issues=[]
        )


def test_normalizes_duplicate_llm_evidence_refs():
    result = review_result(refs=["ev_known", "ev_known"])
    validate_review(
        result,
        case_id="CASE_001",
        role="payment-agent",
        refs={"ev_known"},
        allowed_issues=[],
    )
    assert result["evidence_refs"] == ["ev_known"]


def test_missing_key_is_actionable():
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIReviewer("")


class ReviewModel:
    model = "gpt-4o-mini"
    last_usage = {"input_tokens": 10, "output_tokens": 10}

    def __init__(self, reject=None):
        self.calls = []
        self.reject = reject

    async def review(self, *, role, case, proposal, evidence, allowed_issues):
        self.calls.append(role)
        result = review_result(case["case_id"], [e["evidence_ref"] for e in evidence])
        if role == self.reject:
            result.update(
                proposal_valid=False,
                verdict="needs_investigation",
                concern_codes=["PROPOSAL_ERROR"],
                proposal_error_codes=["PROPOSAL_ERROR"],
            )
        return result


def test_workflow_calls_all_roles_and_obeys_coordinator(tmp_path):
    from test_workflow import Gateway

    case = {
        "case_id": "CASE_001",
        "policy_version": "EC_POLICY_V2",
        "customer_unique_id_hint": "customer-1",
        "candidate_order_ids": ["order-1"],
        "customer_request": {"claimed_order_id": "order-1", "claims": []},
    }
    original = copy.deepcopy(case)
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    reviewer = ReviewModel()
    output = asyncio.run(CaseWorkflow(case, Gateway(), trace, llm=reviewer).run())
    assert reviewer.calls == [
        "coordinator",
        "entity-agent",
        "order-agent",
        "payment-agent",
        "shipment-agent",
        "policy-agent",
        "verifier",
    ]
    assert case == original
    assert output["assessment"]["confidence"] <= 0.8
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    assert sum(e.get("decision_code") == "LLM_REVIEW_COMPLETED" for e in events) == 7
    contracts.validate_output(output, "test")


def test_uncorroborated_llm_verifier_error_is_an_audit_warning(tmp_path):
    from test_workflow import Gateway

    case = {
        "case_id": "CASE_001",
        "policy_version": "EC_POLICY_V2",
        "customer_unique_id_hint": "customer-1",
        "candidate_order_ids": ["order-1"],
        "customer_request": {"claimed_order_id": "order-1", "claims": []},
    }
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    output = asyncio.run(CaseWorkflow(case, Gateway(), trace, llm=ReviewModel("verifier")).run())
    contracts.validate_output(output, "test")
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    verification = next(
        event for event in events if event["event_type"] == "verification_completed"
    )
    assert verification["decision_code"] == "VERIFICATION_PASSED"
    assert verification["attributes"]["warnings"] == 1


def test_refusal_is_not_silently_replaced_by_rules():
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(
                    return_value=SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                finish_reason="stop",
                                message=SimpleNamespace(refusal="refused", content=None),
                            )
                        ]
                    )
                )
            )
        )
    )
    reviewer = OpenAIReviewer("test-only-key", client=client)
    with pytest.raises(RuntimeError, match="refused"):
        asyncio.run(
            reviewer.review(
                role="coordinator",
                case={"case_id": "CASE_001"},
                proposal={},
                evidence=[],
                allowed_issues=[],
            )
        )


def test_missing_key_does_not_touch_existing_outputs(tmp_path, monkeypatch):
    from student_agent import cli

    output = tmp_path / "outputs" / "existing.json"
    output.parent.mkdir()
    output.write_text('{"preserve":true}', encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        asyncio.run(cli._run(tmp_path))
    assert output.read_text(encoding="utf-8") == '{"preserve":true}'


def test_policy_can_only_select_evidence_supported_issue():
    result = review_result(refs=["ev_known"])
    result["selected_primary_issue"] = "refund_pending"
    validate_review(
        result,
        case_id="CASE_001",
        role="policy-agent",
        refs={"ev_known"},
        allowed_issues=["refund_pending", "payment_mismatch"],
    )
    with pytest.raises(ContractError, match="unsupported"):
        validate_review(
            result,
            case_id="CASE_001",
            role="policy-agent",
            refs={"ev_known"},
            allowed_issues=["payment_mismatch"],
        )


def test_coordinator_may_plan_before_evidence_exists():
    result = review_result()
    result.update(verdict="needs_investigation", concern_codes=[])
    validate_review(result, case_id="CASE_001", role="coordinator", refs=set(), allowed_issues=[])


def test_invalid_proposal_is_identified_by_proposal_error_code():
    result = review_result()
    result.update(
        proposal_valid=False,
        verdict="approve",
        concern_codes=[],
        proposal_error_codes=["PROPOSAL_ERROR"],
    )
    validate_review(result, case_id="CASE_001", role="coordinator", refs=set(), allowed_issues=[])
    assert result["proposal_valid"] is False


def test_proposal_valid_is_normalized_from_error_codes():
    result = review_result()
    result["proposal_valid"] = False
    validate_review(result, case_id="CASE_001", role="coordinator", refs=set(), allowed_issues=[])
    assert result["proposal_valid"] is True


def test_valid_specialist_may_report_case_needs_investigation_without_error():
    result = review_result()
    result.update(verdict="needs_investigation", concern_codes=[])
    validate_review(
        result, case_id="CASE_001", role="shipment-agent", refs=set(), allowed_issues=[]
    )


def test_llm_specialist_error_is_audit_warning_not_missing_evidence(tmp_path):
    from test_workflow import Gateway

    class PolicyGateway(Gateway):
        async def call(self, name, **arguments):
            evidence = await super().call(name, **arguments)
            if name == "get_policy":
                evidence["data"] = {
                    "policy_version": "EC_POLICY_V2",
                    "currency": "BRL",
                    "rules": {
                        "payment_mismatch": {
                            "case_status": "action_required",
                            "recommended_action": "reconcile_payment",
                            "refund_brl": 0,
                            "responsible_parties": [
                                {"party_type": "payment_provider", "party_id": None}
                            ],
                        }
                    },
                }
            return evidence

    case = {
        "case_id": "CASE_001",
        "policy_version": "EC_POLICY_V2",
        "customer_unique_id_hint": "customer-1",
        "candidate_order_ids": ["order-1"],
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [{"claim_id": "claim-1", "topic": "payment_mismatch"}],
        },
    }
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    workflow = CaseWorkflow(
        case, PolicyGateway(), trace, llm=ReviewModel("payment-agent")
    )
    output = asyncio.run(workflow.run())
    assert output["assessment"]["case_status"] == "action_required"
    assert "investigate_missing_evidence" not in output["resolution_actions"]
    assert not any(item.startswith("llm:") for item in workflow.missing)
    assert any(
        item["actor"] == "payment-agent" and item["kind"] == "proposal_error_codes"
        for item in workflow.review_warnings
    )


def test_confidence_uses_only_issue_relevant_reviewers(tmp_path):
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    workflow = CaseWorkflow({"case_id": "CASE_001"}, object(), trace)
    workflow.reviews = {
        "shipment-agent": {"confidence": 0.05},
        "order-agent": {"confidence": 0.9},
        "payment-agent": {"confidence": 0.8},
        "policy-agent": {"confidence": 0.85},
    }
    decision = {
        "assessment": {"primary_issue": "payment_mismatch", "confidence": 0.95},
        "claim_assessments": [{"confidence": 0.95}],
    }
    workflow.apply_review_confidence(decision)
    assert decision["assessment"]["confidence"] == 0.85
    assert decision["claim_assessments"][0]["confidence"] == 0.85
