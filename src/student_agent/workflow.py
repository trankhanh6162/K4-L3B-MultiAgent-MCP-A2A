from __future__ import annotations

import json
import os
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .mcp_gateway import EvidenceGateway, ToolSpec
from .trace import TraceWriter


class InvestigationState(TypedDict, total=False):
    case: dict[str, Any]
    gateway: EvidenceGateway
    trace: TraceWriter
    ledger: dict[str, dict[str, Any]]
    by_domain: dict[str, list[dict[str, Any]]]
    resolved_order_ids: list[str]
    rejected_candidates: list[str]
    entity_status: str
    customer_id: str | None
    tool_errors: list[str]
    output: dict[str, Any]


ALIASES = {
    "order_id": ("order_id", "id"),
    "customer_unique_id": ("customer_unique_id", "customer_id"),
    "policy_version": ("policy_version", "version"),
}


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _values(value: Any, *names: str) -> list[Any]:
    wanted = {name.lower() for name in names}
    found: list[Any] = []
    for obj in _walk(value):
        for key, child in obj.items():
            if key.lower() in wanted and child is not None:
                found.extend(child if isinstance(child, list) else [child])
    return found


def _ids(value: Any, *names: str) -> list[str]:
    return list(dict.fromkeys(str(item) for item in _values(value, *names) if str(item)))[:20]


def _first(value: Any, *names: str) -> Any:
    values = _values(value, *names)
    return values[0] if values else None


