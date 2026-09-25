# Sprint 1 Plan — MVP trong một buổi

## Mục tiêu

Trong hôm nay, chạy được **một case L3B end-to-end** bằng LangGraph, MCP và OpenRouter/Qwen3 8B; tạo output đúng schema và trace hợp lệ.

Không tối ưu điểm hoặc xử lý toàn bộ edge case trong Sprint 1.

## Thời lượng đề xuất

Khoảng 4–6 giờ.

| Khung thời gian | Công việc |
| --- | --- |
| 30 phút | Cài dependency, kiểm `.env`, validate input |
| 30 phút | Chạy MCP discovery và ghi lại tool schemas |
| 60 phút | Tạo config, LLM client, state và graph skeleton |
| 90 phút | Viết các node điều tra tối thiểu |
| 60 phút | Build output, verifier và trace |
| 30–60 phút | Chạy case 001, sửa lỗi và validate |

## Phạm vi MVP

- Graph chạy tuần tự, không parallel và không persistent checkpoint.
- Một typed state dùng riêng cho từng case.
- MCP tool discovery và evidence ledger đơn giản.
- Entity resolution cho claimed order và candidates.
- Các bước order, shipment, payment/refund và policy tối thiểu.
- Qwen3 8B hỗ trợ tổng hợp/classification bằng structured JSON.
- Python deterministic xử lý số tiền và validation.
- Một vòng verifier, chưa có rework phức tạp.

## Tạm hoãn sang Sprint 2

- Chạy và tối ưu đủ 100 case.
- Per-case call budget nâng cao và cache bền vững.
- Parallel specialist nodes.
- Persistent checkpoint và resume.
- Prompt tuning theo từng topic.
- Conflict resolution nâng cao và confidence calibration.
- Bộ unit test đầy đủ.

## Definition of Done hôm nay

- Dependencies cài thành công.
- `day09 validate-inputs` pass.
- `day09 mcp-tools` trả về danh sách tool.
- `solve_case()` không còn `NotImplementedError`.
- Case `L3B_CASE_001` chạy qua toàn bộ graph.
- Output case 001 pass `l3b-output-v2`.
- Trace có `task_assigned`, `tool_result_consumed`, `handoff` và `verification_completed`.
- Không có secret hoặc evidence giả trong artifact.

## Nguyên tắc cắt scope

Nếu sắp hết thời gian:

1. Ưu tiên schema và evidence hợp lệ.
2. Giữ graph tuần tự.
3. Bỏ LLM ở bước có thể viết rule Python.
4. Chỉ test case 001 và một case payment khác.
5. Không triển khai checkpoint, concurrency hoặc UI.
