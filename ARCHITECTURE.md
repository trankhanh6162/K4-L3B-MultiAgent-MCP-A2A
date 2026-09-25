# L3B Architecture Record

Tài liệu mô tả các quyết định kiến trúc có thể kiểm chứng của workflow L3B. Trace chỉ ghi sự kiện quan sát được, mã quyết định và evidence đã sử dụng; không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Mỗi case được xử lý độc lập. Coordinator tạo investigation context theo `case_id`, giao việc cho các specialist, tổng hợp kết quả, yêu cầu conflict resolver xử lý nguồn không thống nhất và chỉ tạo output sau khi verifier chấp thuận.

```text
Input case
   │
   ▼
Coordinator ──► Entity/Customer Agent ──► resolved order + customer context
   │                         │
   │                         └──────────────► MCP evidence gateway
   │
   ├────────► Order/Product Agent ─────────► order, item, seller, product evidence
   ├────────► Shipment Agent ──────────────► shipment verdict + timeline
   ├────────► Payment/Refund Agent ────────► captured/refunded/refundable totals
   └────────► Policy Agent ────────────────► applicable policy + allowed resolution
                         │
                         ▼
                 Conflict Resolver
                         │
                         ▼
                      Verifier
                    │           │
              reject/rework   approve
                    │           │
                    └─────► Coordinator ──► output JSON

All observable stages ────────────────────► trace.jsonl
```

MCP là nguồn evidence duy nhất được đưa vào `evidence_refs`. Nội dung khiếu nại chỉ là claim cần xác minh, không phải bằng chứng authoritative. Không có fallback tự tạo dữ liệu khi MCP thiếu thông tin.

## 2. Agent ownership

Tên tool cụ thể được lấy bằng tool discovery lúc khởi động. Quyền dưới đây được áp theo `domain` của MCP response thay vì đoán tên tool.

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Coordinator | Case JSON, discovered tools | Tạo context, lập kế hoạch tối thiểu, giao task, quản lý budget, tổng hợp và tạo output | Không điều tra rộng; chỉ discovery và điều phối | Investigation plan, specialist tasks, final output |
| Entity/customer agent | Claimed order, candidates, customer hint | Xác minh candidate, reject candidate sai, tìm customer duy nhất và related orders | `order`, `customer`; chỉ dùng `payment`/`shipment` khi cần phân giải candidate | `EntityResolutionResult` cho coordinator và specialists |
| Order/product agent | Resolved order IDs | Xác minh order, items, sellers, trạng thái sản phẩm và entity bị ảnh hưởng | `order`, `item`, `seller`, `product` | `OrderProductResult` |
| Shipment agent | Resolved order/items/sellers, opened time | Dựng timeline, xác định đúng hạn, seller delay, logistics delay, lost hoặc returned | `shipment`; `order` nếu timeline cần mốc order authoritative | `ShipmentResult` |
| Payment/refund agent | Resolved order, order totals | Đối soát capture, split payment, duplicate capture, refund và số tiền có thể hoàn | `payment`, `refund`; không tự suy số tiền thiếu | `PaymentRefundResult` |
| Policy agent | Policy version, verified facts | Lấy đúng policy version, ánh xạ facts sang eligibility và action hợp lệ | `policy` | `PolicyDecision` |
| Conflict resolver | Tất cả specialist results | Phát hiện mâu thuẫn, chọn nguồn theo policy/authority/freshness hoặc để unresolved | Không gọi tool mặc định; được yêu cầu đúng một targeted lookup nếu thiếu fact quyết định | `ConflictResolutionResult` hoặc rework request |
| Verifier | Draft output, evidence ledger, trace state | Kiểm schema, scope, totals, claim linkage, consistency và provenance trước finalize | Không gọi tool mặc định; trả rework có giới hạn khi thiếu evidence bắt buộc | `VerificationResult(approved/rework)` |