def _money(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    return float(max(amount, Decimal("0")))


def _sum_money(data: Any, *names: str) -> float | None:
    values = [_money(item) for item in _values(data, *names)]
    usable = [item for item in values if item is not None]
    return round(sum(usable), 2) if usable else None


def _text(data: Any) -> str:
    return " ".join(
        str(value).lower()
        for obj in _walk(data)
        for value in obj.values()
        if not isinstance(value, (dict, list))
    )


def _arguments(spec: ToolSpec, context: dict[str, Any]) -> dict[str, Any] | None:
    properties = spec.input_schema.get("properties", {})
    required = set(spec.input_schema.get("required", [])) - {"case_id"}
    result: dict[str, Any] = {}
    for name in properties:
        if name == "case_id":
            continue
        candidates = (name, *ALIASES.get(name, ()))
        value = next((context[key] for key in candidates if context.get(key) is not None), None)
        if value is not None:
            result[name] = value
    return result if required.issubset(result) else None


# Tool names published by the competition MCP gateway, per evidence domain.
PREFERRED_TOOLS = {
    "order": ("get_order",),
    "customer": ("get_customer_history",),
    "item": ("get_order_items",),
    "product": ("get_product_context",),
    "seller": ("get_sellers",),
    "shipment": ("get_shipment_summary",),
    "payment": ("get_payment_timeline", "get_order_payments"),
    "refund": ("get_refund_timeline",),
    "policy": ("get_policy",),
}

DOMAIN_TOKENS = {
    "order": ("order", "pedido"),
    "customer": ("customer", "history", "cliente"),
    "item": ("item",),
    "product": ("product", "category"),
    "seller": ("seller",),
    "shipment": ("shipment", "delivery", "logistics", "freight"),
    "payment": ("payment", "charge", "capture"),
    "refund": ("refund", "reimburse"),
    "policy": ("policy", "eligibility", "rule"),
}


def _select_tool(
    tools: dict[str, ToolSpec], domain: str, context: dict[str, Any]
) -> tuple[ToolSpec, dict[str, Any]] | None:
    for name in PREFERRED_TOOLS.get(domain, ()):
        if name in tools and (args := _arguments(tools[name], context)) is not None:
            return tools[name], args
    # Fallback for unknown gateways: a token in the tool name outweighs one in its description,
    # and names carrying other domains' tokens are penalised so "get_sellers" never wins "order".
    ranked: list[tuple[int, int, ToolSpec, dict[str, Any]]] = []
    for spec in tools.values():
        args = _arguments(spec, context)
        if args is None:
            continue
        name = spec.name.lower()
        description = spec.description.lower()
        score = sum(
            5 if token in name else 1 if token in description else 0
            for token in DOMAIN_TOKENS[domain]
        )
        other = sum(
            1
            for other_domain, tokens in DOMAIN_TOKENS.items()
            if other_domain != domain and any(token in name for token in tokens)
        )
        if score:
            ranked.append((score - 2 * other, -len(name), spec, args))
    if not ranked:
        return None
    _, _, spec, args = max(ranked, key=lambda item: (item[0], item[1], item[2].name))
    return spec, args


def _debug_dump(case_id: str, tool: str, args: dict[str, Any], payload: Any) -> None:
    """Local-only evidence dump for development (set DAY09_DEBUG_DIR); never packaged."""
    directory = os.environ.get("DAY09_DEBUG_DIR")
    if not directory:
        return
    target = Path(directory) / f"{case_id}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        record = {"tool": tool, "args": args, "evidence": payload}
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


async def _consume(
    state: InvestigationState, domain: str, actor: str, context: dict[str, Any]
) -> dict[str, Any] | None:
    gateway, case, trace = state["gateway"], state["case"], state["trace"]
    selected = _select_tool(await gateway.discover(), domain, context)
    if selected is None:
        return None
    spec, args = selected
    try:
        evidence = await gateway.call(spec.name, case_id=case["case_id"], **args)
    except (TimeoutError, OSError, RuntimeError, ValueError) as exc:
        # Tool errors (e.g. unknown candidate order) are evidence of absence, not a crash.
        state.setdefault("tool_errors", []).append(f"{spec.name}: {exc}")
        _debug_dump(case["case_id"], spec.name, args, {"error": str(exc)})
        return None
    ref = evidence["evidence_ref"]
    _debug_dump(case["case_id"], spec.name, args, evidence)
    state["ledger"][ref] = {**evidence, "tool_name": spec.name, "consumer": actor}
    state["by_domain"].setdefault(evidence["domain"], []).append(evidence)
    trace.emit(
        case_id=case["case_id"],
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=spec.name,
        evidence_refs=[ref],
    )
    return evidence


def _assign(state: InvestigationState, actor: str) -> None:
    state["trace"].emit(
        case_id=state["case"]["case_id"],
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
    )


def _handoff(state: InvestigationState, actor: str, code: str = "COMPLETED") -> None:
    state["trace"].emit(
        case_id=state["case"]["case_id"],
        event_type="handoff",
        actor=actor,
        target="coordinator",
        decision_code=code,
    )


async def entity_node(state: InvestigationState) -> dict[str, Any]:
    _assign(state, "entity-agent")
    case = state["case"]
    request = case["customer_request"]
    candidates = list(
        dict.fromkeys([request.get("claimed_order_id"), *case.get("candidate_order_ids", [])])
    )
    candidates = [item for item in candidates if item]
    valid: list[str] = []
    rejected: list[str] = []
    customer_hint = case.get("customer_unique_id_hint")
    customer_id: str | None = None
    for candidate in candidates:
        evidence = await _consume(
            state,
            "order",
            "entity-agent",
            {"order_id": candidate, "customer_unique_id": customer_hint},
        )
        if evidence is None:
            rejected.append(candidate)
            continue
        data = evidence["data"]
        returned_id = _first(data, "order_id")
        exists = _first(data, "exists", "found")
        # Order rows carry a per-order customer_id; only customer_unique_id identifies the person.
        found_customer = _first(data, "customer_unique_id")
        wrong_order = returned_id is not None and str(returned_id) != candidate
        wrong_customer = customer_hint and found_customer and str(found_customer) != customer_hint
        if exists is False or wrong_order or wrong_customer:
            rejected.append(candidate)
        else:
            valid.append(candidate)
            customer = found_customer or customer_hint
            customer_id = str(customer) if customer else None
    status = "resolved" if len(valid) == 1 else "ambiguous" if len(valid) > 1 else "not_found"
    _handoff(state, "entity-agent", f"ENTITY_{status.upper()}")
    return {
        "resolved_order_ids": valid[:20],
        "rejected_candidates": rejected[:20],
        "entity_status": status,
        "customer_id": customer_id,
    }


async def investigation_node(state: InvestigationState) -> dict[str, Any]:
    case = state["case"]
    order_id = (state.get("resolved_order_ids") or [None])[0]
    common = {
        "order_id": order_id,
        "customer_unique_id": state.get("customer_id") or case.get("customer_unique_id_hint"),
        "policy_version": case["policy_version"],
    }
    stages = [
        ("order-agent", "item"),
        ("shipment-agent", "shipment"),
        ("payment-agent", "payment"),
        ("payment-agent", "refund"),
    ]
    if case["investigation_scope"].get("include_product_context"):
        stages.insert(1, ("order-agent", "product"))
    if case["investigation_scope"].get("include_customer_history"):
        stages.insert(0, ("customer-agent", "customer"))
    for actor, domain in stages:
        _assign(state, actor)
        evidence = (
            await _consume(state, domain, actor, common)
            if order_id or domain == "customer"
            else None
        )
        _handoff(state, actor, "COMPLETED" if evidence else "INSUFFICIENT_EVIDENCE")
    return {}


async def policy_node(state: InvestigationState) -> dict[str, Any]:
    _assign(state, "policy-agent")
    case = state["case"]
    evidence = await _consume(
        state,
        "policy",
        "policy-agent",
        {
            "policy_version": case["policy_version"],
            "order_id": (state.get("resolved_order_ids") or [None])[0],
        },
    )
    state["trace"].emit(
        case_id=case["case_id"],
        event_type="policy_decided",
        actor="policy-agent",
        decision_code="POLICY_FOUND" if evidence else "POLICY_UNAVAILABLE",
        evidence_refs=[evidence["evidence_ref"]] if evidence else [],
    )
    _handoff(state, "policy-agent", "COMPLETED" if evidence else "INSUFFICIENT_EVIDENCE")
    return {}


def _domain_data(state: InvestigationState, *domains: str) -> list[Any]:
    return [
        evidence["data"] for domain in domains for evidence in state["by_domain"].get(domain, [])
    ]


def build_node(state: InvestigationState) -> dict[str, Any]:
    case = state["case"]
    all_data = [entry["data"] for entry in state["ledger"].values()]
    shipment_data = _domain_data(state, "shipment")
    payment_data = _domain_data(state, "payment", "refund")
    order_data = _domain_data(state, "order", "item", "seller", "product")
    shipment_text, payment_text, order_text = (
        _text(shipment_data),
        _text(payment_data),
        _text(order_data),
    )
    shipment_verdict = "insufficient_evidence"
    if shipment_data:
        if "lost" in shipment_text:
            shipment_verdict = "lost"
        elif "return" in shipment_text:
            shipment_verdict = "returned"
        elif "seller_delay" in shipment_text or "seller delay" in shipment_text:
            shipment_verdict = "seller_delay"
        elif any(x in shipment_text for x in ("logistics_delay", "late", "delayed")):
            shipment_verdict = "logistics_delay"
        elif any(x in shipment_text for x in ("on_time", "on time", "delivered")):
            shipment_verdict = "on_time"
    captured = _sum_money(
        payment_data,
        "captured_total_brl",
        "captured_amount_brl",
        "captured_amount",
        "amount_brl",
        "payment_value",
    )
    refunded = _sum_money(
        payment_data,
        "refunded_total_brl",
        "refunded_amount_brl",
        "refund_amount_brl",
        "refund_amount",
    )
    explicit_refundable = _sum_money(payment_data, "refundable_total_brl", "refundable_amount_brl")
    refundable = explicit_refundable
    if refundable is None and captured is not None:
        refundable = round(max(captured - (refunded or 0), 0), 2)
    payment_verdict = "insufficient_evidence"
    if payment_data:
        if "duplicate" in payment_text:
            payment_verdict = "duplicate_capture"
        elif "refund_failed" in payment_text or "refund failed" in payment_text:
            payment_verdict = "refund_failed"
        elif "refund_pending" in payment_text or "refund pending" in payment_text:
            payment_verdict = "refund_pending"
        elif refunded and captured is not None and refunded >= captured:
            payment_verdict = "refunded"
        elif "mismatch" in payment_text:
            payment_verdict = "capture_mismatch"
        else:
            payment_verdict = "reconciled"
    claims = case["customer_request"]["claims"]
    primary = next(
        (claim["topic"] for claim in claims if claim["topic"] != "requested_full_refund"),
        "insufficient_evidence",
    )
    authoritative = bool(state["ledger"]) and state["entity_status"] == "resolved"
    topic_supported = {
        "late_delivery_logistics": shipment_verdict == "logistics_delay",
        "late_delivery_seller": shipment_verdict == "seller_delay",
        "valid_split_payment": payment_verdict == "reconciled" and "split" in payment_text,
        "payment_mismatch": payment_verdict == "capture_mismatch",
        "duplicate_charge": payment_verdict == "duplicate_capture",
        "refund_pending": payment_verdict == "refund_pending",
        "refund_failed": payment_verdict == "refund_failed",
        "canceled_order_paid": "cancel" in order_text and bool(captured),
        "unavailable_order_paid": "unavailable" in order_text and bool(captured),
        "unsupported_claim": False,
    }
    supported = topic_supported.get(primary, False)
    policy_text = _text(_domain_data(state, "policy"))
    denied = any(x in policy_text for x in ("ineligible", "not eligible", "deny"))
    eligible = supported and refundable is not None and refundable > 0 and not denied
    refund = refundable if eligible else 0.0
    status = (
        "action_required" if refund > 0 else "no_action" if authoritative else "needs_investigation"
    )
    confidence = (
        0.92
        if authoritative and shipment_data and payment_data
        else 0.55
        if state["ledger"]
        else 0.15
    )
    refs_by_domain = {
        domain: [evidence["evidence_ref"] for evidence in entries]
        for domain, entries in state["by_domain"].items()
    }
    claim_assessments = []
    for claim in claims:
        topic = claim["topic"]
        refs = list(refs_by_domain.get("policy", []))
        if topic.startswith("late_"):
            refs += refs_by_domain.get("shipment", [])
        elif topic != "requested_full_refund":
            refs += (
                refs_by_domain.get("payment", [])
                + refs_by_domain.get("refund", [])
                + refs_by_domain.get("order", [])
            )
        else:
            refs += refs_by_domain.get("payment", []) + refs_by_domain.get("refund", [])
        enough = bool(refs) and state["entity_status"] == "resolved"
        claim_supported = (
            eligible if topic == "requested_full_refund" else topic_supported.get(topic, False)
        )
        verdict = (
            "supported"
            if enough and claim_supported
            else "unsupported"
            if enough
            else "insufficient_evidence"
        )
        claim_assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence if enough else min(confidence, 0.5),
                "evidence_refs": list(dict.fromkeys(refs))[:30],
            }
        )
    item_ids = _ids(all_data, "item_id", "order_item_id")
    seller_ids = _ids(all_data, "seller_id")
    causes = [] if not authoritative else [{"cause_code": primary.upper(), "rank": 1}]
    party_type = "unknown"
    if shipment_verdict == "seller_delay":
        party_type = "seller"
    elif shipment_verdict in {"logistics_delay", "lost", "returned"}:
        party_type = "logistics_provider"
    elif payment_verdict not in {"reconciled", "insufficient_evidence"}:
        party_type = "payment_provider"
    actions = (
        ["issue_refund"]
        if refund > 0
        else []
        if status == "no_action"
        else ["request_additional_evidence"]
    )
    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": primary if authoritative else "insufficient_evidence",
            "secondary_issues": [],
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": state["resolved_order_ids"],
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": _ids(
                all_data, "payment_reference", "payment_id", "transaction_id"
            ),
            "shipment_ids": _ids(all_data, "shipment_id", "tracking_id", "tracking_code"),
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": state["entity_status"],
            "resolved_order_ids": state["resolved_order_ids"],
            "rejected_candidates": state["rejected_candidates"],
            "confidence": (
                0.95 if state["entity_status"] == "resolved" else 0.5 if state["ledger"] else 0.1
            ),
        },
        "customer_context": {
            "customer_unique_id": state.get("customer_id"),
            "related_order_ids": _ids(
                _domain_data(state, "customer"), "order_id", "related_order_ids"
            ),
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": seller_ids if shipment_verdict == "seller_delay" else [],
            "timeline_complete": bool(shipment_data)
            and not any(entry.get("warnings") for entry in state["by_domain"].get("shipment", [])),
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": captured,
            "refunded_total_brl": refunded,
            "refundable_total_brl": refundable,
        },
        "root_cause_analysis": {
            "ranked_causes": causes,
            "responsible_parties": [
                {
                    "party_type": party_type,
                    "party_id": seller_ids[0] if party_type == "seller" and seller_ids else None,
                }
            ]
            if causes
            else [],
        },
        "evidence_refs": list(state["ledger"])[:30],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": [
                {
                    "reason_code": primary.upper(),
                    "amount_brl": refund,
                    "entity_id": state["resolved_order_ids"][0],
                }
            ]
            if refund > 0
            else [],
        },
        "resolution_actions": actions,
    }
    return {"output": output}


