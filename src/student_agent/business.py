"""Deterministic business rules for the discovered Olist MCP evidence shapes.

No case-number/topic lookup and no inferred evidence references. Missing source
responses stay unknown; policy controls actions, never whether a claim is true.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")
CENT = Decimal("0.01")


def money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def amount(value: Decimal | None) -> float | None:
    return float(value.quantize(CENT)) if value is not None else None


def date(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except (ValueError, AttributeError, TypeError):
        return None


def after(left: Any, right: Any) -> bool | None:
    left, right = date(left), date(right)
    if left is None or right is None:
        return None
    try:
        return left > right
    except TypeError:
        return None


def rows(value: Any, key: str) -> list[dict]:
    if isinstance(value, dict):
        value = value.get(key, [])
    return value if isinstance(value, list) and all(isinstance(r, dict) for r in value) else []


def unique_rows(values: list[dict]) -> list[dict]:
    return list({json.dumps(r, sort_keys=True): r for r in values}.values())


def conflict(
    field: str,
    sources: list[str],
    selected: str | None = None,
    code: str = "UNRESOLVED_SOURCE_CONFLICT",
) -> dict:
    return {
        "field": field,
        "sources": sources,
        "selected_source": selected,
        "resolution_code": code,
    }


def item_facts(order_id: str, data: Any) -> dict:
    groups = defaultdict(list)
    conflicts = []
    for row in rows(data, "items"):
        if row.get("order_id", order_id) != order_id:
            raise ValueError("Cross-order item evidence")
        item_id = row.get("order_item_id", row.get("item_id"))
        if item_id is not None:
            groups[str(item_id)].append(row)
    total, freight, complete = ZERO, ZERO, bool(groups)
    sellers, products = set(), set()
    for versions in groups.values():
        prices = {(money(v.get("price")), money(v.get("freight_value"))) for v in versions}
        sellers.update(str(v["seller_id"]) for v in versions if v.get("seller_id"))
        products.update(str(v["product_id"]) for v in versions if v.get("product_id"))
        if len(prices) != 1 or None in next(iter(prices)):
            complete = False
            if len(prices) > 1:
                conflicts.append(conflict("items.amount", ["item_snapshot_1", "item_snapshot_2"]))
        else:
            price, shipping = next(iter(prices))
            total += price + shipping
            freight += shipping
    return {
        "item_ids": sorted(groups),
        "seller_ids": sorted(sellers),
        "product_ids": sorted(products),
        "total": total if complete else None,
        "freight": freight if complete else None,
        "conflicts": conflicts,
    }


def shipment_facts(order: dict, data: Any, items: dict) -> dict:
    result = {"verdict": "insufficient_evidence", "late_seller_ids": [], "timeline_complete": False}
    conflicts, issues = [], []
    if not isinstance(data, dict):
        return {"analysis": result, "issues": issues, "conflicts": conflicts, "shipment_ids": []}
    if data.get("order_id") != order["order_id"]:
        raise ValueError("Cross-order shipment evidence")
    carrier = data.get("delivered_carrier_at")
    delivered = data.get("delivered_customer_at")
    estimated = data.get("estimated_delivery_at")
    late = after(delivered, estimated)
    limits = rows(data, "shipping_limits")
    late_sellers = sorted(
        {
            str(r["seller_id"])
            for r in limits
            if r.get("seller_id") and after(carrier, r.get("shipping_limit_at"))
        }
    )
    events = unique_rows(rows(data, "events"))
    for event in events:
        if event.get("order_id", order["order_id"]) != order["order_id"]:
            raise ValueError("Cross-order shipment event")
    confirmed = [r for r in events if r.get("status") in {"confirmed", "completed"}]
    actors = {r.get("actor") for r in confirmed if r.get("event_type") == "delivered_late"}
    types = {r.get("event_type") for r in confirmed}
    if "seller" in actors:
        issues.append("late_delivery_seller")
    if "logistics_provider" in actors:
        issues.append("late_delivery_logistics")
    if actors and (late is False or len(actors) > 1):
        conflicts.append(conflict("shipment.delivery", ["shipment_summary", "shipment_events"]))
        result["verdict"] = "conflicting"
    elif "lost" in types or "shipment_lost" in types:
        result["verdict"] = "lost"
    elif "returned" in types or "shipment_returned" in types:
        result["verdict"] = "returned"
    elif actors:
        result["verdict"] = "seller_delay" if "seller" in actors else "logistics_delay"
    elif late is True:
        result["verdict"] = (
            "seller_delay"
            if late_sellers
            else ("logistics_delay" if carrier and limits else "insufficient_evidence")
        )
        if result["verdict"] != "insufficient_evidence":
            issues.append("late_delivery_seller" if late_sellers else "late_delivery_logistics")
    elif late is False:
        result["verdict"] = "on_time"
    if result["verdict"] == "seller_delay":
        # Actor=seller alone does not identify which seller in a multi-seller order.
        result["late_seller_ids"] = late_sellers or (
            items["seller_ids"] if len(items["seller_ids"]) == 1 else []
        )
    result["timeline_complete"] = bool(
        date(carrier)
        and date(delivered)
        and date(estimated)
        and limits
        and all(date(r.get("shipping_limit_at")) for r in limits)
        and not conflicts
    )
    shipment_ids = sorted({str(r["shipment_id"]) for r in events if r.get("shipment_id")})
    return {
        "analysis": result,
        "issues": issues,
        "conflicts": conflicts,
        "shipment_ids": shipment_ids,
    }


def payment_facts(order: dict, data: Any, refund: Any, items: dict) -> dict:
    conflicts, issues = [], []
    analysis = {
        "verdict": "insufficient_evidence",
        "captured_total_brl": None,
        "refunded_total_brl": None,
        "refundable_total_brl": None,
    }
    empty = {
        "analysis": analysis,
        "issues": issues,
        "conflicts": conflicts,
        "references": [],
        "pending": ZERO,
        "failed": ZERO,
        "duplicate": ZERO,
    }
    if not isinstance(data, dict) or data.get("order_id") != order["order_id"]:
        return empty
    payment_rows = unique_rows(rows(data, "payments"))
    events = unique_rows(rows(data, "events"))
    for row in payment_rows + events:
        if row.get("order_id", order["order_id"]) != order["order_id"]:
            raise ValueError("Cross-order payment evidence")
    captured_events = [
        r
        for r in events
        if r.get("event_type") in {"captured", "capture", "duplicate_capture"}
        and r.get("status") in {"confirmed", "completed", "succeeded"}
    ]
    # One settled transaction may be repeated in several lifecycle snapshots.
    keyed_captures, anonymous_captures = {}, []
    capture_conflict = False
    for row in captured_events:
        identity = row.get("capture_id", row.get("transaction_id"))
        if identity is None:
            anonymous_captures.append(row)
            continue
        key = (str(identity), row.get("event_type"))
        previous = keyed_captures.get(key)
        if previous and money(previous.get("amount_brl")) != money(row.get("amount_brl")):
            capture_conflict = True
        keyed_captures[key] = row
    captured_events = list(keyed_captures.values()) + anonymous_captures
    amounts = [money(r.get("amount_brl")) for r in captured_events]
    captured = sum(amounts, ZERO) if amounts and None not in amounts else None
    if data.get("events") == [] and data.get("payments") == []:
        captured = ZERO
    if capture_conflict:
        captured = None
        conflicts.append(
            conflict("payment.capture_amount", ["capture_snapshot_1", "capture_snapshot_2"])
        )
    duplicate_events = [
        r
        for r in captured_events
        if r.get("event_type") == "duplicate_capture" or r.get("duplicate_of")
    ]
    duplicate = sum((money(r.get("amount_brl")) or ZERO for r in duplicate_events), ZERO)
    if duplicate:
        issues.append("duplicate_charge")
    mismatch = any(
        r.get("event_type") == "reconciliation_mismatch"
        and r.get("status") not in {"resolved", "reversed"}
        for r in events
    )
    expected = items["total"]
    if expected is None:
        expected = money(order.get("order_total_brl"))
    # Explicit lifecycle evidence is stronger than a count of payment rows.
    if mismatch or (
        captured is not None and expected is not None and captured != expected and not duplicate
    ):
        issues.append("payment_mismatch")
    if captured is not None and expected is not None and captured == expected:
        sequences = {
            str(r["payment_sequential"])
            for r in payment_rows
            if r.get("payment_sequential") is not None
        }
        if len(sequences) > 1 and not mismatch and not duplicate:
            issues.append("valid_split_payment")
    # Multiple versions of the same payment row are not additional transactions.
    versions = defaultdict(set)
    for row in payment_rows:
        if row.get("payment_sequential") is not None:
            versions[str(row["payment_sequential"])].add(str(row.get("payment_value")))
    if any(len(values) > 1 for values in versions.values()):
        conflicts.append(
            conflict(
                "payment.payment_value",
                ["payment_rows", "payment_events"],
                "payment_events",
                "AUTHORITATIVE_LIFECYCLE",
            )
        )
    purchase = order.get("order_purchase_timestamp")
    if any(after(purchase, r.get("event_at")) is True for r in captured_events):
        conflicts.append(conflict("payment.timeline", ["order", "payment_events"]))
    refunded = pending = failed = None
    if (
        isinstance(refund, dict)
        and refund.get("order_id") == order["order_id"]
        and isinstance(refund.get("events"), list)
        and all(isinstance(row, dict) for row in refund["events"])
    ):
        refund_events = unique_rows(rows(refund, "events"))
        groups = defaultdict(list)
        for event in refund_events:
            if event.get("order_id", order["order_id"]) != order["order_id"]:
                raise ValueError("Cross-order refund evidence")
            # Without a refund identifier, one lifecycle can be followed, but
            # multiple independent requests cannot be safely added together.
            groups[
                str(event.get("refund_id", event.get("refund_reference", "unidentified")))
            ].append(event)
        refunded, pending, failed = ZERO, ZERO, ZERO
        for key, lifecycle in groups.items():
            if any(not date(r.get("event_at")) for r in lifecycle):
                refunded = pending = failed = None
                break
            try:
                lifecycle.sort(key=lambda r: date(r["event_at"]))
            except TypeError:
                refunded = pending = failed = None
                break
            if key == "unidentified" and len({str(r.get("amount_brl")) for r in lifecycle}) > 1:
                conflicts.append(conflict("refund.amount", ["refund_event_1", "refund_event_2"]))
                refunded = pending = failed = None
                break
            last = lifecycle[-1]
            value = money(last.get("amount_brl"))
            if value is None:
                refunded = pending = failed = None
                break
            status, kind = last.get("status"), last.get("event_type")
            if status in {"completed", "succeeded", "confirmed"} and kind in {
                "refund_completed",
                "refunded",
                "refund_succeeded",
                "refund_settled",
            }:
                refunded += value
            elif status in {"pending", "processing", "requested"}:
                pending += value
            elif status == "failed" or kind == "refund_failed":
                failed += value
            else:
                refunded = pending = failed = None
                break
        if failed:
            issues.append("refund_failed")
        if pending:
            issues.append("refund_pending")
    outstanding = (
        max(ZERO, captured - refunded) if captured is not None and refunded is not None else None
    )
    if captured is not None and refunded is not None and refunded > captured:
        conflicts.append(conflict("payment.refunded_total", ["payment_events", "refund_events"]))
        outstanding = None
    verdict = "insufficient_evidence"
    for issue, value in [
        ("refund_failed", "refund_failed"),
        ("refund_pending", "refund_pending"),
        ("duplicate_charge", "duplicate_capture"),
        ("payment_mismatch", "capture_mismatch"),
    ]:
        if issue in issues:
            verdict = value
            break
    else:
        if refunded:
            verdict = "refunded"
        elif captured is not None and expected is not None and captured == expected:
            verdict = "reconciled"
    analysis.update(
        verdict=verdict,
        captured_total_brl=amount(captured),
        refunded_total_brl=amount(refunded),
        refundable_total_brl=amount(outstanding),
    )
    references = {
        str(r[k])
        for r in payment_rows + events
        for k in ("payment_reference", "transaction_id", "payment_id")
        if r.get(k)
    }
    return {
        "analysis": analysis,
        "issues": issues,
        "conflicts": conflicts,
        "references": sorted(references),
        "pending": pending,
        "failed": failed,
        "duplicate": duplicate,
    }


def decide(
    case: dict,
    orders: dict[str, dict],
    facts: dict[str, dict],
    policy: Any,
    missing: set[str],
    entity_conflicts: list[dict],
    *,
    preferred_issue: str | None = None,
) -> dict:
    """Select only independently supported issues, then apply the evidence policy."""
    supported = set()
    conflicts = list(entity_conflicts)
    for oid, group in facts.items():
        supported.update(group["shipment"]["issues"])
        supported.update(group["payment"]["issues"])
        for domain in ("items", "shipment", "payment"):
            conflicts.extend(group[domain]["conflicts"])
        captured = money(group["payment"]["analysis"]["captured_total_brl"])
        if captured and orders[oid].get("order_status") in {"canceled", "cancelled", "unavailable"}:
            supported.add(
                "unavailable_order_paid"
                if orders[oid]["order_status"] == "unavailable"
                else "canceled_order_paid"
            )
    good = (
        bool(facts)
        and not any(not item.startswith("llm:") for item in missing)
        and all(
            g["shipment"]["analysis"]["verdict"] == "on_time"
            and g["payment"]["analysis"]["verdict"] == "reconciled"
            for g in facts.values()
        )
    )
    if not supported and good:
        supported.add("unsupported_claim")
    claims = case.get("customer_request", {}).get("claims", [])
    requested = [c.get("topic") for c in claims if c.get("topic") in supported]
    priority = [
        "canceled_order_paid",
        "unavailable_order_paid",
        "duplicate_charge",
        "refund_failed",
        "refund_pending",
        "payment_mismatch",
        "late_delivery_seller",
        "late_delivery_logistics",
        "valid_split_payment",
        "unsupported_claim",
    ]
    primary = next(
        iter(requested), next((x for x in priority if x in supported), "insufficient_evidence")
    )
    if preferred_issue is not None:
        if preferred_issue not in supported:
            raise ValueError("Primary issue lacks deterministic evidence support")
        primary = preferred_issue
    rules = policy.get("rules", {}) if isinstance(policy, dict) else {}
    rules = rules if isinstance(rules, dict) else {}
    valid_policy = (
        isinstance(policy, dict)
        and policy.get("policy_version") == case["policy_version"]
        and policy.get("currency") == "BRL"
    )
    rule = rules.get(primary) if valid_policy else None
    rule = rule if isinstance(rule, dict) else {}
    status = rule.get("case_status", "needs_investigation")
    if status not in {"no_action", "action_required", "needs_investigation"}:
        status = "needs_investigation"
    actions = (
        [rule["recommended_action"]] if isinstance(rule.get("recommended_action"), str) else []
    )
    lines = []
    requested_refund = money(rule.get("refund_brl"))
    unknown_balance = False
    if requested_refund is not None and requested_refund > 0:
        for oid, group in facts.items():
            pay = group["payment"]
            balance = money(pay["analysis"]["refundable_total_brl"])
            if balance is None or pay["pending"] is None:
                unknown_balance = True
                continue
            available = max(ZERO, balance - pay["pending"])
            refund = min(requested_refund, available)
            if primary == "duplicate_charge":
                refund = min(refund, pay["duplicate"])
            if primary == "refund_failed":
                refund = min(refund, pay["failed"] or ZERO)
            if refund > 0:
                lines.append(
                    {"reason_code": primary.upper(), "amount_brl": amount(refund), "entity_id": oid}
                )
    conflicts = unique_rows(conflicts)
    unresolved = any(c["selected_source"] is None for c in conflicts)
    if missing or unresolved or unknown_balance or not rule:
        status = "needs_investigation"
        # Do not issue an irreversible monetary recommendation under unresolved conflicts.
        if unresolved or missing or unknown_balance:
            lines = []
        actions = [
            a
            for a in actions
            if a
            not in {"issue_refund", "refund_duplicate_charge", "refund_freight", "retry_refund"}
        ]
        actions.append(
            "investigate_missing_evidence"
            if missing or unknown_balance
            else "resolve_source_conflicts"
        )
    total = sum((Decimal(str(r["amount_brl"])) for r in lines), ZERO)
    sellers = {s for g in facts.values() for s in g["items"]["seller_ids"]}
    parties = []
    for party in rule.get("responsible_parties", []):
        if not isinstance(party, dict):
            continue
        kind = party.get("party_type")
        if kind not in {
            "seller",
            "platform",
            "logistics_provider",
            "payment_provider",
            "customer",
            "unknown",
        }:
            continue
        pid = party.get("party_id")
        if kind == "seller":
            scoped = {
                s for g in facts.values() for s in g["shipment"]["analysis"]["late_seller_ids"]
            }
            scoped = scoped or (sellers if primary == "unavailable_order_paid" else set())
            parties.extend({"party_type": "seller", "party_id": s} for s in sorted(scoped))
        elif kind in {"platform", "logistics_provider", "payment_provider", "customer", "unknown"}:
            parties.append({"party_type": kind, "party_id": pid if isinstance(pid, str) else None})
    confidence = 0.95 if primary != "insufficient_evidence" else 0.15
    if conflicts:
        confidence = min(confidence, 0.85)
    if missing or unknown_balance:
        confidence = min(confidence, 0.7)
    if unresolved:
        confidence = min(confidence, 0.55)
    verdicts = []
    for claim in claims:
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            balances = [
                money(g["payment"]["analysis"]["refundable_total_brl"]) for g in facts.values()
            ]
            full = sum(balances, ZERO) if balances and None not in balances else None
            verdict = (
                "supported"
                if full and total >= full
                else "partially_supported"
                if total > 0
                else "unsupported"
                if status == "no_action"
                else "insufficient_evidence"
            )
        elif topic in supported:
            verdict = "supported"
        elif good:
            verdict = "unsupported"
        else:
            verdict = "insufficient_evidence"
        verdicts.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": [],
            }
        )
    return {
        "assessment": {
            "primary_issue": primary,
            "secondary_issues": sorted(supported - {primary}),
            "case_status": status,
            "confidence": confidence,
        },
        "claim_assessments": verdicts,
        "data_conflicts": compact_conflicts(conflicts),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": amount(total),
            "refund_lines": lines,
        },
        "resolution_actions": sorted(set(actions)),
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": issue.upper(), "rank": index + 1}
                for index, issue in enumerate([primary] + sorted(supported - {primary}))
                if issue != "insufficient_evidence"
            ][:5],
            "responsible_parties": unique_rows(parties)[:5],
        },
    }


def compact_conflicts(conflicts: list[dict]) -> list[dict]:
    """Keep unresolved conflicts first; summarize overflow within public limits."""
    if len(conflicts) <= 5:
        return conflicts
    ordered = sorted(conflicts, key=lambda c: c["selected_source"] is not None)
    remainder = ordered[4:]
    sources = sorted({source for c in remainder for source in c["sources"]})[:5]
    return ordered[:4] + [
        conflict("additional_conflicts", sources, code="MULTIPLE_SOURCE_CONFLICTS")
    ]