Mỗi actor chỉ nhận phần context cần thiết. Specialist không sửa state của nhau; coordinator là nơi duy nhất merge kết quả và tạo output cuối.

## 3. Entity resolution và A2A protocol

### 3.1 Candidate resolution

Candidate pool là hợp của `claimed_order_id` và `candidate_order_ids`, sau khi loại trùng. Với từng candidate, entity agent chỉ thu thập các tín hiệu có khả năng phân biệt:

1. Order tồn tại và thuộc đúng customer hoặc liên kết được với `customer_unique_id_hint`.
2. Timeline/order status phù hợp với thời điểm mở case.
3. Items, seller, shipment hoặc payment khớp facts đã được xác minh.
4. Candidate không thuộc customer khác và không mâu thuẫn với evidence authoritative.

Không chọn order chỉ vì nó đứng đầu danh sách hoặc bằng `claimed_order_id`. Candidate có contradiction authoritative bị đưa vào `rejected_candidates`.

Quy tắc kết luận:

- `resolved`: có một candidate thắng rõ ràng, không còn contradiction trọng yếu; confidence đề xuất `>= 0.85`.
- `ambiguous`: từ hai candidate trở lên vẫn hợp lệ hoặc nguồn authoritative mâu thuẫn; confidence `< 0.85`.
- `not_found`: không candidate nào tồn tại/thuộc đúng customer; confidence phản ánh độ đầy đủ của việc xác minh.

Khi `ambiguous` hoặc `not_found`, hệ thống không tạo refund dương nếu chưa có policy và evidence xác định đúng entity. Case chuyển sang `needs_investigation` trừ khi policy cho phép kết luận `no_action` với evidence hiện có.

### 3.2 Internal A2A envelope

```text
{
  message_id, case_id, correlation_id,
  sender, recipient, task_type, payload,
  evidence_refs, attempt, deadline_ms
}
```

- `case_id` và `correlation_id` bất biến trong toàn bộ case.
- `evidence_refs` chỉ chứa ref đã trả về từ MCP cho chính team/run/case hiện tại.
- Handoff chỉ diễn ra khi output của actor đạt internal schema và có đủ field bắt buộc.
- Mỗi task có tối đa một lần rework; coordinator từ chối message có `attempt > 2`.
- Actor không gửi task ngược cho actor đã gọi mình, ngoại trừ rework do coordinator quản lý; nhờ đó tránh vòng lặp.
- Timeout không tạo fact giả. Actor trả trạng thái `incomplete` cùng phần evidence hợp lệ đã có.
- Trace dùng `task_assigned` khi giao việc và `handoff` khi nhận kết quả; không ghi payload chi tiết hoặc suy luận riêng.

## 4. Evidence và conflict lifecycle

### 4.1 Evidence ledger

Mọi MCP response phải được `EvidenceGateway` validate theo `mcp-evidence-response-v1` trước khi sử dụng. Coordinator duy trì ledger riêng cho từng case:

```text
evidence_ref -> {
  case_id, tool_name, domain, result_hash,
  data, warnings, consumers
}
```

Cache key gồm `(case_id, tool_name, normalized_arguments)`; cache hit không gọi lại MCP. Ledger/cache không được chia sẻ giữa các case.

Khi một actor thực sự dùng evidence để tạo verdict hoặc field output, workflow emit `tool_result_consumed` với đúng `tool_name`, actor và `evidence_ref`. Evidence được gọi nhưng không liên quan không được đưa vào output.

### 4.2 Claim and output linkage

- Mỗi `claim_assessments[i].claim_id` phải tồn tại trong input.
- Evidence của claim phải trực tiếp hỗ trợ hoặc bác bỏ claim đó.
- `output.evidence_refs` là hợp không trùng của evidence thật sự hỗ trợ entity resolution, analysis, conflict resolution, policy và claim assessments.
- Ref dùng trong claim/output phải xuất hiện trong ledger và trong ít nhất một `tool_result_consumed` của cùng case.
- `warnings` từ MCP được giữ trong internal result và làm giảm confidence khi ảnh hưởng đến fact quyết định.

