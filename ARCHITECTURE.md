# L3B Architecture Record

## 1. System overview

```text
Input -> Entity agent -> Coordinator -> Domain specialists -> Conflict resolver
             |                |                |                     |
             +----------------+-------------- MCP                    |
                                                                    v
                                                          gpt-4o-mini synthesis
                                                                    |
                                                                    v
                                                               Verifier -> Output
                                                                    |
                                                                   Trace
```

Python owns candidate lookup, MCP scope, per-case caching, schema validation and
deterministic invariants. `gpt-4o-mini` receives only the case and validated MCP evidence and
produces one JSON object. It is not allowed to issue tools or create evidence references.

## 2. Agent ownership

| Actor | Input | Responsibility | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity agent | Candidate IDs | Resolve candidates | `get_order` | Order evidence to coordinator |
| Coordinator | Case and entity result | Assign bounded tasks | None | Tasks and final output |
| Customer agent | Resolved customer | Customer context | `get_customer_history` | Evidence to conflict resolver |
| Order/product agent | Resolved order | Item and product context | `get_order_items`, `get_product_context` | Evidence to conflict resolver |
| Shipment agent | Resolved order | Delivery and responsibility | `get_shipment_summary` | Evidence to conflict resolver |
| Payment/refund agent | Resolved order | Capture and refund lifecycle | `get_payment_timeline`, `get_refund_timeline` | Evidence to conflict resolver |
| Policy agent | Policy version | Applicable refund policy | `get_policy` | Policy decision and evidence |
| Conflict resolver | All specialist evidence | Produce schema-shaped decision | OpenAI only, no MCP | Draft output to verifier |
| Verifier | Draft and evidence set | Schema, provenance and consistency checks | None | Verified output or bounded failure |

Tool access follows least privilege. The normal plan makes seven audited MCP calls: one
authoritative order lookup and six scoped specialist lookups. Synthetic `candidate-*` decoys are
rejected without an audited lookup because those calls return no evidence. Payment mismatch and
refund lifecycle topics add one refund timeline lookup.

## 3. Entity resolution and A2A protocol

The entity agent checks each unique candidate with `get_order`. The claimed order is preferred
when authoritative evidence exists; otherwise the first candidate returning authoritative order
evidence is investigated. Agent communication is represented by observable `task_assigned` and
`handoff` events correlated by `case_id`. There are no recursive handoffs or agent-controlled
tool loops.

## 4. Evidence and conflict lifecycle

Every MCP response is validated against `day09-mcp-evidence-v1`. Calls are cached by
`(tool_name, arguments)` within one case only. Every consumed response emits
`tool_result_consumed` with the unchanged `evidence_ref`. The model receives complete evidence
envelopes so it can expose source conflicts in `data_conflicts`; input messages and evidence
values are explicitly treated as untrusted data rather than instructions.

The verifier rejects evidence references absent from the current case, claim references missing
from top-level `evidence_refs`, mismatched claim IDs, overlapping resolved/rejected candidates,
and inconsistent refund totals.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace/event behavior |
| --- | ---: | --- | --- |
| Candidate not found | 0 | Check remaining declared candidate | Handoff only successful evidence |
| Required MCP call fails | 0 | Abort case; never invent evidence | No finalized event |
| Entity not found | 0 | Abort case | No fabricated output |
| Source conflict | 0 | Preserve conflict in output | Conflict resolver handoff |
| Invalid model output | 0 | Abort case | Verifier does not complete |

The fixed plan prevents broad scans. No evidence or cache entry is shared across cases. Independent
verification cross-checks already collected sources instead of repeating audited MCP calls.

## 6. Verification invariants

- Output and input `case_id` match.
- Output passes the public L3B JSON Schema.
- Claim assessments exactly cover input claim IDs.
- Submitted and claim-level evidence references belong to the current evidence set.
- Resolved and rejected candidates do not overlap.
- Recommended refund equals the sum of refund lines within BRL 0.01.
- Confidence values and all enums remain schema bounded.
- A successful case emits assignment, handoff and verification lifecycle events.

## 7. Reproducibility

- Python: 3.11 or newer.
- Model: `gpt-4o-mini`, temperature `0`, one model call per case.
- MCP processing: cases and specialist calls are sequential to keep the MCP stream deterministic.
- MCP cache scope: one case.
- Randomness: no application random seed; trace event IDs use UUIDs.
- Install: `python -m pip install -e ".[dev]"`.
- Run: `day09 run`, then `day09 validate`.
- Required secrets: `COMPETITION_TEAM_API_KEY` and `OPENAI_API_KEY` in `.env`; neither is submitted.
