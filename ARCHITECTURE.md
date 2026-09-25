# L3B Architecture Record

## 1. Triển khai hiện tại

Điểm vào: `src/student_agent/workflow.py::solve_case(case, gateway, trace)`.
Python async điều phối A2A nội bộ; OpenAI `gpt-4o-mini` tham gia đủ 7 vai trò.
`llm.py` dùng Chat Completions với strict JSON Schema, temperature=0 và store=false.
Các phép tính và điều kiện evidence nằm trong `business.py`, tách khỏi transport.
Không dùng case number, pattern ID hoặc claim topic làm đáp án. Topic chỉ ưu tiên
vấn đề đã được evidence xác nhận; nội dung message không được thực thi như chỉ thị.

Coordinator đọc case để chọn shipment-first/payment-first, không được bỏ task bắt buộc.
Entity/Order/Shipment/Payment dùng LLM đọc evidence của domain mình và review kết luận
trước handoff. Concern hoặc proposal error của LLM được ghi vào audit; chỉ kiểm tra
deterministic xác nhận thiếu evidence, sai scope hoặc conflict chưa phân xử mới làm kết quả
cần investigation. Policy LLM chọn
primary issue trong tập đã được evidence/rules xác nhận; Python áp dụng policy và
tính lại tiền. Verifier dùng LLM kiểm tra draft với raw evidence, đồng thời chạy
invariant checks. Lỗi invariant xác định là hard gate. Nhận xét LLM chưa được một
invariant xác nhận được giữ thành warning trong audit, không tự chặn một output hợp lệ.
Review tách concern_codes (case còn bất định nhưng output có thể đúng) khỏi
proposal_error_codes (model cho rằng proposal sai và cần được kiểm tra lại).
proposal_valid được chuẩn hóa từ proposal_error_codes vì JSON Schema không kiểm tra
quan hệ giữa các field; chỉ invariant xác định mới là lỗi chặn ở bước verifier cuối.

Đây là kiến trúc hybrid có giới hạn rõ: LLM không tùy ý sửa số tiền, tạo ref, chọn
issue không có chứng cứ hoặc gọi tool ngoài quyền. Review schema được validate tại
API lẫn local. Confidence deterministic là mức trần; review chỉ lấy từ các agent liên quan
đến primary issue rồi lấy trung bình để tránh một domain không liên quan kéo sai calibration.
LLM không thể thay thế evidence server còn thiếu. Không fallback rule-only khi API lỗi.

Đã nối với MCP SDK 2.2 và kiểm tra response thật. Hỗ trợ 10 nhóm: canceled/unavailable
order paid, seller/logistics delay, valid split payment, payment mismatch, explicit
duplicate capture, refund pending/failed, unsupported claim. Dữ liệu thiếu vẫn cần
investigation; không bảo đảm điểm tối đa khi server thiếu hoặc mâu thuẫn evidence.

## 2. Public contracts

Giữ nguyên `contracts/schemas/`: l3b-output-v2, l3a-output-v2 (shared definitions),
trace-event-v1, submission-manifest-v2, mcp-evidence-response-v1. JSON Schema là nguồn
chuẩn về field, enum, bounds và required. Kiểm thử SHA-256 (chuẩn hóa LF) phát hiện
thay đổi public schema. Schema nội bộ nằm tại `src/student_agent/schemas/`.
Không thêm metadata nội bộ vào output, manifest hoặc MCP envelope.

## 3. Luồng A2A và quyền agent

```mermaid
flowchart LR
    C[Coordinator] --> E[Entity / Customer]
    E --> O[Order / Product]
    O --> S[Shipment]
    S --> P[Payment / Refund]
    P --> R[Policy / Conflict]
    R --> V[Verifier]
    V --> F[Validated output]
```

Các agent chạy tuần tự để chia sẻ dữ liệu và tránh calls trùng. Dispatcher validate
cả task lẫn reply, giữ run_id/case_id/task_id, kiểm tra sender/recipient và loại result.
Đây là protocol nội bộ, không tuyên bố tương thích transport A2A bên ngoài.

| Actor | Tool được phép | Handoff |
| --- | --- | --- |
| coordinator | Discovery, không đọc domain trực tiếp | Task và draft |
| entity-agent | get_customer_history, get_order | Identity, rejected candidates, customer context |
| order-agent | get_order, get_order_items, get_product_context | Entities và item/product context |
| shipment-agent | get_shipment_summary | Timeline, verdict, seller responsibility |
| payment-agent | get_payment_timeline, get_refund_timeline | Capture/refund reconciliation |
| policy-agent | get_policy | Rule, action, financial decision, conflicts, claims |
| verifier | Không gọi tool mới; đọc evidence gốc | Report pass/fail |

