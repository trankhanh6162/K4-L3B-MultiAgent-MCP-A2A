"""Internal message validation; transport and agent execution are separate concerns."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from .contracts import ContractError

TASK_ROUTES = {
    "resolve_entity": ("entity-agent", "entity", None),
    "analyze_order": ("order-agent", "specialist", "order"),
    "analyze_shipment": ("shipment-agent", "specialist", "shipment"),
    "analyze_payment": ("payment-agent", "specialist", "payment"),
    "decide_policy": ("policy-agent", "policy", None),
    "verify": ("verifier", "verification", None),
}


class A2AContracts:
    def __init__(self, public_schema_root: Path) -> None:
        schema_path = Path(__file__).with_name("schemas") / "a2a-message-v1.schema.json"
        registry = Registry()
        for path in [*sorted(public_schema_root.glob("*.schema.json")), schema_path]:
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        internal_schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.validator = Draft202012Validator(
            internal_schema, registry=registry, format_checker=FormatChecker()
        )

    def validate_message(self, message: dict[str, Any]) -> None:
        error = next(self.validator.iter_errors(message), None)
        if error is not None:
            location = "/".join(map(str, error.absolute_path)) or "$"
            raise ContractError(f"A2A:{location}: {error.message}")
        if message["message_type"] == "task":
            actor, _, _ = TASK_ROUTES[message["payload"]["task_type"]]
            if message["sender"] != "coordinator" or message["recipient"] != actor:
                raise ContractError("A2A: task route does not match agent ownership")
        elif message["recipient"] != "coordinator" or message["sender"] == "coordinator":
            raise ContractError("A2A: replies must return from an agent to coordinator")

    def validate_reply(self, task: dict[str, Any], reply: dict[str, Any]) -> None:
        """Validate correlation, not replay protection or evidence ownership."""
        self.validate_message(task)
        self.validate_message(reply)
        if task["message_type"] != "task" or reply["message_type"] == "task":
            raise ContractError("A2A: expected a task and a reply")
        for field in ("run_id", "case_id", "task_id"):
            if task[field] != reply[field]:
                raise ContractError(f"A2A: mismatched {field}")
        if reply["in_reply_to"] != task["message_id"]:
            raise ContractError("A2A: mismatched in_reply_to")
        if reply["message_id"] == task["message_id"]:
            raise ContractError("A2A: reply must have a distinct message_id")
        if (reply["sender"], reply["recipient"]) != (task["recipient"], task["sender"]):
            raise ContractError("A2A: mismatched reply actors")
        if reply["message_type"] == "result":
            _, result_type, domain = TASK_ROUTES[task["payload"]["task_type"]]
            payload = reply["payload"]
            if payload["result_type"] != result_type:
                raise ContractError("A2A: unexpected result_type for task")
            if domain is not None and payload["domain"] != domain:
                raise ContractError("A2A: unexpected specialist domain for task")
