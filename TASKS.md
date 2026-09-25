# Sprint 1 Tasks — One-day MVP

## P0 — Phải hoàn thành

### S1-01 — Setup (30 phút)

- [ ] Thêm `langgraph` và OpenAI-compatible client vào `pyproject.toml`.
- [ ] Cài project với dev dependencies.
- [ ] Kiểm các biến OpenRouter trong `.env`.
- [ ] Đưa `case-set.json` và `inputs/` đúng vị trí.
- [ ] Chạy `day09 validate-inputs`.

### S1-02 — MCP discovery (30 phút)

- [ ] Chạy `day09 mcp-tools`.
- [ ] Ghi tên và arguments của các tool cần cho order, customer, shipment, payment, refund và policy.
- [ ] Chọn tập tool tối thiểu cho case 001.

**Phụ thuộc:** S1-01.

### S1-03 — Config và clients (45 phút)

- [ ] Mở rộng `Settings` cho OpenRouter.
- [ ] Tạo Qwen3 8B client với temperature `0`, timeout và JSON output.
- [ ] Tạo MCP helper lưu `evidence_ref` theo case.
- [ ] Không log API key.

**Phụ thuộc:** S1-01, S1-02.

### S1-04 — State và graph skeleton (45 phút)

- [ ] Tạo `InvestigationState` tối thiểu.
- [ ] Tạo graph tuần tự: entity → order → shipment → payment → policy → build → verify.
- [ ] Kết nối compiled graph vào `solve_case()`.
- [ ] Emit `task_assigned` và `handoff` tại node boundaries.

**Phụ thuộc:** S1-03.

### S1-05 — Investigation nodes (90 phút)

- [ ] Resolve claimed order và candidates.
- [ ] Lấy order/customer context.
- [ ] Lấy shipment evidence và verdict.
- [ ] Lấy payment/refund evidence và tính totals.
- [ ] Lấy policy áp dụng.
- [ ] Emit `tool_result_consumed` cho evidence thực sự dùng.

**Phụ thuộc:** S1-04.

### S1-06 — Output và verifier (60 phút)

- [ ] Map state sang đủ field bắt buộc của L3B output.
- [ ] Tạo claim assessments.
- [ ] Tính refund bằng Python, không nhờ model.
- [ ] Validate output bằng `Contracts.validate_output()`.
- [ ] Emit `policy_decided` và `verification_completed`.

**Phụ thuộc:** S1-05.

### S1-07 — End-to-end (30–60 phút)

- [ ] Chạy riêng `L3B_CASE_001` trong lúc phát triển.
- [ ] Kiểm output JSON và trace JSONL.
- [ ] Sửa schema, evidence linkage và consistency errors.
- [ ] Chạy thêm một case payment nếu còn thời gian.
- [ ] Ghi backlog cho Sprint 2.

**Phụ thuộc:** S1-06.

## P1 — Chỉ làm nếu còn thời gian

- [ ] Cache trùng MCP calls trong cùng case.
- [ ] Một retry cho timeout và invalid model JSON.
- [ ] Route `ambiguous`/`not_found` riêng.
- [ ] Một targeted verifier rework.
- [ ] Unit tests cho entity resolution và refund totals.

## Không làm hôm nay

- Parallel graph nodes.
- Persistent checkpoint.
- Chạy/tối ưu đủ 100 case.
- LangSmith, dashboard hoặc UI.
- Autonomous agent conversations.