def verify_node(state: InvestigationState) -> dict[str, Any]:
    output = state["output"]
    refund_sum = round(
        sum(line["amount_brl"] for line in output["financial_resolution"]["refund_lines"]),
        2,
    )
    approved = refund_sum == output["financial_resolution"]["recommended_refund_brl"]
    approved &= not (
        set(output["entity_resolution"]["resolved_order_ids"])
        & set(output["entity_resolution"]["rejected_candidates"])
    )
    state["trace"].emit(
        case_id=state["case"]["case_id"],
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="APPROVED" if approved else "REJECTED",
    )
    if not approved:
        raise ValueError("verifier rejected inconsistent output")
    return {}


def _graph():
    graph = StateGraph(InvestigationState)
    graph.add_node("entity", entity_node)
    graph.add_node("investigate", investigation_node)
    graph.add_node("policy", policy_node)
    graph.add_node("build", build_node)
    graph.add_node("verify", verify_node)
    graph.add_edge(START, "entity")
    graph.add_edge("entity", "investigate")
    graph.add_edge("investigate", "policy")
    graph.add_edge("policy", "build")
    graph.add_edge("build", "verify")
    graph.add_edge("verify", END)
    return graph.compile()


GRAPH = _graph()


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    state: InvestigationState = {
        "case": case,
        "gateway": gateway,
        "trace": trace,
        "ledger": {},
        "by_domain": {},
        "tool_errors": [],
    }
    result = await GRAPH.ainvoke(state)
    if not state["ledger"]:
        # An output without any evidence ref is hard-gated to 0; stop instead of burning calls.
        errors = "; ".join(state["tool_errors"][:3]) or "no tool matched"
        raise RuntimeError(
            f"{case['case_id']}: MCP returned no usable evidence ({errors}). "
            "Check the team API key / registration and MCP gateway status."
        )
    return result["output"]
