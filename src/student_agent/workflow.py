from __future__ import annotations

import asyncio
import copy
import json
import secrets
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from . import business
from .a2a import TASK_ROUTES, A2AContracts
from .contracts import ContractError
from .llm import OpenAIReviewer, validate_review

if TYPE_CHECKING:
    from .mcp_gateway import EvidenceGateway
    from .trace import TraceWriter

TOOLS = {
    "order": ("get_order",),
    "customer": ("get_customer_history",),
    "item": ("get_order_items",),
    "product": ("get_product_context",),
    "shipment": ("get_shipment_summary",),
    "payment": ("get_payment_timeline",),
    "refund": ("get_refund_timeline",),
    "policy": ("get_policy",),
}
PERMISSIONS = {
    "entity-agent": {"order", "customer"},
    "order-agent": {"order", "item", "product"},
    "shipment-agent": {"shipment"},
    "payment-agent": {"payment", "refund"},
    "policy-agent": {"policy"},
    "verifier": set(),
}


class CaseWorkflow:
    """Case-local evidence investigation using discovered, read-only MCP tools."""

    def __init__(
        self,
        case: dict,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        *,
        llm: OpenAIReviewer | None = None,
    ) -> None:
        self.case, self.gateway, self.trace = copy.deepcopy(case), gateway, trace
        self.case_id = case["case_id"]
        self.run_id = secrets.token_hex(12)
        self.contracts = trace.contracts
        self.a2a = A2AContracts(self.contracts.root)
        self.tools: dict[str, dict] = {}
        self.cache: dict[str, dict] = {}
        self.evidence: dict[str, dict] = {}
        self.sources: dict[str, str] = {}
        self.used: set[str] = set()
        self.missing: set[str] = set()
        self.budget = getattr(gateway, "case_budget", {"attempts": 0})
        self.attempts = self.budget["attempts"]
        self.deadline = 0.0
        self.orders: list[str] = []
        self.order_data: dict[str, dict] = {}
        self.raw: dict[str, dict] = {}
        self.facts: dict[str, dict] = {}
        self.entity_conflicts: list[dict] = []
        self.policy_data = None
        self.decision: dict = {}
        self.entity_result: dict = {}
        self.llm = llm
        self.reviews: dict[str, dict] = {}
        self.review_warnings: list[dict[str, str]] = []
        self.preferred_issue: str | None = None

    async def review(self, actor: str, proposal: dict, allowed: list[str] | None = None) -> dict:
        domains = {
            "coordinator": set(),
            "entity-agent": {"order", "customer"},
            "order-agent": {"order", "item", "product"},
            "shipment-agent": {"order", "item", "shipment"},
            "payment-agent": {"order", "item", "payment", "refund"},
        }.get(actor)
        evidence = [
            self.evidence[ref]
            for ref in sorted(self.used)
            if domains is None or self.evidence[ref]["domain"] in domains
        ]
        assert self.llm is not None
        started = asyncio.get_running_loop().time()
        result = await self.llm.review(
            role=actor,
            case=self.case,
            proposal=proposal,
            evidence=evidence,
            allowed_issues=allowed or [],
        )
        # OpenAI has its own request timeout; its latency must not consume the MCP window.
        self.deadline += asyncio.get_running_loop().time() - started
        validate_review(
            result,
            case_id=self.case_id,
            role=actor,
            refs={e["evidence_ref"] for e in evidence},
            allowed_issues=allowed or [],
        )
        self.reviews[actor] = result
        # Observable decisions/token counts, never prompts or private reasoning.
        self.emit(
            "handoff",
            actor,
            target="coordinator",
            decision_code="LLM_REVIEW_COMPLETED",
            evidence_refs=result["evidence_refs"][:20],
            attributes={
                "model": self.llm.model,
                "review_verdict": result["verdict"],
                "proposal_valid": result["proposal_valid"],
                "concern_count": len(result["concern_codes"]),
                "concern_codes": "|".join(result["concern_codes"])[:80],
                "proposal_errors": "|".join(result["proposal_error_codes"])[:80],
                **self.llm.last_usage,
            },
        )
        return result

    def record_review_warnings(self, actor: str, review: dict) -> None:
        """Keep non-deterministic model criticism observable without faking missing evidence."""
        for kind in ("concern_codes", "proposal_error_codes"):
            for code in review[kind]:
                self.review_warnings.append(
                    {"actor": actor, "kind": kind, "code": code}
                )

    @staticmethod
    def confidence_reviewers(primary_issue: str) -> set[str]:
        if primary_issue.startswith("late_delivery"):
            return {"order-agent", "shipment-agent", "policy-agent"}
        if primary_issue in {
            "payment_mismatch",
            "duplicate_charge",
            "valid_split_payment",
            "refund_failed",
            "refund_pending",
        }:
            return {"order-agent", "payment-agent", "policy-agent"}
        if primary_issue in {"canceled_order_paid", "unavailable_order_paid"}:
            return {"entity-agent", "payment-agent", "policy-agent"}
        return {"entity-agent", "order-agent", "shipment-agent", "payment-agent", "policy-agent"}

    def apply_review_confidence(self, decision: dict) -> dict:
        """Cap confidence using reviews that can actually assess the selected issue."""
        reviewers = self.confidence_reviewers(decision["assessment"]["primary_issue"])
        relevant = [r["confidence"] for actor, r in self.reviews.items() if actor in reviewers]
        if relevant:
            # Averaging independent, issue-relevant reviews avoids allowing one unrelated
            # specialist to dominate calibration. Deterministic evidence remains the ceiling.
            review_cap = round(sum(relevant) / len(relevant), 4)
            decision["assessment"]["confidence"] = min(
                decision["assessment"]["confidence"], review_cap
            )
            for claim in decision["claim_assessments"]:
                claim["confidence"] = min(claim["confidence"], review_cap)
        return decision

    def emit(self, event: str, actor: str, **kwargs: Any) -> None:
        attributes = {"run_id": self.run_id, **kwargs.pop("attributes", {})}
        self.trace.emit(
            case_id=self.case_id, event_type=event, actor=actor, attributes=attributes, **kwargs
        )

    def consume(self, actor: str, evidence: dict) -> None:
        ref = evidence["evidence_ref"]
        if ref not in self.evidence:
            raise ContractError("Evidence does not belong to this case attempt")
        self.used.add(ref)
        self.emit("tool_result_consumed", actor, tool_name=self.sources[ref], evidence_refs=[ref])

    def refs(self, *domains: str) -> list[str]:
        return sorted(ref for ref in self.used if self.evidence[ref]["domain"] in domains)

    async def read(self, actor: str, domain: str, **arguments: Any) -> dict | None:
        if domain not in PERMISSIONS[actor]:
            raise PermissionError(f"{actor} cannot read {domain}")
        descriptor = next((self.tools[name] for name in TOOLS[domain] if name in self.tools), None)
        if descriptor is None:
            self.missing.add(f"{domain}:unsupported_tool")
            return None
        name = descriptor["name"]
        args = {"case_id": self.case_id, **arguments}
        if args["case_id"] != self.case_id:
            raise ContractError("Cross-case tool arguments")
        if not Draft202012Validator(descriptor["inputSchema"]).is_valid(args):
            self.missing.add(f"{domain}:unsupported_arguments")
            return None
        if actor != "entity-agent" and "order_id" in args and args["order_id"] not in self.orders:
            raise ContractError("Tool arguments outside resolved entity scope")
        key = json.dumps([name, args], sort_keys=True, separators=(",", ":"))
        if key in self.cache:
            return self.cache[key]
        for retry in range(3):
            remaining = self.deadline - asyncio.get_running_loop().time()
            if self.attempts >= 20 or remaining <= 0:
                self.missing.add(f"{domain}:budget_exhausted")
                return None
            self.attempts += 1
            self.budget["attempts"] = self.attempts
            try:
                evidence = await asyncio.wait_for(
                    self.gateway.call(name, **args), min(30, remaining)
                )
                self.contracts.validate_evidence(evidence)
                if evidence["domain"] != domain:
                    raise ContractError(f"Unexpected evidence domain for {name}")
                ref = evidence["evidence_ref"]
                if ref in self.evidence and self.evidence[ref] != evidence:
                    raise ContractError("Evidence ref reused for different content")
                self.evidence[ref], self.sources[ref], self.cache[key] = evidence, name, evidence
                if evidence.get("warnings"):
                    self.missing.add(f"{domain}:source_warning")
                return evidence
            except (TimeoutError, ConnectionError, OSError):
                if retry == 2:
                    self.missing.add(f"{domain}:unavailable")
                    return None
                await asyncio.sleep(
                    min(2**retry, max(0, self.deadline - asyncio.get_running_loop().time()))
                )
            except RuntimeError:
                self.missing.add(f"{domain}:tool_error")
                self.emit(
                    "handoff",
                    actor,
                    target="coordinator",
                    decision_code="MCP_TOOL_ERROR",
                    tool_name=name,
                    attributes={"domain": domain},
                )
                return None
        return None

    async def dispatch(self, task_type: str, operation: Any, draft: dict | None = None) -> dict:
        actor, _, _ = TASK_ROUTES[task_type]
        task_id = secrets.token_hex(8)
        request = self.case.get("customer_request", {})
        task = {
            "schema_version": "internal-a2a-v1",
            "message_id": secrets.token_hex(12),
            "run_id": self.run_id,
            "case_id": self.case_id,
            "task_id": task_id,
            "in_reply_to": None,
            "sender": "coordinator",
            "recipient": actor,
            "message_type": "task",
            "payload": {
                "task_type": task_type,
                "order_ids": self.orders,
                "claim_ids": [c["claim_id"] for c in request.get("claims", [])],
                "evidence_refs": sorted(self.used),
                "max_new_tool_calls": max(0, 20 - self.attempts),
                "context": {
                    "candidate_order_ids": self.case.get("candidate_order_ids", []),
                    "claimed_order_id": request.get("claimed_order_id"),
                    "customer_unique_id_hint": self.case.get("customer_unique_id_hint"),
                    "policy_version": self.case["policy_version"],
                },
                "draft": draft,
            },
        }
        self.a2a.validate_message(task)
        self.emit(
            "task_assigned",
            "coordinator",
            target=actor,
            attributes={"task_id": task_id, "message_id": task["message_id"]},
        )
        payload = await operation()
        if self.llm is not None and actor not in {"coordinator", "policy-agent", "verifier"}:
            review = await self.review(actor, payload)
            self.record_review_warnings(actor, review)
        reply = {
            **task,
            "message_id": secrets.token_hex(12),
            "in_reply_to": task["message_id"],
            "sender": actor,
            "recipient": "coordinator",
            "message_type": "result",
            "payload": payload,
        }
        self.a2a.validate_reply(task, reply)
        self.emit(
            "handoff",
            actor,
            target="coordinator",
            attributes={"task_id": task_id, "message_id": reply["message_id"]},
        )
        return payload

    def common(self, refs: list[str]) -> dict:
        return {
            "status": "partial" if self.missing else "completed",
            "evidence_refs": refs,
            "missing_evidence": sorted(self.missing)[:20],
            "confidence": 0.65 if self.missing else 0.95,
        }

    async def entity(self) -> dict:
        request = self.case.get("customer_request", {})
        claimed = request.get("claimed_order_id")
        candidates = list(
            dict.fromkeys(([claimed] if claimed else []) + self.case.get("candidate_order_ids", []))
        )
        hint = self.case.get("customer_unique_id_hint")
        history, history_rows = None, []
        if hint:
            history = await self.read("entity-agent", "customer", customer_unique_id=hint)
            if (
                history
                and isinstance(history["data"], dict)
                and history["data"].get("customer_unique_id") == hint
            ):
                history_rows = business.rows(history["data"], "orders")
                self.consume("entity-agent", history)
            else:
                self.missing.add("customer:unverified_identity")
                history = None
        history_ids = {r["order_id"] for r in history_rows if isinstance(r.get("order_id"), str)}
        matches = [c for c in candidates if c in history_ids]
        selected = [claimed] if claimed in matches else matches if len(matches) == 1 else []
        # If the input has no candidates, exactly one history order is unambiguous.
        if not candidates and len(history_ids) == 1:
            selected = sorted(history_ids)
            candidates = selected[:]
        if not selected and not history and claimed:
            selected = [claimed]  # Still requires independent identity checks below.
        for oid in selected:
            evidence = await self.read("entity-agent", "order", order_id=oid)
            if not evidence or not isinstance(evidence["data"], dict):
                continue
            order = evidence["data"].get("order", evidence["data"])
            if order.get("order_id") != oid:
                self.missing.add("entity:order_not_verified")
                continue
            linked = [
                r
                for r in history_rows
                if r.get("order_id") == oid
                and (
                    not order.get("customer_id") or r.get("customer_id") == order.get("customer_id")
                )
            ]
            direct = bool(hint and order.get("customer_unique_id") == hint)
            if not linked and not direct:
                continue
            self.consume("entity-agent", evidence)
            self.order_data[oid] = order
            self.raw[oid] = {"order": evidence}
            for field in ("order_status", "order_purchase_timestamp"):
                if any(
                    r.get(field) is not None and r.get(field) != order.get(field) for r in linked
                ):
                    self.entity_conflicts.append(
                        business.conflict(
                            field, ["order", "customer_history"], "order", "AUTHORITATIVE_ORDER"
                        )
                    )
        self.orders = sorted(self.order_data)
        rejected = [c for c in candidates if history and c not in history_ids]
        evaluations = []
        for oid in candidates:
            status = (
                "accepted"
                if oid in self.orders
                else "rejected"
                if oid in rejected
                else "unresolved"
            )
            evaluations.append(
                {
                    "order_id": oid,
                    "status": status,
                    "reason_code": {
                        "accepted": "ORDER_CUSTOMER_LINK_CONFIRMED",
                        "rejected": "OUTSIDE_CUSTOMER_HISTORY",
                        "unresolved": "AMBIGUOUS_IDENTITY",
                    }[status],
                    "evidence_refs": self.refs("customer", "order"),
                }
            )
        if not self.orders:
            self.missing.add("entity:unresolved")
        self.entity_result = {
            "result_type": "entity",
            **self.common(self.refs("customer", "order")),
            "entity_resolution": {
                "status": "resolved" if self.orders else "ambiguous",
                "resolved_order_ids": self.orders,
                "rejected_candidates": rejected,
                "confidence": 0.97 if self.orders and history else 0.75 if self.orders else 0.1,
            },
            "customer_context": {
                "customer_unique_id": hint if history or self.orders else None,
                "related_order_ids": (self.orders + sorted(history_ids - set(self.orders)))[:20],
            },
            "candidate_evaluations": evaluations,
        }
        return self.entity_result

    def specialist(self, domain: str, field: str, value: dict, refs: list[str]) -> dict:
        findings = (
            [
                {
                    "finding_id": secrets.token_hex(8),
                    "claim_ids": [],
                    "field_path": "/" + field,
                    "value": value,
                    "evidence_refs": refs,
                    "confidence": 0.8,
                }
            ]
            if refs
            else []
        )
        return {
            "result_type": "specialist",
            "domain": domain,
            **self.common(refs),
            "findings": findings,
            "conflicts": [],
        }

    async def order(self) -> dict:
        for oid in self.orders:
            evidence = await self.read("order-agent", "item", order_id=oid)
            data = evidence["data"] if evidence else None
            items = business.item_facts(oid, data)
            self.facts[oid] = {"items": items}
            if evidence:
                self.raw[oid]["item"] = evidence
                self.consume("order-agent", evidence)
            if items["total"] is None:
                self.missing.add("item:unknown_total")
            if self.case.get("investigation_scope", {}).get("include_product_context"):
                product = await self.read("order-agent", "product", order_id=oid)
                if product:
                    contexts = business.rows(product["data"], "products")
                    if contexts and all(
                        str(p.get("product_id")) in items["product_ids"] for p in contexts
                    ):
                        self.raw[oid]["product"] = product
                        self.consume("order-agent", product)
                    else:
                        self.missing.add("product:unverified_scope")
        return self.specialist(
            "order", "affected_entities", self.entities(), self.refs("order", "item", "product")
        )

    def entities(self) -> dict:
        return {
            "order_ids": self.orders,
            "item_ids": sorted({i for g in self.facts.values() for i in g["items"]["item_ids"]}),
            "seller_ids": sorted(
                {i for g in self.facts.values() for i in g["items"]["seller_ids"]}
            ),
            "payment_references": sorted(
                {i for g in self.facts.values() for i in g.get("payment", {}).get("references", [])}
            ),
            "shipment_ids": sorted(
                {
                    i
                    for g in self.facts.values()
                    for i in g.get("shipment", {}).get("shipment_ids", [])
                }
            ),
        }

    def aggregate_shipment(self) -> dict:
        analyses = [g["shipment"]["analysis"] for g in self.facts.values() if "shipment" in g]
        verdicts = {a["verdict"] for a in analyses}
        verdict = (
            next(iter(verdicts))
            if len(verdicts) == 1
            else "conflicting"
            if verdicts
            else "insufficient_evidence"
        )
        return {
            "verdict": verdict,
            "late_seller_ids": sorted({s for a in analyses for s in a["late_seller_ids"]}),
            "timeline_complete": bool(analyses) and all(a["timeline_complete"] for a in analyses),
        }

    async def shipment(self) -> dict:
        for oid in self.orders:
            evidence = await self.read("shipment-agent", "shipment", order_id=oid)
            self.facts[oid]["shipment"] = business.shipment_facts(
                self.order_data[oid],
                evidence["data"] if evidence else None,
                self.facts[oid]["items"],
            )
            if evidence:
                self.raw[oid]["shipment"] = evidence
                self.consume("shipment-agent", evidence)
        return self.specialist(
            "shipment",
            "shipment_analysis",
            self.aggregate_shipment(),
            self.refs("shipment", "item", "order"),
        )

    def aggregate_payment(self) -> dict:
        analyses = [g["payment"]["analysis"] for g in self.facts.values() if "payment" in g]
        priority = [
            "refund_failed",
            "refund_pending",
            "duplicate_capture",
            "capture_mismatch",
            "insufficient_evidence",
            "refunded",
            "reconciled",
        ]
        result = {
            "verdict": next(
                (v for v in priority if any(a["verdict"] == v for a in analyses)),
                "insufficient_evidence",
            )
        }
        for field in ("captured_total_brl", "refunded_total_brl", "refundable_total_brl"):
            values = [business.money(a[field]) for a in analyses]
            result[field] = (
                business.amount(sum(values, business.ZERO))
                if values and None not in values
                else None
            )
        return result

    async def payment(self) -> dict:
        for oid in self.orders:
            payment = await self.read("payment-agent", "payment", order_id=oid)
            refund = await self.read("payment-agent", "refund", order_id=oid)
            self.facts[oid]["payment"] = business.payment_facts(
                self.order_data[oid],
                payment["data"] if payment else None,
                refund["data"] if refund else None,
                self.facts[oid]["items"],
            )
            for domain, evidence in [("payment", payment), ("refund", refund)]:
                if evidence:
                    if (
                        not isinstance(evidence["data"], dict)
                        or evidence["data"].get("order_id") != oid
                    ):
                        raise ContractError(f"Invalid {domain} scope")
                    self.raw[oid][domain] = evidence
                    self.consume("payment-agent", evidence)
        return self.specialist(
            "payment",
            "payment_analysis",
            self.aggregate_payment(),
            self.refs("payment", "refund", "item"),
        )

    def claim_refs(self, topic: str) -> list[str]:
        base = ["order", "customer", "policy"]
        domains = (
            ["shipment", "item"]
            if topic.startswith("late_delivery")
            else ["payment", "refund", "item"]
            if topic
            in {
                "payment_mismatch",
                "duplicate_charge",
                "valid_split_payment",
                "refund_failed",
                "refund_pending",
                "canceled_order_paid",
                "unavailable_order_paid",
            }
            else ["payment", "refund", "item", "shipment", "product"]
        )
        return self.refs(*(base + domains))

    async def policy(self) -> dict:
        evidence = await self.read(
            "policy-agent", "policy", policy_version=self.case["policy_version"]
        )
        if evidence:
            self.policy_data = evidence["data"]
            if (
                not isinstance(self.policy_data, dict)
                or self.policy_data.get("policy_version") != self.case["policy_version"]
            ):
                self.missing.add("policy:version_mismatch")
                self.policy_data = None
            else:
                self.consume("policy-agent", evidence)
        self.decision = business.decide(
            self.case,
            self.order_data,
            self.facts,
            self.policy_data,
            self.missing,
            self.entity_conflicts,
        )
        if self.llm is not None:
            assessment = self.decision["assessment"]
            allowed = [assessment["primary_issue"], *assessment["secondary_issues"]]
            allowed = [issue for issue in allowed if issue != "insufficient_evidence"]
            review = await self.review("policy-agent", self.decision, allowed)
            self.record_review_warnings("policy-agent", review)
            self.preferred_issue = review["selected_primary_issue"]
            self.decision = business.decide(
                self.case,
                self.order_data,
                self.facts,
                self.policy_data,
                self.missing,
                self.entity_conflicts,
                preferred_issue=self.preferred_issue,
            )
            self.apply_review_confidence(self.decision)
        for claim, assessment in zip(
            self.case.get("customer_request", {}).get("claims", []),
            self.decision["claim_assessments"],
            strict=True,
        ):
            assessment["evidence_refs"] = self.claim_refs(claim.get("topic", ""))
        self.emit(
            "policy_decided",
            "policy-agent",
            decision_code=self.decision["assessment"]["primary_issue"].upper(),
            evidence_refs=self.refs("policy"),
            attributes={"status": self.decision["assessment"]["case_status"]},
        )
        result = {
            "result_type": "policy",
            **self.common(sorted(self.used)),
            "policy_version": self.case["policy_version"],
        }
        result.update({k: v for k, v in self.decision.items() if k != "assessment"})
        return result

    def build_output(self) -> dict:
        return {
            "schema_version": "day09-l3b-output-v2",
            "case_id": self.case_id,
            "entity_resolution": self.entity_result["entity_resolution"],
            "customer_context": self.entity_result["customer_context"],
            "affected_entities": self.entities(),
            "shipment_analysis": self.aggregate_shipment(),
            "payment_analysis": self.aggregate_payment(),
            "evidence_refs": sorted(self.used),
            **copy.deepcopy(self.decision),
        }

    async def verify(self, output: dict) -> dict:
        self.contracts.validate_output(output, "workflow output")
        violations = []

        def require(ok: bool, code: str, field: str) -> None:
            if not ok:
                violations.append({"code": code, "field_path": field, "severity": "error"})

        require(output["case_id"] == self.case_id, "CASE_MISMATCH", "/case_id")
        require(set(output["evidence_refs"]) <= self.used, "UNKNOWN_EVIDENCE", "/evidence_refs")
        require(
            not set(output["entity_resolution"]["resolved_order_ids"])
            & set(output["entity_resolution"]["rejected_candidates"]),
            "ENTITY_OVERLAP",
            "/entity_resolution",
        )
        require(
            set(output["affected_entities"]["order_ids"]) == set(self.orders),
            "ENTITY_SCOPE",
            "/affected_entities",
        )
        # Reconstruct numerical findings from raw evidence, not specialist handoffs.
        independent = {}
        for oid, raw in self.raw.items():
            order = raw["order"]["data"].get("order", raw["order"]["data"])
            items = business.item_facts(oid, raw.get("item", {}).get("data"))
            shipment = business.shipment_facts(order, raw.get("shipment", {}).get("data"), items)
            payment = business.payment_facts(
                order, raw.get("payment", {}).get("data"), raw.get("refund", {}).get("data"), items
            )
            independent[oid] = {"items": items, "shipment": shipment, "payment": payment}
            require(independent[oid] == self.facts[oid], "SPECIALIST_RESULT_MISMATCH", "/analysis")
        expected = business.decide(
            self.case,
            self.order_data,
            independent,
            self.policy_data,
            self.missing,
            self.entity_conflicts,
            preferred_issue=self.preferred_issue,
        )
        self.apply_review_confidence(expected)
        for field in (
            "assessment",
            "financial_resolution",
            "data_conflicts",
            "resolution_actions",
            "root_cause_analysis",
        ):
            require(output[field] == expected[field], "DECISION_MISMATCH", "/" + field)
        require(
            output["shipment_analysis"] == self.aggregate_shipment(),
            "SHIPMENT_MISMATCH",
            "/shipment_analysis",
        )
        require(
            output["payment_analysis"] == self.aggregate_payment(),
            "PAYMENT_MISMATCH",
            "/payment_analysis",
        )
        assessment = output["assessment"]
        actions = output["resolution_actions"]
        refund = business.money(output["financial_resolution"]["recommended_refund_brl"])
        monetary_actions = {
            "issue_refund",
            "refund_duplicate_charge",
            "refund_freight",
            "retry_refund",
        }
        require(
            assessment["case_status"] != "no_action" or refund == business.ZERO,
            "NO_ACTION_WITH_REFUND",
            "/financial_resolution/recommended_refund_brl",
        )
        require(
            assessment["case_status"] != "no_action"
            or not monetary_actions.intersection(actions),
            "NO_ACTION_WITH_MONETARY_ACTION",
            "/resolution_actions",
        )
        require(
            assessment["case_status"] != "needs_investigation"
            or bool(actions),
            "INVESTIGATION_WITHOUT_FOLLOWUP",
            "/resolution_actions",
        )
        require(
            assessment["case_status"] != "action_required" or bool(actions),
            "ACTION_REQUIRED_WITHOUT_ACTION",
            "/resolution_actions",
        )
        require(
            assessment["primary_issue"] != "late_delivery_seller"
            or {
                party["party_id"]
                for party in output["root_cause_analysis"]["responsible_parties"]
                if party["party_type"] == "seller"
            }
            == set(output["shipment_analysis"]["late_seller_ids"]),
            "SELLER_RESPONSIBILITY_MISMATCH",
            "/root_cause_analysis/responsible_parties",
        )
        require(
            len(actions) == len(set(actions)),
            "DUPLICATE_ACTION",
            "/resolution_actions",
        )
        for actual, expected_claim in zip(
            output["claim_assessments"], expected["claim_assessments"], strict=True
        ):
            require(
                all(actual[k] == expected_claim[k] for k in ("claim_id", "verdict", "confidence")),
                "CLAIM_MISMATCH",
                "/claim_assessments",
            )
            require(
                set(actual["evidence_refs"]) <= set(output["evidence_refs"]),
                "CLAIM_EVIDENCE_SCOPE",
                "/claim_assessments",
            )
            require(
                actual["verdict"] == "insufficient_evidence" or bool(actual["evidence_refs"]),
                "UNSUPPORTED_ASSERTION",
                "/claim_assessments",
            )
        finance = output["financial_resolution"]
        require(
            sum((business.money(r["amount_brl"]) for r in finance["refund_lines"]), business.ZERO)
            == business.money(finance["recommended_refund_brl"]),
            "REFUND_TOTAL",
            "/financial_resolution",
        )
        for key, evidence in self.cache.items():
            _, args = json.loads(key)
            require(args["case_id"] == self.case_id, "EVIDENCE_SCOPE", "/evidence_refs")
            if evidence["evidence_ref"] in self.used:
                self.contracts.validate_evidence(evidence)
                self.consume("verifier", evidence)
        if self.llm is not None:
            review = await self.review("verifier", output)
            self.record_review_warnings("verifier", review)
            # The model is an independent semantic reviewer, but its free-form
            # error labels are not reproducible enough to be a hard contract
            # gate. Preserve them as audit warnings; the checks above remain
            # authoritative for schema, evidence, scope and computed results.
            for error_code in review["proposal_error_codes"]:
                normalized = "".join(
                    char if char.isalnum() else "_" for char in error_code.upper()
                ).strip("_")
                violations.append(
                    {
                        "code": f"LLM_REVIEW_{normalized}"[:128],
                        "field_path": "/assessment",
                        "severity": "warning",
                    }
                )
        passed = not any(violation["severity"] == "error" for violation in violations)
        self.emit(
            "verification_completed",
            "verifier",
            decision_code="VERIFICATION_PASSED" if passed else "VERIFICATION_FAILED",
            attributes={
                "passed": passed,
                "violations": len(violations),
                "warnings": sum(v["severity"] == "warning" for v in violations),
                "error_codes": "|".join(
                    v["code"] for v in violations if v["severity"] == "error"
                )[:240],
            },
        )
        return {
            "result_type": "verification",
            "passed": passed,
            "violations": violations,
            "required_followups": [],
            "evidence_refs": sorted(self.used),
        }

    async def run(self) -> dict:
        self.deadline = asyncio.get_running_loop().time() + 180
        descriptors = await asyncio.wait_for(self.gateway.discover_tools(), timeout=30)
        if not descriptors:
            raise RuntimeError("MCP Gateway returned no tools")
        self.tools = {tool["name"]: tool for tool in descriptors}
        tasks = [
            ("resolve_entity", self.entity),
            ("analyze_order", self.order),
            ("analyze_shipment", self.shipment),
            ("analyze_payment", self.payment),
            ("decide_policy", self.policy),
        ]
        if self.llm is not None:
            plan = await self.review(
                "coordinator",
                {
                    "mandatory_tasks": [task for task, _ in tasks],
                    "investigation_scope": self.case.get("investigation_scope", {}),
                },
            )
            if plan["investigation_order"] == "payment_first":
                tasks[2], tasks[3] = tasks[3], tasks[2]
        for task, operation in tasks:
            await self.dispatch(task, operation)
        output = self.build_output()
        self.contracts.validate_output(output, "draft output")
        report = await self.dispatch("verify", lambda: self.verify(output), draft=output)
        if not report["passed"]:
            errors = [
                violation["code"]
                for violation in report["violations"]
                if violation["severity"] == "error"
            ]
            detail = ", ".join(errors) if errors else "deterministic invariant"
            raise ContractError(f"Independent verification failed: {detail}; refusing to finalize")
        return output


async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    llm: OpenAIReviewer | None = None,
) -> dict[str, Any]:
    if llm is not None:
        return await CaseWorkflow(case, gateway, trace, llm=llm).run()
    reviewer = OpenAIReviewer.from_env(trace.contracts.root.parent.parent)
    try:
        return await CaseWorkflow(case, gateway, trace, llm=reviewer).run()
    finally:
        await reviewer.close()
