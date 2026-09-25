# Architecture Decision Record

## ADR-001: Sử dụng LangGraph để điều phối multi-agent

- **Trạng thái:** Accepted
- **Ngày:** 2026-09-25

### Bối cảnh

L3B gồm nhiều bước phụ thuộc và có nhánh: resolve order, điều tra customer/order/shipment/payment, áp dụng policy, xử lý conflict và verification. Workflow cần state riêng cho từng case, retry có giới hạn và trace rõ ràng.

### Quyết định

Sử dụng LangGraph `StateGraph` làm lớp điều phối cho `solve_case()`.

```text
START
  → Entity Resolver
  → Customer Context
  → Order / Shipment / Payment Agents
  → Policy Agent
  → Conflict Resolver
  → Verifier
      ├─ approved → Finalize → END
      └─ rework → Targeted Lookup → Verifier
```

Mỗi case có một `InvestigationState` riêng, gồm input, entity đã resolve, evidence ledger, specialist results, conflicts, call budget, draft output và verification result.

LangGraph chỉ quản lý state và routing:

- MCP call vẫn đi qua `EvidenceGateway`.
- Output/evidence/trace vẫn được `Contracts` kiểm tra.
- Competition trace vẫn được ghi bằng `TraceWriter`.
- Tính tiền, schema validation và consistency checks dùng Python deterministic.
- Tối đa một lần entity disambiguation và một lần verifier rework.

### Lý do chọn

- Luồng agent và handoff được biểu diễn rõ bằng node/edge.
- Conditional routing phù hợp với entity `resolved`, `ambiguous`, `not_found`.
- Shared state giúp tái sử dụng evidence, tránh gọi MCP lặp.
- Retry và rework có thể giới hạn tại từng node.
- Dễ kiểm thử từng agent và quan sát trạng thái giữa các bước.
- Có thể chạy song song các specialist sau khi resolve đúng order.

### Hạn chế

- Thêm dependency và độ phức tạp orchestration.
- LangGraph không tự bảo đảm provenance hoặc business correctness.
- Checkpoint và concurrency có thể gây duplicate MCP calls nếu quản lý sai.

### Guardrails

- State, cache và evidence phải tách biệt theo `case_id`.
- Không tự tạo evidence hoặc dùng evidence chéo case.
- Mọi vòng lặp đều có giới hạn.
- Chỉ gọi MCP bổ sung theo targeted lookup.
- `case_finalized` chỉ được ghi sau khi verifier approve.
- Không dùng LangGraph trace thay cho `trace.jsonl` của cuộc thi.

### Hệ quả

- Thêm `langgraph` vào dependencies.
- `solve_case()` gọi compiled graph và trả output cuối.
- Cần triển khai typed state, graph nodes, routing functions và unit tests.
- Không sửa public contracts để phù hợp framework.

## ADR-002: Sử dụng OpenRouter và Qwen3 8B

- **Trạng thái:** Accepted
- **Ngày:** 2026-09-25

### Quyết định

Dùng OpenRouter qua API tương thích OpenAI với model `qwen/qwen3-8b`. Model hỗ trợ các node cần diễn giải hoặc phân loại; logic entity matching, tính tiền, validation và consistency vẫn dùng Python deterministic.

### Lý do

- Model 8B có năng lực reasoning tốt hơn model 4B nhưng vẫn nằm dưới giới hạn 10B.
- OpenRouter cung cấp một API thống nhất, dễ thay model bằng cấu hình.
- Qwen hỗ trợ đa ngôn ngữ và phù hợp structured agent workflow.

### Guardrails

- Model name, API URL và API key chỉ lấy từ environment.
- Dùng temperature `0` và yêu cầu structured JSON output.
- Validate mọi model response trước khi cập nhật graph state.
- Model không được tự tạo evidence, entity ID, policy hoặc số tiền.
- Lỗi model tối đa một retry; không fallback sang model khác ngoài cấu hình.