`get_payment_timeline` đã chứa base payments nên không gọi thừa `get_order_payments`.
Seller IDs lấy từ item evidence; không gọi `get_sellers` nếu không cần thêm thông tin.
Tên tool phải có trong discovery; validate inputSchema trước gọi. SDK dùng
PaginatedRequestParams, input_schema, next_cursor, is_error và structured_content.

## 4. Entity resolution

Đọc history bằng customer hint; kiểm tra customer_unique_id của response. Intersect
candidate IDs với order history, sau đó xác minh bằng get_order và customer_id.
Claimed ID chỉ được ưu tiên trong các candidate đã có linkage. Candidate không
thuộc history được đánh dấu rejected với evidence customer. Nhiều candidate hợp lệ
không có tiêu chí phân biệt thì ambiguous; không chọn phần tử đầu tiên.

Order không có customer_unique_id vẫn resolve được qua customer_id/order history.
Nếu history có nhiều snapshot khác nhau của cùng order, ghi conflict và chọn order
row vì discovery mô tả get_order là nguồn authoritative. Related orders chỉ là context,
không tự động đưa vào affected_entities. Chỉ gửi tool chuyên trách trong resolved scope.

## 5. Business rules và adapter

- Items là list; order_item_id là khóa item. Không cộng hai snapshot của cùng item
  thành hai món hàng. Giá/freight khác nhau cho cùng ID tạo conflict và total unknown.
- Product context lấy theo order_id. Kiểm tra product ID thuộc item đã xác minh.
- Shipment dùng delivered_carrier_at, delivered_customer_at, estimated_delivery_at,
  shipping_limits và events. Seller handoff muộn và customer delivery muộn được phân
  biệt. Event confirmed có actor hỗ trợ kết luận, nhưng summary trái event phải giữ
  conflict; không tự tạo source precedence mà policy chưa quy định.
- Payment dùng events captured/duplicate_capture và amount_brl. Không nhân số tiền
  với installments. Snapshot cùng transaction/capture ID được đếm một lần; version
  khác amount tạo conflict. Dòng base payments không tự chứng minh capture đã xảy ra.
- Duplicate charge cần explicit duplicate_capture/duplicate_of, không suy chỉ vì có
  hai payments. Split payment hợp lệ khi nhiều sequential và capture khớp invoice.
- Refund grouped theo refund_id/refund_reference, dùng trạng thái cuối lifecycle và
  chỉ cộng settled amounts một lần. Không coi requested/pending/failed là đã hoàn.
  Nếu không có ID và nhiều amounts không phân biệt được, để unknown và báo conflict.
- Decimal xử lý BRL. Missing/refund tool error không đổi thành 0. Explicit events=[]
  trong response đúng scope là evidence không có refund event; response lỗi không phải.
- Policy phải đúng version và BRL. Rule chỉ áp dụng sau khi issue được chứng minh.
  Số tiền đề xuất bị chặn bởi số dư, pending refund và amount thuộc duplicate/failed
  refund nếu có. Không áp dụng party_id seller ngoài scope từ một rule dùng chung.
- Khi evidence bắt buộc thiếu hoặc conflict chưa phân xử, không đề xuất hoàn tiền.
  Primary issue đã xác minh vẫn được giữ; case_status có thể needs_investigation.
- Đánh giá từng claim, kể cả full-refund claim; trả supported/unsupported/partial/
  insufficient theo evidence, không chép topic thành đáp án. Confidence giảm khi có
  conflict hoặc thiếu dữ liệu. Các mức 0.95/0.85/0.70/0.55 là heuristic, chưa calibration
  trên feedback scorer riêng tư.

## 6. Evidence và trace

Evidence cache theo instance case/run, keyed bởi tool + canonical arguments có case_id.
Giữ nguyên envelope, evidence_ref và result_hash. Không tự tạo/ref sửa hash, không
reuse evidence từ lần chạy trước. Chỉ schema-valid evidence được giữ trong store.
`read()` lấy evidence; agent gọi `consume()` sau khi kiểm tra và sử dụng dữ liệu.
Output refs chỉ gồm evidence đã tiêu thụ. Claim refs lọc theo domain hỗ trợ claim.
Không ghi việc validate envelope đơn thuần thành chứng minh nghiệp vụ.