### 4.3 Conflict resolution

Khi hai nguồn khác nhau về cùng một field, conflict resolver:

1. Xác định field và các nguồn liên quan.
2. Áp dụng source precedence do policy evidence quy định.
3. Nếu policy không quy định, ưu tiên bản ghi domain trực tiếp cho chính fact đó, đúng entity và có timeline đầy đủ.
4. Chỉ dùng freshness để phân giải khi hai nguồn có cùng authority.
5. Ghi `data_conflicts` với `sources`, `selected_source` và `resolution_code`.
6. Nếu không thể chọn an toàn, đặt `selected_source = null`, dùng verdict `conflicting`/`insufficient_evidence`, giảm confidence và tránh hành động tài chính không đảo ngược.

Không âm thầm bỏ qua contradiction và không dùng majority vote thay cho authority/policy.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout hoặc lỗi tạm thời | Tối đa 1 retry cùng arguments | Specialist trả `incomplete`; chỉ dùng evidence hợp lệ đã có | `handoff` / `MCP_TIMEOUT_INCOMPLETE` |
| MCP response sai schema/hash/ref | Không dùng response; tối đa 1 targeted re-query | Không đưa response vào ledger/output | `handoff` / `INVALID_EVIDENCE_RESPONSE` |
| Entity not found | 1 lượt kiểm tra candidate pool | `not_found`, không refund dương | `handoff` / `ENTITY_NOT_FOUND` |
| Entity ambiguous | 1 targeted disambiguation lookup | `ambiguous`, giảm confidence, không đoán order | `handoff` / `ENTITY_AMBIGUOUS` |
| Source conflict | 1 policy hoặc targeted lookup | Ghi unresolved conflict và dùng conservative action | `handoff` / `SOURCE_CONFLICT_UNRESOLVED` |
| Invalid specialist result | 1 rework với field lỗi | Verifier từ chối finalize thay vì tạo output giả | `verification_completed` / `SPECIALIST_RESULT_INVALID` |

Efficiency strategy:

- Discovery tool đúng một lần cho cả run.
- Bắt đầu bằng lookup hẹp theo candidate; không quét history trước khi cần.
- Tái sử dụng evidence trong phạm vi case qua cache.
- Chỉ gọi product context khi input yêu cầu hoặc product fact có thể đổi kết luận.
- Chỉ lấy customer history sau khi xác định customer, hoặc khi history cần để resolve entity.
- Policy lookup một lần cho mỗi `(case_id, policy_version)`.
- Conflict resolver/verifier chỉ gửi một targeted request, không chạy lại toàn bộ investigation.
- Không retry song song và không retry lỗi validation/dữ liệu không tồn tại.

Call budget là soft budget cấu hình được, không hard-code theo số tool chưa biết. Coordinator dừng lookup khi entity, verdict, policy, evidence bắt buộc và số tiền đã đủ để verifier kết luận.

## 6. Verification invariants

Verifier phải kiểm tra trước khi `approved`:

### Schema và scope

- Output pass `day09-l3b-output-v2`; `case_id` khớp input/file.
- Không có additional properties hoặc enum tự tạo.
- Mọi entity trong output thuộc resolved case; không trộn dữ liệu giữa case.
- `resolved_order_ids` và `rejected_candidates` không giao nhau.

### Evidence và provenance

- Mọi ref đúng format, tồn tại trong ledger và thuộc case hiện tại.
- Evidence domain liên quan trực tiếp tới field/claim nó hỗ trợ.
- Mọi ref trong output đã có `tool_result_consumed` trong trace.
- Không dùng claim text, candidate hint hoặc model inference như evidence.

### Claims và analysis

