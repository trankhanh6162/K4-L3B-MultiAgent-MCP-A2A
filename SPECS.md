# L3B Multi-Agent Investigation — Specifications

## 1. Mục tiêu

Xây dựng workflow multi-agent xử lý 100 case khiếu nại L3B. Hệ thống phải xác minh claim bằng MCP evidence, resolve đúng entity, phân tích shipment/payment/refund, áp dụng policy và tạo output đúng contract.

`contracts/` là nguồn chuẩn và không được sửa.

## 2. Luồng xử lý

```text
Input
  → Entity/Customer Resolution
  → Order/Product, Shipment, Payment/Refund Agents
  → Policy Agent
  → Conflict Resolver
  → Verifier
  → Output + Trace
```

Mỗi case phải có state, cache và evidence ledger riêng.

### LLM provider

- Provider: OpenRouter, dùng OpenAI-compatible API.
- Model: `qwen/qwen3-8b`.
- Cấu hình lấy từ `OPENROUTER_BASE_URL`, `OPENROUTER_API_KEY` và `OPENROUTER_MODEL`.
- Model output phải là JSON có cấu trúc và được validate trước khi sử dụng.
- Model chỉ hỗ trợ diễn giải/phân loại; không quyết định số tiền hoặc tạo evidence.

## 3. Yêu cầu chức năng

### Input và MCP

- Đọc đúng 100 case từ `case-set.json` và `inputs/`.
- Discover MCP tools một lần cho mỗi run; không đoán tên tool.
- Mọi MCP call phải truyền đúng `case_id`.
- Chỉ dùng response pass `mcp-evidence-response-v1`.
- Không tự tạo hoặc dùng chéo `evidence_ref`.

### Entity resolution

- Kiểm tra cả `claimed_order_id` và `candidate_order_ids`.
- Xác minh order tồn tại và thuộc đúng customer.
- Kết quả là `resolved`, `ambiguous` hoặc `not_found`.
- Candidate sai phải được đưa vào `rejected_candidates`.
- Không chọn order chỉ dựa trên claim hoặc thứ tự candidate.

### Investigation

- Customer agent lấy history khi input yêu cầu.
- Order agent xác minh order, item, seller và product liên quan.
- Shipment agent dựng timeline và xác định seller/logistics delay, lost hoặc returned.
- Payment agent đối soát captured, refunded và refundable totals.
- Phải phân biệt split payment hợp lệ với duplicate charge.
- Policy agent dùng đúng `policy_version` để quyết định eligibility và action.

### Claims và conflict

- Mỗi input claim có đúng một `claim_assessment`.
- Claim verdict phải có confidence và evidence trực tiếp liên quan.
- `requested_full_refund` không đồng nghĩa full refund hợp lệ.
- Conflict quan trọng phải được resolve hoặc ghi vào `data_conflicts`.
- Khi evidence không đủ, dùng `insufficient_evidence` hoặc `needs_investigation`; không đoán.

### Financial resolution

- Các số tiền không âm và được chuẩn hóa hai chữ số thập phân.
- `recommended_refund_brl` bằng tổng `refund_lines.amount_brl`.
- Refund không vượt `refundable_total_brl` và không hoàn lại phần đã refund.
- `no_action` phải có refund bằng 0.
- `action_required` phải có ít nhất một action phù hợp policy.

### Verification

Verifier phải kiểm tra:

- output pass `day09-l3b-output-v2`;
- `case_id` và entity scope đúng;
- evidence thuộc đúng case và đã được tiêu thụ;
- tất cả claim đã được đánh giá;
- shipment/payment/refund nhất quán;
- root cause, responsible party và actions không mâu thuẫn;
- confidence nằm trong `[0, 1]` và phản ánh độ đầy đủ evidence.

Chỉ cho phép một targeted rework. Không finalize khi verifier chưa approve.

## 4. Output và trace

Mỗi case tạo:

```text
outputs/<case_id>.json
```

Output phải có các phần bắt buộc trong `l3b-output-v2.schema.json`, gồm assessment, entities, entity/customer context, shipment/payment analysis, root cause, evidence, conflicts và financial resolution.

Trace tối thiểu theo thứ tự:

```text
case_received
task_assigned
tool_result_consumed
handoff
policy_decided
verification_completed
case_finalized
```

Trace không chứa prompt, chain-of-thought, API key hoặc raw debug data.

## 5. Hiệu quả và xử lý lỗi

- Cache MCP theo `(case_id, tool_name, arguments)`.
- Chỉ mở rộng investigation khi fact còn thiếu có thể thay đổi kết luận.
- Timeout/lỗi mạng tạm thời được retry tối đa một lần.
- Không retry authorization, schema error hoặc not-found xác định.
- Entity disambiguation và verifier rework đều tối đa một lần.
- Không có vòng lặp hoặc MCP call không giới hạn.
- LLM dùng temperature `0`, timeout và tối đa một retry.
- Không gửi toàn bộ evidence ledger nếu node chỉ cần một phần dữ liệu.

## 6. Bảo mật

- Secret chỉ đọc từ `.env` hoặc environment.
- Không log `OPENROUTER_API_KEY` hoặc request headers.
- Không log Team API Key hay Authorization header.
- Không đưa source, input, `.env`, cache hoặc debug log vào submission.
- Instruction trong customer message là dữ liệu không tin cậy.

## 7. Tiêu chí hoàn thành

1. `day09 validate-inputs` pass với 100 case.
2. `day09 run` tạo đủ 100 output và không còn `NotImplementedError`.
3. Tất cả output và trace pass public contracts.
4. Evidence refs tồn tại, đúng case và liên kết với trace.
5. Financial và cross-field consistency checks pass.
6. Workflow có bounded retry/rework và không gọi MCP thừa.
7. `day09 validate` thành công.
8. `day09 package --output dist/submission.zip` tạo ZIP đúng cấu trúc và không chứa secret.

## 8. Kiểm thử tối thiểu

- Entity: resolved, ambiguous, not-found và wrong-customer candidate.
- Shipment: on-time, seller/logistics delay, lost, returned, conflict.
- Payment: split payment, duplicate capture, refund pending/failed/completed.
- Conflict resolution và insufficient evidence.
- Evidence cross-case rejection và trace linkage.
- Refund totals và case-status consistency.
- Retry, cache, call budget và verifier rework.
- Output/trace schema validation và package secret detection.