Trace có task_assigned, handoff, tool_result_consumed, policy_decided,
verification_completed. CLI chịu trách nhiệm case_received/case_finalized.
Attributes chỉ chứa scalar và IDs, không prompts, secrets hay chain-of-thought.
Handoff `LLM_REVIEW_COMPLETED` ghi model trả về, token usage và review verdict thực tế.
Một case đầy đủ dùng 7 logical LLM calls; SDK retry tối đa 1 lần/request, timeout 45s.
LLM nhận case/evidence, không nhận nội dung .env hoặc Team API Key.
Evidence từ failed attempt vẫn ở trace thực tế nhưng không trộn vào output attempt sau.
Server audit độc lập; client trace không thay thế server provenance.

## 7. Budget, failure và resume

Budget 20 MCP attempts/case, chia sẻ qua các lần reconnect. Tối đa 3 attempts cho
một tool đọc khi có TimeoutError/ConnectionError/OSError; backoff 1s/2s. Timeout
30s/call; cửa sổ cấp call của một workflow attempt là 180s, không tính thời gian LLM.
Runtime tool error không
retry mù, ghi missing và handoff lỗi. Schema/scope violation dừng case.

Transport failure trong SDK TaskGroup khiến session không dùng lại được: CLI đóng
session, mở session mới và chạy lại case, tối đa 2 reconnect. Không dùng lại ref của
attempt trước. Calls ở attempt thất bại vẫn tiêu audit/budget. Không catch lỗi schema
như lỗi mạng. Chưa chạy specialist đồng thời, do đó không có in-flight duplicate.

`day09 run --resume` chỉ bỏ qua output pass schema và có case_finalized trong trace.
Nó không cập nhật output cũ khi code thay đổi. `day09 run` là lượt chạy mới và dọn
output/trace cũ. Không sửa inputs hoặc case-set.json trong mọi chế độ chạy.

## 8. Verifier và kiểm thử

Verifier đọc raw evidence để dựng lại items/shipment/payment, so sánh với handoff,
kiểm tra policy decision, claim verdict, refs/ownership, resolved/rejected overlap,
affected scope, totals và JSON Schema. Sai invariant thì không finalize. Verifier
hiện dùng lại các pure functions để tránh lệch định nghĩa; các test nghiệp vụ độc lập
kiểm tra số tiền và kết quả mong đợi, không chỉ so sánh implementation với chính nó.

Kiểm thử offline:

```powershell
python -m pytest tests/test_starter.py tests/test_a2a_contracts.py tests/test_workflow.py tests/test_business.py tests/test_mcp_gateway.py tests/test_resume.py tests/test_llm.py -q
```

Tests gồm 10 nhóm nghiệp vụ, claim sai, missing sources, monetary precision,
refund lifecycle, scope violation, duplicate item snapshots, conflict, SDK pagination
và reconnect. test_release_safety kiểm tra repo phát hành rỗng, không áp dụng assertion
không có inputs trong workspace học viên đã tải bộ case; không xóa input để pass test.

Smoke test MCP thật (trước khi tích hợp LLM) đã chạy case 035 và 009 với trace tạm,
không ghi đè outputs. Bản LLM được kiểm thử bằng API mock; chưa chạy OpenAI thật khi
chưa có OPENAI_API_KEY. Không coi test mock là bằng chứng gọi model thật.
035 resolve được và kết luận refund_pending; 009 resolve được unavailable_order_paid
nhưng server trả tool error cho refund timeline. Những kết quả này xác nhận luồng thật,
không chứng minh điểm chấm riêng tư hoặc tất cả 100 case hoàn hảo.

## 9. Chạy lại và đóng gói

Thêm OPENAI_API_KEY vào .env và OPENAI_MODEL=gpt-4o-mini. CLI kiểm tra cấu hình trước
khi dọn output cũ. Chạy `python -m pip install -e ".[dev]"` để cập nhật dependencies.
Không dùng --resume với output cũ nếu muốn chạy lại qua LLM.

```powershell
day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Không dùng --resume để làm mới output do solver cũ tạo. Nếu lượt chạy mới bị gián đoạn,
dùng --resume để tiếp tục. ZIP chỉ có manifest.json, trace.jsonl, outputs/<case_id>.json.
Không đóng gói source, input, .env, API keys hoặc diagnostic files.

Python >=3.11, MCP 2.x, OpenAI SDK 2.x; dependency ranges trong pyproject.toml.
LLM temperature=0 không bảo đảm kết quả hoàn toàn xác định. Metadata model nằm trong
metadata.json ở root, chỉ dùng nội bộ, không đưa vào submission ZIP. GPT-4o-mini
không có số tham số được công bố trong model card; under_10b_verified=false, không
khai báo đáp ứng giới hạn <10B khi chưa có căn cứ. Model giữ đúng lựa chọn của user.
ID trace ngẫu nhiên. Chưa có dependency lock;
ghi lại pip freeze của môi trường thi nếu cần tái hiện đúng versions.