- Mỗi input claim có đúng một assessment; không có ID lạ/trùng.
- `primary_issue`, shipment verdict và payment verdict phù hợp evidence.
- `late_seller_ids` chỉ chứa seller đã xác minh và chỉ có khi seller chịu trách nhiệm.
- `timeline_complete=false` làm giảm confidence.

### Financial consistency

- Các total BRL không âm; `refunded_total_brl <= captured_total_brl` khi cả hai đã biết.
- `recommended_refund_brl` bằng tổng `refund_lines.amount_brl`.
- Refund đề xuất không vượt `refundable_total_brl` khi đã biết.
- `no_action` không có refund dương hoặc action yêu cầu hoàn tiền.
- `action_required` có ít nhất một action cụ thể, phù hợp policy.
- Refund đã hoàn tất không bị hoàn lần hai; split payment hợp lệ không bị gắn duplicate charge.

### Root cause, conflicts và confidence

- Cause ranks không trùng và tăng từ 1 theo mức ưu tiên.
- Responsible party nhất quán với root cause, seller delay và actions.
- Mọi conflict trọng yếu được resolve hoặc ghi trong `data_conflicts`.
- Confidence thuộc `[0, 1]`, giảm khi entity ambiguous, timeline thiếu, warning hoặc conflict chưa giải quyết.

### Workflow trace

- Thứ tự tối thiểu: `case_received` → `task_assigned` → `handoff` → `verification_completed` → `case_finalized`.
- `case_finalized` chỉ emit sau khi verifier approved.
- Có từ hai actor thực sự phối hợp; không tạo trace giả cho agent không tham gia.

Verifier trả danh sách error code cho coordinator. Chỉ một vòng rework được phép; nếu vẫn không đạt, workflow dừng case rõ ràng thay vì ghi artifact không hợp lệ.

## 7. Reproducibility

- Runtime: Python 3.11 hoặc 3.12; dependencies lấy từ `pyproject.toml`.
- LLM provider: OpenRouter qua OpenAI-compatible API.
- Model mặc định: `qwen/qwen3-8b`; model ID không hard-code trong node mà lấy từ `OPENROUTER_MODEL`.
- Generation mặc định dùng temperature `0`; model response phải là structured JSON và được validate trước khi merge vào state.
- Entrypoint: `day09 run`; validation: `day09 validate`; packaging: `day09 package --output dist/submission.zip`.
- MCP endpoint, Team API Key và OpenRouter API Key chỉ lấy từ environment; không ghi vào source, output hoặc trace.
- Tool discovery được cache trong memory cho cả run; tên tool không đoán/hard-code nếu có thể ánh xạ theo schema/description.
- Mặc định chạy case tuần tự để trace và rate limit ổn định. Nếu bật concurrency, dùng giới hạn cấu hình nhỏ và ledger/cache tách theo `case_id`.
- Không dùng randomness cho nghiệp vụ. Nếu thêm model sampling, dùng cấu hình xác định và ghi model/config không chứa secret trong deployment record.
- Giá trị tài chính dùng phép làm tròn hai chữ số nhất quán trước khi serialize.
- ZIP cuối chỉ chứa `manifest.json`, `trace.jsonl` và `outputs/<case_id>.json`.

## 8. Implementation boundaries

`solve_case()` là orchestration entrypoint. Có thể tách thành:

```text
workflow.py          coordinator và solve_case
agents/entity.py     entity/customer resolution
agents/order.py      order/product analysis
agents/shipment.py   shipment timeline/verdict
agents/payment.py    payment/refund reconciliation
agents/policy.py     policy decision
conflicts.py         deterministic conflict rules
verification.py      output invariants
models.py            internal typed results/A2A envelope
```

Các rule như cộng tiền, enum mapping, status mapping và validation phải viết bằng code deterministic. Qwen3 8B chỉ hỗ trợ diễn giải/classification trong output có cấu trúc và không được tự tạo evidence, entity, policy hoặc số tiền.
