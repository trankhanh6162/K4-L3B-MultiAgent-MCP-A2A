from copy import deepcopy
from decimal import Decimal

import pytest

from student_agent import business as b


def fixture():
    order = {
        "order_id": "o1",
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-01-01T09:00:00Z",
    }
    items = b.item_facts(
        "o1",
        [
            {
                "order_id": "o1",
                "order_item_id": "i1",
                "seller_id": "s1",
                "product_id": "p1",
                "price": "80",
                "freight_value": "20",
            }
        ],
    )
    shipment = {
        "order_id": "o1",
        "delivered_carrier_at": "2018-01-02T00:00:00Z",
        "delivered_customer_at": "2018-01-04T00:00:00Z",
        "estimated_delivery_at": "2018-01-05T00:00:00Z",
        "shipping_limits": [{"seller_id": "s1", "shipping_limit_at": "2018-01-03T00:00:00Z"}],
        "events": [],
    }
    payment = {
        "order_id": "o1",
        "payments": [{"payment_sequential": "1"}],
        "events": [
            {
                "order_id": "o1",
                "event_type": "captured",
                "status": "confirmed",
                "event_at": "2018-01-01T10:00:00Z",
                "amount_brl": "100",
            }
        ],
    }
    refund = {"order_id": "o1", "events": []}
    return order, items, shipment, payment, refund


@pytest.mark.parametrize(
    "issue",
    [
        "canceled_order_paid",
        "unavailable_order_paid",
        "duplicate_charge",
        "payment_mismatch",
        "refund_pending",
        "refund_failed",
        "late_delivery_seller",
        "late_delivery_logistics",
        "valid_split_payment",
        "unsupported_claim",
    ],
)
def test_all_supported_business_scenarios(issue):
    order, items, shipment, payment, refund = fixture()
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        order["order_status"] = "canceled" if issue.startswith("canceled") else "unavailable"
    elif issue == "duplicate_charge":
        payment["events"].append(
            {
                **payment["events"][0],
                "event_type": "duplicate_capture",
                "event_at": "2018-01-01T11:00:00Z",
            }
        )
    elif issue == "payment_mismatch":
        payment["events"][0]["amount_brl"] = "70"
    elif issue in {"refund_pending", "refund_failed"}:
        refund["events"] = [
            {
                "event_type": "refund_requested",
                "amount_brl": "100",
                "status": issue.removeprefix("refund_"),
                "event_at": "2018-01-06T00:00:00Z",
            }
        ]
    elif issue.startswith("late_delivery"):
        shipment["delivered_customer_at"] = "2018-01-08T00:00:00Z"
        if issue.endswith("seller"):
            shipment["delivered_carrier_at"] = "2018-01-04T00:00:00Z"
    elif issue == "valid_split_payment":
        payment["payments"] = [{"payment_sequential": "1"}, {"payment_sequential": "2"}]
        payment["events"] = [
            {**payment["events"][0], "amount_brl": "60"},
            {**payment["events"][0], "amount_brl": "40", "event_at": "2018-01-01T11:00:00Z"},
        ]
    facts = {
        "o1": {
            "items": items,
            "shipment": b.shipment_facts(order, shipment, items),
            "payment": b.payment_facts(order, payment, refund, items),
        }
    }
    no_action = issue in {"valid_split_payment", "unsupported_claim"}
    policy = {
        "policy_version": "P1",
        "currency": "BRL",
        "rules": {
            issue: {
                "case_status": "no_action" if no_action else "action_required",
                "recommended_action": "document_no_action" if no_action else "issue_refund",
                "refund_brl": 0 if no_action or issue == "refund_pending" else 20,
                "responsible_parties": [],
            }
        },
    }
    case = {
        "policy_version": "P1",
        "customer_request": {
            "claims": [
                {"claim_id": "c1", "topic": issue},
                {"claim_id": "c2", "topic": "requested_full_refund"},
            ]
        },
    }
    result = b.decide(case, {"o1": order}, facts, policy, set(), [])
    assert result["assessment"]["primary_issue"] == issue
    assert result["claim_assessments"][0]["verdict"] == "supported"
    assert result["assessment"]["case_status"] == ("no_action" if no_action else "action_required")
    assert result["financial_resolution"]["recommended_refund_brl"] == (
        0 if no_action or issue == "refund_pending" else 20
    )


def test_claim_is_not_evidence():
    order, items, shipment, payment, refund = fixture()
    facts = {
        "o1": {
            "items": items,
            "shipment": b.shipment_facts(order, shipment, items),
            "payment": b.payment_facts(order, payment, refund, items),
        }
    }
    case = {
        "policy_version": "P1",
        "customer_request": {"claims": [{"claim_id": "c1", "topic": "duplicate_charge"}]},
    }
    result = b.decide(case, {"o1": order}, facts, None, set(), [])
    assert result["assessment"]["primary_issue"] == "unsupported_claim"
    assert result["claim_assessments"][0]["verdict"] == "unsupported"


