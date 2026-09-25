from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import LLMSettings
from .llm import generate_output
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


@lru_cache(maxsize=1)
def _output_schemas() -> tuple[dict[str, Any], dict[str, Any]]:
    schema_root = Path(__file__).resolve().parents[2] / "contracts" / "schemas"
    l3b = json.loads((schema_root / "l3b-output-v2.schema.json").read_text(encoding="utf-8"))
    shared = json.loads((schema_root / "l3a-output-v2.schema.json").read_text(encoding="utf-8"))
    return l3b, shared


def _find_string(value: Any, key: str) -> str | None:
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
        for nested in value.values():
            found = _find_string(nested, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_string(nested, key)
            if found is not None:
                return found
    return None


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _unique_strings(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _derive_issue(facts: dict[str, Any]) -> tuple[str, str, list[str]]:
    order = facts["order"]
    status = str(order.get("order_status", "")).lower()
    captured = _money(facts["captured_total_brl"])
    refunded = _money(facts["refunded_total_brl"])
    shipment = facts["shipment"]
    delivered = shipment.get("delivered_customer_at")
    estimated = shipment.get("estimated_delivery_at")
    shipment_verdict = (
        "on_time"
        if isinstance(delivered, str) and isinstance(estimated, str) and delivered <= estimated
        else "insufficient_evidence"
    )
    if status == "canceled" and captured > refunded:
        return "canceled_order_paid", shipment_verdict, []
    if status in {"unavailable", "unavailable_order"} and captured > refunded:
        return "unavailable_order_paid", shipment_verdict, []

    refund_events = facts["refund_events"]
    refund_statuses = {str(event.get("status", "")).lower() for event in refund_events}
    if "failed" in refund_statuses:
        return "refund_failed", shipment_verdict, []
    if refund_events and refund_statuses & {"pending", "requested", "processing"}:
        return "refund_pending", shipment_verdict, []

    if isinstance(delivered, str) and isinstance(estimated, str) and delivered > estimated:
        carrier = shipment.get("delivered_carrier_at")
        limits = shipment.get("shipping_limits", [])
        late_sellers = _unique_strings(
            [
                limit.get("seller_id")
                for limit in limits
                if isinstance(carrier, str) and carrier > limit.get("shipping_limit_at", "")
            ]
        )
        if late_sellers:
            return "late_delivery_seller", "seller_delay", late_sellers
        return "late_delivery_logistics", "logistics_delay", []

    expected = _money(facts["expected_order_total_brl"])
    captures = [
        _money(event.get("amount_brl"))
        for event in facts["payment_events"]
        if event.get("event_type") == "captured"
    ]
    if expected > 0 and captured > expected and len(captures) > 1:
        return "duplicate_charge", shipment_verdict, []
    if expected > 0 and abs(captured - expected) > Decimal("0.01"):
        return "payment_mismatch", shipment_verdict, []
    if len(captures) > 1 and abs(captured - expected) <= Decimal("0.01"):
        return "valid_split_payment", shipment_verdict, []
    return "unsupported_claim", shipment_verdict, []


def _build_facts(
    case: dict[str, Any],
    selected_order_id: str,
    candidates: list[str],
    order_results: list[dict[str, Any] | None],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    by_domain = {item["domain"]: item for item in evidence}
    order = by_domain["order"]["data"]
    purchased_at = order.get("order_purchase_timestamp", "")

    raw_items = by_domain.get("item", {}).get("data", [])
    if not isinstance(raw_items, list):
        raw_items = []
    current_items = [
        item
        for item in raw_items
        if item.get("order_id") == selected_order_id
        and item.get("shipping_limit_date", "") >= purchased_at
    ]
    items_by_id = {
        item["order_item_id"]: item
        for item in current_items
        if isinstance(item.get("order_item_id"), str)
    }
    current_items = list(items_by_id.values())

    shipment = dict(by_domain.get("shipment", {}).get("data", {}))
    if not isinstance(shipment.get("shipping_limits", []), list):
        shipment["shipping_limits"] = []
    shipment["shipping_limits"] = [
        limit
        for limit in shipment.get("shipping_limits", [])
        if limit.get("shipping_limit_at", "") >= purchased_at
    ]

    payment = by_domain.get("payment", {}).get("data", {})
    if not isinstance(payment, dict):
        payment = {}
    payment_events = [
        event
        for event in payment.get("events", [])
        if event.get("order_id") == selected_order_id and event.get("event_at", "") >= purchased_at
    ]
    refund = by_domain.get("refund", {}).get("data", {})
    if not isinstance(refund, dict):
        refund = {}
    refund_events = [
        event
        for event in refund.get("events", [])
        if event.get("order_id") == selected_order_id and event.get("event_at", "") >= purchased_at
    ]
    captured = sum(
        (_money(event.get("amount_brl")) for event in payment_events if event.get("event_type") == "captured"),
        Decimal("0"),
    )
    refunded = sum(
        (
            _money(event.get("amount_brl"))
            for event in refund_events
            if event.get("event_type") in {"refunded", "refund_completed"}
            or event.get("status") in {"refunded", "completed", "succeeded"}
        ),
        Decimal("0"),
    )
    expected = sum(
        (_money(item.get("price")) + _money(item.get("freight_value")) for item in current_items),
        Decimal("0"),
    )

    claim_topic = case["customer_request"]["claims"][0]["topic"]
    policy_rules = by_domain.get("policy", {}).get("data", {}).get("rules", {})
    customer = by_domain.get("customer", {}).get("data", {})
    if not isinstance(customer, dict):
        customer = {}
    related_orders = _unique_strings(
        [row.get("order_id") for row in customer.get("orders", [])]
    )
    evidence_refs = {item["domain"]: item["evidence_ref"] for item in evidence}
    rejected = [
        candidate
        for candidate, result in zip(candidates, order_results, strict=True)
        if result is None
    ]

    facts = {
        "resolved_order_id": selected_order_id,
        "rejected_candidates": rejected,
        "order": order,
        "items": current_items,
        "item_ids": _unique_strings([item.get("order_item_id") for item in current_items]),
        "seller_ids": _unique_strings([item.get("seller_id") for item in current_items]),
        "shipment": shipment,
        "payment_events": payment_events,
        "refund_events": refund_events,
        "captured_total_brl": float(captured),
        "refunded_total_brl": float(refunded),
        "unrefunded_total_brl": float(max(captured - refunded, Decimal("0"))),
        "expected_order_total_brl": float(expected),
        "customer_unique_id": customer.get("customer_unique_id")
        or case.get("customer_unique_id_hint"),
        "related_order_ids": related_orders,
        "claimed_issue": claim_topic,
        "evidence_refs_by_domain": evidence_refs,
    }
    issue, shipment_verdict, late_sellers = _derive_issue(facts)
    facts["derived_issue"] = issue
    facts["shipment_verdict"] = shipment_verdict
    facts["late_seller_ids"] = late_sellers
    facts["applicable_policy_rule"] = policy_rules.get(issue)
    return facts


def _normalize_output(
    case: dict[str, Any], output: dict[str, Any], facts: dict[str, Any]
) -> None:
    order_id = facts["resolved_order_id"]
    output["schema_version"] = "day09-l3b-output-v2"
    output["case_id"] = case["case_id"]
    output["entity_resolution"] = {
        "status": "resolved",
        "resolved_order_ids": [order_id],
        "rejected_candidates": facts["rejected_candidates"],
        "confidence": 1.0 if facts["rejected_candidates"] else 0.9,
    }
    output["customer_context"] = {
        "customer_unique_id": facts["customer_unique_id"],
        "related_order_ids": facts["related_order_ids"],
    }
    entities = output.setdefault("affected_entities", {})
    entities["order_ids"] = [order_id]
    entities["item_ids"] = facts["item_ids"]
    entities["seller_ids"] = facts["seller_ids"]
    entities["payment_references"] = []
    entities["shipment_ids"] = []

    payment = output.setdefault("payment_analysis", {})
    payment["captured_total_brl"] = facts["captured_total_brl"]
    payment["refunded_total_brl"] = facts["refunded_total_brl"]
    payment["refundable_total_brl"] = facts["unrefunded_total_brl"]

    issue = facts["derived_issue"]
    payment["verdict"] = {
        "payment_mismatch": "capture_mismatch",
        "duplicate_charge": "duplicate_capture",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }.get(issue, "reconciled")
    output["shipment_analysis"] = {
        "verdict": facts["shipment_verdict"],
        "late_seller_ids": facts["late_seller_ids"],
        "timeline_complete": all(
            facts["shipment"].get(field)
            for field in ("delivered_carrier_at", "estimated_delivery_at")
        ),
    }

    claims = {
        claim.get("claim_id"): claim for claim in output.get("claim_assessments", [])
    }
    primary_claim = case["customer_request"]["claims"][0]
    policy = facts.get("applicable_policy_rule") or {}
    policy_ref = facts["evidence_refs_by_domain"].get("policy")
    domain_refs = list(facts["evidence_refs_by_domain"].values())
    output["evidence_refs"] = domain_refs
    primary_verdict = "supported" if primary_claim["topic"] == issue else "unsupported"
    if primary_claim["claim_id"] in claims:
        claims[primary_claim["claim_id"]].update(
            verdict=primary_verdict,
            confidence=0.95,
            evidence_refs=domain_refs,
        )

    refund = float(policy.get("refund_brl", 0)) if policy else 0.0
    requested_claim = next(
        (
            claim
            for claim in case["customer_request"]["claims"]
            if claim["topic"] == "requested_full_refund"
        ),
        None,
    )
    if requested_claim and requested_claim["claim_id"] in claims:
        full_amount = facts["unrefunded_total_brl"]
        refund_verdict = (
            "supported"
            if refund > 0 and abs(refund - full_amount) <= 0.01
            else "partially_supported"
            if refund > 0
            else "unsupported"
        )
        claims[requested_claim["claim_id"]].update(
            verdict=refund_verdict,
            confidence=0.95,
            evidence_refs=domain_refs,
        )

    assessment = output.setdefault("assessment", {})
    assessment["primary_issue"] = issue
    assessment["secondary_issues"] = []
    assessment["confidence"] = 0.95
    parties = [dict(party) for party in policy.get("responsible_parties", [])] if policy else []
    for party in parties:
        if party.get("party_type") == "seller" and party.get("party_id") not in facts["seller_ids"]:
            party["party_id"] = next(
                iter(facts["late_seller_ids"] or facts["seller_ids"]),
                None,
            )
    responsible_entity = next(
        (party.get("party_id") for party in parties if party.get("party_id")),
        order_id,
    )
    if policy:
        refund = float(policy.get("refund_brl", 0))
        action = policy.get("recommended_action")
        output["financial_resolution"] = {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": (
                [{"reason_code": issue, "amount_brl": refund, "entity_id": responsible_entity}]
                if refund > 0
                else []
            ),
        }
        output["resolution_actions"] = [action] if isinstance(action, str) and action else []
        assessment["case_status"] = policy.get("case_status", "needs_investigation")
        if policy_ref and policy_ref not in output["evidence_refs"]:
            output["evidence_refs"].append(policy_ref)
    else:
        output["financial_resolution"] = {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        }
        output["resolution_actions"] = []
        assessment["case_status"] = "needs_investigation"

    output["root_cause_analysis"] = {
        "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
        "responsible_parties": parties,
    }


class CaseEvidence:
    def __init__(self, case_id: str, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case_id = case_id
        self.gateway = gateway
        self.trace = trace
        self.cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}

    async def call(self, actor: str, tool_name: str, **arguments: str) -> dict[str, Any]:
        key = (tool_name, tuple(sorted(arguments.items())))
        if key not in self.cache:
            self.cache[key] = await self.gateway.call(
                tool_name, case_id=self.case_id, **arguments
            )
        evidence = self.cache[key]
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[evidence["evidence_ref"]],
        )
        return evidence

    async def optional_call(
        self, actor: str, tool_name: str, **arguments: str
    ) -> dict[str, Any] | None:
        try:
            return await self.call(actor, tool_name, **arguments)
        except RuntimeError:
            return None


def _verify_output(
    case: dict[str, Any], output: dict[str, Any], evidence: list[dict[str, Any]]
) -> None:
    case_id = case["case_id"]
    if output.get("case_id") != case_id:
        raise ValueError(f"OpenAI returned a mismatched case_id for {case_id}")

    known_refs = {item["evidence_ref"] for item in evidence}
    submitted_refs = set(output.get("evidence_refs", []))
    claim_refs = {
        evidence_ref
        for claim in output.get("claim_assessments", [])
        for evidence_ref in claim.get("evidence_refs", [])
    }
    unknown_refs = (submitted_refs | claim_refs) - known_refs
    if unknown_refs:
        raise ValueError(f"OpenAI invented evidence refs for {case_id}: {sorted(unknown_refs)}")
    if not claim_refs <= submitted_refs:
        output["evidence_refs"] = [
            item["evidence_ref"]
            for item in evidence
            if item["evidence_ref"] in submitted_refs | claim_refs
        ]
        submitted_refs = set(output["evidence_refs"])

    expected_claims = {
        claim["claim_id"] for claim in case["customer_request"].get("claims", [])
    }
    actual_claims = {claim.get("claim_id") for claim in output.get("claim_assessments", [])}
    if actual_claims != expected_claims:
        raise ValueError(f"OpenAI returned incomplete claim assessments for {case_id}")
    if not claim_refs <= submitted_refs:
        raise ValueError(f"OpenAI returned unlinked claim evidence for {case_id}")

    financial = output.get("financial_resolution", {})
    recommended = financial.get("recommended_refund_brl")
    lines = financial.get("refund_lines", [])
    if isinstance(recommended, (int, float)) and isinstance(lines, list):
        line_total = sum(
            line.get("amount_brl", 0)
            for line in lines
            if isinstance(line, dict) and isinstance(line.get("amount_brl", 0), (int, float))
        )
        if abs(float(recommended) - float(line_total)) > 0.01:
            financial["recommended_refund_brl"] = round(float(line_total), 2)

    resolution = output.get("entity_resolution", {})
    resolved = set(resolution.get("resolved_order_ids", []))
    rejected = set(resolution.get("rejected_candidates", []))
    if resolved & rejected:
        raise ValueError(f"OpenAI both resolved and rejected an order for {case_id}")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    collector = CaseEvidence(case_id, gateway, trace)
    candidates = list(dict.fromkeys(case.get("candidate_order_ids", [])))
    claimed_order_id = case["customer_request"].get("claimed_order_id")
    if claimed_order_id and claimed_order_id not in candidates:
        candidates.insert(0, claimed_order_id)

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        decision_code="RESOLVE_ORDER_CANDIDATES",
    )
    order_results = []
    for order_id in candidates:
        order_results.append(
            await collector.optional_call("entity-agent", "get_order", order_id=order_id)
        )
    order_evidence = [item for item in order_results if item is not None]
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="coordinator",
        decision_code="ENTITY_RESOLUTION_COMPLETE",
        evidence_refs=[item["evidence_ref"] for item in order_evidence],
    )
    if not order_evidence:
        raise RuntimeError(f"MCP could not resolve any order candidate for {case_id}")

    selected_order_id = claimed_order_id or candidates[0]
    selected_order = next(
        (
            evidence
            for order_id, evidence in zip(candidates, order_results, strict=True)
            if order_id == selected_order_id and evidence is not None
        ),
        order_evidence[0],
    )
    if selected_order is not order_results[candidates.index(selected_order_id)]:
        selected_order_id = next(
            order_id
            for order_id, evidence in zip(candidates, order_results, strict=True)
            if evidence is selected_order
        )

    customer_id = _find_string(selected_order.get("data"), "customer_unique_id")
    customer_id = customer_id or case.get("customer_unique_id_hint")
    assignments = (
        ("customer-agent", "INVESTIGATE_CUSTOMER"),
        ("order-product-agent", "INVESTIGATE_ITEMS_PRODUCTS"),
        ("shipment-agent", "INVESTIGATE_SHIPMENT"),
        ("payment-refund-agent", "INVESTIGATE_PAYMENT_REFUND"),
        ("policy-agent", "EVALUATE_POLICY"),
    )
    for actor, decision_code in assignments:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code=decision_code,
        )

    call_specs = [
        ("order-product-agent", "get_order_items", {"order_id": selected_order_id}, False),
        ("order-product-agent", "get_product_context", {"order_id": selected_order_id}, False),
        ("shipment-agent", "get_shipment_summary", {"order_id": selected_order_id}, False),
        ("payment-refund-agent", "get_payment_timeline", {"order_id": selected_order_id}, False),
        ("policy-agent", "get_policy", {"policy_version": case["policy_version"]}, False),
    ]
    topics = {claim["topic"] for claim in case["customer_request"].get("claims", [])}
    refund_topics = {
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "canceled_order_paid",
        "unavailable_order_paid",
    }
    if topics & refund_topics:
        call_specs.append(
            ("payment-refund-agent", "get_refund_timeline", {"order_id": selected_order_id}, True)
        )
    if customer_id:
        call_specs.append(
            (
                "customer-agent",
                "get_customer_history",
                {"customer_unique_id": customer_id},
                False,
            )
        )
    specialist_evidence = []
    call_actors = []
    for actor, tool_name, arguments, optional in call_specs:
        if optional:
            evidence = await collector.optional_call(actor, tool_name, **arguments)
            if evidence is None:
                continue
        else:
            evidence = await collector.call(actor, tool_name, **arguments)
        specialist_evidence.append(evidence)
        call_actors.append(actor)

    for actor in dict.fromkeys(call_actors):
        actor_evidence = [
            item["evidence_ref"]
            for item, owner in zip(specialist_evidence, call_actors, strict=True)
            if owner == actor
        ]
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target="conflict-resolver",
            decision_code="SPECIALIST_FINDINGS_READY",
            evidence_refs=actor_evidence,
        )

    policy_ref = next(
        item["evidence_ref"]
        for item, owner in zip(specialist_evidence, call_actors, strict=True)
        if owner == "policy-agent"
    )
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="conflict-resolver",
        decision_code="POLICY_EVIDENCE_APPLIED",
        evidence_refs=[policy_ref],
    )

    all_evidence = [selected_order, *specialist_evidence]
    facts = _build_facts(
        case,
        selected_order_id,
        candidates,
        order_results,
        all_evidence,
    )
    settings = LLMSettings.load()
    output_schema, shared_schema = _output_schemas()
    output = await generate_output(
        settings,
        case=case,
        facts=facts,
        output_schema=output_schema,
        shared_schema=shared_schema,
    )
    _normalize_output(case, output, facts)

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="conflict-resolver",
        target="verifier",
        decision_code="VERIFY_FINAL_OUTPUT",
    )
    gateway.validate_output(output, f"OpenAI output for {case_id}")
    _verify_output(case, output, all_evidence)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="SCHEMA_PROVENANCE_CONSISTENCY_OK",
        evidence_refs=output["evidence_refs"],
    )
    return output
