import json

from student_agent.submission import compact_trace


def event(event_id, case_id, event_type, actor, *, run_id=None, **extra):
    value = {
        "schema_version": "day09-trace-event-v1",
        "event_id": event_id,
        "case_id": case_id,
        "event_type": event_type,
        "occurred_at": "2026-01-01T00:00:00Z",
        "actor": actor,
        **extra,
    }
    if run_id is not None:
        value["attributes"] = {"run_id": run_id, **value.get("attributes", {})}
    return json.dumps(value, separators=(",", ":"))


def test_compact_trace_keeps_successful_lifecycle_and_evidence_linkage():
    case_id = "CASE_001"
    lines = [
        event("evt_received_old", case_id, "case_received", "coordinator"),
        event(
            "evt_verify_old",
            case_id,
            "verification_completed",
            "verifier",
            run_id="old",
            attributes={"passed": False},
        ),
        event("evt_received_new", case_id, "case_received", "coordinator"),
        event(
            "evt_task",
            case_id,
            "task_assigned",
            "coordinator",
            run_id="good",
            target="payment-agent",
        ),
        event(
            "evt_consumed_specialist",
            case_id,
            "tool_result_consumed",
            "payment-agent",
            run_id="good",
            evidence_refs=["ev_abcdefghijklmnopqrst"],
        ),
        event(
            "evt_consumed_verifier",
            case_id,
            "tool_result_consumed",
            "verifier",
            run_id="good",
            evidence_refs=["ev_abcdefghijklmnopqrst"],
        ),
        event(
            "evt_handoff_plain",
            case_id,
            "handoff",
            "payment-agent",
            run_id="good",
            target="coordinator",
        ),
        event(
            "evt_handoff_review",
            case_id,
            "handoff",
            "payment-agent",
            run_id="good",
            target="coordinator",
            decision_code="LLM_REVIEW_COMPLETED",
            evidence_refs=["ev_abcdefghijklmnopqrst"],
        ),
        event(
            "evt_verify_good",
            case_id,
            "verification_completed",
            "verifier",
            run_id="good",
            attributes={"passed": True},
        ),
        event("evt_finalized", case_id, "case_finalized", "coordinator"),
    ]
    compacted = [json.loads(line) for line in compact_trace(
        lines, {case_id: {"evidence_refs": ["ev_abcdefghijklmnopqrst"]}}
    )]
    ids = {item["event_id"] for item in compacted}
    assert "evt_verify_old" not in ids
    assert "evt_consumed_specialist" in ids
    assert "evt_consumed_verifier" not in ids
    assert "evt_handoff_review" in ids
    assert "evt_handoff_plain" not in ids
    assert all(
        item.get("attributes", {}).get("run_id") == "good"
        for item in compacted
        if item["event_type"] not in {"case_received", "case_finalized"}
    )
    assert {item["event_type"] for item in compacted} >= {
        "case_received",
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "verification_completed",
        "case_finalized",
    }