def test_refund_lifecycle_counts_settlement_once():
    order, items, _, payment, refund = fixture()
    refund["events"] = [
        {
            "refund_id": "r1",
            "event_at": "2018-01-06T00:00:00Z",
            "amount_brl": "30",
            "event_type": "refund_requested",
            "status": "pending",
        },
        {
            "refund_id": "r1",
            "event_at": "2018-01-07T00:00:00Z",
            "amount_brl": "30",
            "event_type": "refund_completed",
            "status": "completed",
        },
    ]
    refund["events"].append(deepcopy(refund["events"][-1]))
    result = b.payment_facts(order, payment, refund, items)
    assert result["analysis"]["refunded_total_brl"] == 30
    assert result["analysis"]["refundable_total_brl"] == 70
    assert result["pending"] == 0


def test_missing_refund_source_is_not_zero():
    order, items, _, payment, _ = fixture()
    result = b.payment_facts(order, payment, None, items)
    assert result["analysis"]["captured_total_brl"] == 100
    assert result["analysis"]["refunded_total_brl"] is None
    assert result["analysis"]["refundable_total_brl"] is None


def test_duplicate_item_versions_do_not_double_invoice():
    row = {"order_id": "o1", "order_item_id": "i1", "price": "80", "freight_value": "20"}
    facts = b.item_facts("o1", [row, {**row, "shipping_limit_date": "different snapshot"}])
    assert facts["total"] == Decimal(100)
    assert facts["item_ids"] == ["i1"]


def test_conflicting_item_prices_are_unknown():
    row = {"order_id": "o1", "order_item_id": "i1", "price": "80", "freight_value": "20"}
    facts = b.item_facts("o1", [row, {**row, "price": "90"}])
    assert facts["total"] is None
    assert facts["conflicts"][0]["selected_source"] is None


def test_shipment_summary_conflicting_with_event_is_explicit():
    order, items, shipment, _, _ = fixture()
    shipment["events"] = [
        {"event_type": "delivered_late", "actor": "logistics_provider", "status": "confirmed"}
    ]
    result = b.shipment_facts(order, shipment, items)
    assert result["analysis"]["verdict"] == "conflicting"
    assert result["analysis"]["timeline_complete"] is False
    assert result["conflicts"][0]["selected_source"] is None


def test_scope_violation_is_rejected():
    order, items, shipment, _, _ = fixture()
    shipment["order_id"] = "foreign-order"
    with pytest.raises(ValueError, match="Cross-order"):
        b.shipment_facts(order, shipment, items)


def test_repeated_transaction_snapshots_do_not_double_capture():
    order, items, _, payment, refund = fixture()
    payment["events"][0]["transaction_id"] = "txn-1"
    payment["events"].append({**payment["events"][0], "event_at": "2018-01-01T12:00:00Z"})
    assert b.payment_facts(order, payment, refund, items)["analysis"]["captured_total_brl"] == 100


def test_conflicting_capture_versions_are_not_arbitrarily_selected():
    order, items, _, payment, refund = fixture()
    payment["events"][0]["transaction_id"] = "txn-1"
    payment["events"].append({**payment["events"][0], "amount_brl": "150"})
    result = b.payment_facts(order, payment, refund, items)
    assert result["analysis"]["captured_total_brl"] is None
    assert result["conflicts"][0]["selected_source"] is None


def test_missing_events_field_is_not_proof_of_no_refund():
    order, items, _, payment, _ = fixture()
    result = b.payment_facts(order, payment, {"order_id": "o1"}, items)
    assert result["analysis"]["refunded_total_brl"] is None


def test_policy_cannot_refund_more_than_available_balance():
    order, items, shipment, payment, refund = fixture()
    order["order_status"] = "canceled"
    refund["events"] = [
        {
            "refund_id": "r1",
            "event_at": "2018-01-08T00:00:00Z",
            "event_type": "refund_completed",
            "status": "completed",
            "amount_brl": "80",
        }
    ]
    facts = {
        "o1": {
            "items": items,
            "shipment": b.shipment_facts(order, shipment, items),
            "payment": b.payment_facts(order, payment, refund, items),
        }
    }
    policy = {
        "policy_version": "P1",
        "currency": "BRL",
        "rules": {
            "canceled_order_paid": {
                "case_status": "action_required",
                "recommended_action": "issue_refund",
                "refund_brl": 100,
                "responsible_parties": [],
            }
        },
    }
    result = b.decide({"policy_version": "P1"}, {"o1": order}, facts, policy, set(), [])
    assert result["financial_resolution"]["recommended_refund_brl"] == 20
    blocked = b.decide(
        {"policy_version": "P1"}, {"o1": order}, facts, policy, {"product:unavailable"}, []
    )
    assert blocked["financial_resolution"]["recommended_refund_brl"] == 0
    assert blocked["assessment"]["case_status"] == "needs_investigation"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", None, True])
def test_invalid_money_is_not_used(value):
    assert b.money(value) is None
