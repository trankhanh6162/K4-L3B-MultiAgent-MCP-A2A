from __future__ import annotations

import json
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import OUTPUT_SCHEMA_VERSION, VARIANT_ID
from .cases import CaseSet
from .contracts import Contracts

SECRET_PATTERN = re.compile(r"sk-team-[A-Za-z0-9_-]{8,}")
MAX_FILE_BYTES = 1024 * 1024
MAX_SUBMISSION_BYTES = 12 * 1024 * 1024


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def build_manifest(case_set: CaseSet) -> dict[str, Any]:
    return {
        "schema_version": "day09-submission-manifest-v2",
        "competition_id": "day09-multiagent-mcp-a2a",
        "variant_id": VARIANT_ID,
        "case_set_version": case_set.version,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "trace_schema_version": "day09-trace-event-v1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "client": {"name": "day09-student-starter", "version": "0.1.0"},
    }


def compact_trace(
    trace_lines: list[str], outputs: dict[str, dict[str, Any]]
) -> list[str]:
    """Keep the score-relevant successful trace while dropping redundant audit events."""
    indexed = [(index, json.loads(line)) for index, line in enumerate(trace_lines)]
    compacted: list[tuple[int, dict[str, Any]]] = []
    required_types = {
        "case_received",
        "task_assigned",
        "handoff",
        "verification_completed",
        "case_finalized",
    }
    for case_id, output in outputs.items():
        case_events = [(index, event) for index, event in indexed if event["case_id"] == case_id]
        verified = [
            (index, event)
            for index, event in case_events
            if event["event_type"] == "verification_completed"
            and event.get("attributes", {}).get("passed") is True
        ]
        if not verified:
            raise ValueError(f"trace has no successful verification for {case_id}")
        verification = verified[-1]
        run_id = verification[1].get("attributes", {}).get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError(f"trace verification has no run_id for {case_id}")
        run_events = [
            (index, event)
            for index, event in case_events
            if event.get("attributes", {}).get("run_id") == run_id
        ]

        received = [item for item in case_events if item[1]["event_type"] == "case_received"]
        finalized = [item for item in case_events if item[1]["event_type"] == "case_finalized"]
        if not received or not finalized:
            raise ValueError(f"trace lifecycle is incomplete for {case_id}")
        received_before_run = [item for item in received if item[0] < verification[0]]
        finalized_after_run = [item for item in finalized if item[0] > verification[0]]
        if not received_before_run or not finalized_after_run:
            raise ValueError(f"trace lifecycle ordering is invalid for {case_id}")

        selected: list[tuple[int, dict[str, Any]]] = [
            received_before_run[-1],
            finalized_after_run[0],
            verification,
        ]
        policy = [item for item in run_events if item[1]["event_type"] == "policy_decided"]
        if policy:
            selected.append(policy[-1])

        # One assignment and one handoff per participant preserve collaboration
        # without retaining both the A2A and LLM audit copy of every transition.
        assignments: dict[str, tuple[int, dict[str, Any]]] = {}
        for item in run_events:
            if item[1]["event_type"] == "task_assigned":
                assignments.setdefault(str(item[1].get("target")), item)
        selected.extend(assignments.values())

        handoffs: dict[str, tuple[int, dict[str, Any]]] = {}
        for item in run_events:
            event = item[1]
            if event["event_type"] != "handoff":
                continue
            actor = event["actor"]
            current = handoffs.get(actor)
            score = (len(event.get("evidence_refs", [])), bool(event.get("decision_code")))
            current_score = (
                len(current[1].get("evidence_refs", [])),
                bool(current[1].get("decision_code")),
            ) if current else (-1, False)
            if score > current_score:
                handoffs[actor] = item
        selected.extend(handoffs.values())

        # Evidence-to-trace linkage needs one consumption event per submitted ref.
        # Prefer the specialist event over the verifier's duplicate consumption.
        evidence_events: dict[str, tuple[int, dict[str, Any]]] = {}
        required_refs = set(output["evidence_refs"])
        for item in run_events:
            event = item[1]
            if event["event_type"] != "tool_result_consumed":
                continue
            for ref in event.get("evidence_refs", []):
                if ref not in required_refs:
                    continue
                current = evidence_events.get(ref)
                if current is None or (
                    current[1]["actor"] == "verifier" and event["actor"] != "verifier"
                ):
                    evidence_events[ref] = item
        missing_refs = required_refs - set(evidence_events)
        if missing_refs:
            raise ValueError(f"trace lacks evidence linkage for {case_id}: {sorted(missing_refs)}")
        selected.extend(evidence_events.values())

        unique = {event["event_id"]: (index, event) for index, event in selected}
        present_types = {event["event_type"] for _, event in unique.values()}
        if not required_types <= present_types:
            raise ValueError(f"compacted trace lacks lifecycle events for {case_id}")
        compacted.extend(unique.values())

    compacted.sort(key=lambda item: item[0])
    minimized = []
    for index, event in compacted:
        event = dict(event)
        # run_id/task/message/token metadata is useful in the full local audit log,
        # but repeats thousands of times. Preserve run_id so the grader can join
        # every selected event to its successful attempt; drop only verbose fields.
        source_attributes = event.get("attributes", {})
        attributes = {}
        if "run_id" in source_attributes:
            attributes["run_id"] = source_attributes["run_id"]
        if event["event_type"] == "verification_completed":
            attributes.update(
                {
                    key: source_attributes[key]
                    for key in ("passed", "violations", "warnings", "error_codes")
                    if key in source_attributes
                }
            )
        if attributes:
            event["attributes"] = attributes
        else:
            event.pop("attributes", None)
        minimized.append((index, event))
    return [
        json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        for _, event in minimized
    ]


