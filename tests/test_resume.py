import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest

from student_agent import cli


def test_nested_transport_failure_classification():
    assert cli._transport_failure(ExceptionGroup("transport", [httpx2.ReadError("")]))
    assert not cli._transport_failure(
        ExceptionGroup("mixed", [httpx2.ReadError(""), ValueError("schema")])
    )


def test_reconnect_replaces_failed_session(monkeypatch):
    sessions = []

    @asynccontextmanager
    async def connect(*args):
        session = SimpleNamespace()
        sessions.append(session)
        yield session

    solve = AsyncMock(
        side_effect=[
            ExceptionGroup("transport", [httpx2.ReadError("connection lost")]),
            {"case_id": "CASE_064"},
        ]
    )
    monkeypatch.setattr(cli, "connect_gateway", connect)
    monkeypatch.setattr(cli, "solve_case", solve)
    monkeypatch.setattr(cli.asyncio, "sleep", AsyncMock())
    settings = SimpleNamespace(mcp_endpoint="test", team_api_key="test")
    result = asyncio.run(cli._solve_connected(settings, Mock(), {"case_id": "CASE_064"}, Mock()))
    assert result["case_id"] == "CASE_064"
    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]


def test_schema_failure_is_not_retried(monkeypatch):
    @asynccontextmanager
    async def connect(*args):
        yield SimpleNamespace()

    solve = AsyncMock(side_effect=ValueError("schema failure"))
    monkeypatch.setattr(cli, "connect_gateway", connect)
    monkeypatch.setattr(cli, "solve_case", solve)
    settings = SimpleNamespace(mcp_endpoint="test", team_api_key="test")
    with pytest.raises(ValueError, match="schema failure"):
        asyncio.run(cli._solve_connected(settings, Mock(), {"case_id": "CASE_064"}, Mock()))
    assert solve.await_count == 1


def test_resume_requires_output_and_finalization(tmp_path):
    import json

    (tmp_path / "outputs").mkdir()
    (tmp_path / "traces").mkdir()
    for case_id in ("CASE_001", "CASE_002"):
        (tmp_path / "outputs" / f"{case_id}.json").write_text(
            json.dumps({"case_id": case_id}), encoding="utf-8"
        )
    (tmp_path / "traces/trace.jsonl").write_text(
        json.dumps(
            {
                "case_id": "CASE_001",
                "event_type": "case_finalized",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert cli._completed_cases(tmp_path, Mock()) == {"CASE_001"}