def validate_artifacts(
    root: Path, case_set: CaseSet, contracts: Contracts
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    outputs_root = root / "outputs"
    actual = {path.stem: path for path in outputs_root.glob("*.json") if path.is_file()}
    expected = set(case_set.case_ids)
    if set(actual) != expected:
        missing = sorted(expected - set(actual))
        extra = sorted(set(actual) - expected)
        raise ValueError(f"outputs do not match case-set; missing={missing}, extra={extra}")

    outputs: dict[str, dict[str, Any]] = {}
    for case_id in case_set.case_ids:
        output = _json_object(actual[case_id])
        contracts.validate_output(output, f"outputs/{case_id}.json")
        if output.get("case_id") != case_id:
            raise ValueError(f"outputs/{case_id}.json has a mismatched case_id")
        outputs[case_id] = output

    trace_path = root / "traces" / "trace.jsonl"
    try:
        trace_lines = trace_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("traces/trace.jsonl is missing or not UTF-8") from exc
    normalized_lines: list[str] = []
    seen_events: set[str] = set()
    for number, line in enumerate(trace_lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"traces/trace.jsonl:{number}: invalid JSON") from exc
        contracts.validate_trace(event, f"traces/trace.jsonl:{number}")
        if event["case_id"] not in expected:
            raise ValueError(f"traces/trace.jsonl:{number}: case is outside this case-set")
        if event["event_id"] in seen_events:
            raise ValueError(f"traces/trace.jsonl:{number}: duplicate event_id")
        seen_events.add(event["event_id"])
        normalized_lines.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))

    serialized = [json.dumps(value, ensure_ascii=False) for value in outputs.values()]
    if SECRET_PATTERN.search("\n".join([*serialized, *normalized_lines])):
        raise ValueError("a Team API Key appears in output or trace")
    return outputs, normalized_lines


def package_submission(root: Path, destination: Path) -> Path:
    from .cases import load_case_set

    root = root.resolve()
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    outputs, trace_lines = validate_artifacts(root, case_set, contracts)
    trace_lines = compact_trace(trace_lines, outputs)
    manifest = build_manifest(case_set)
    contracts.validate_manifest(manifest)

    payloads = {
        "manifest.json": json.dumps(manifest, separators=(",", ":")).encode(),
        "trace.jsonl": ("\n".join(trace_lines) + ("\n" if trace_lines else "")).encode(),
        **{
            f"outputs/{case_id}.json": json.dumps(
                outputs[case_id], ensure_ascii=False, separators=(",", ":")
            ).encode()
            for case_id in case_set.case_ids
        },
    }
    oversized = [name for name, payload in payloads.items() if len(payload) > MAX_FILE_BYTES]
    if oversized:
        raise ValueError(f"submission files exceed 1 MB: {oversized}")
    if sum(map(len, payloads.values())) > MAX_SUBMISSION_BYTES:
        raise ValueError("submission exceeds the 12 MB uncompressed limit")

    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    return destination
