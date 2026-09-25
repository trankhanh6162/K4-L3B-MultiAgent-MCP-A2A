from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx2

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .llm import OpenAIReviewer
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


def _transport_failure(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(_transport_failure(e) for e in exc.exceptions)
    return isinstance(exc, (httpx2.TransportError, ConnectionError, TimeoutError))


def _completed_cases(root: Path, contracts: Contracts) -> set[str]:
    """Only skip validated outputs with a recorded finalization."""
    trace_path = root / "traces" / "trace.jsonl"
    finalized = set()
    if trace_path.exists():
        for number, line in enumerate(trace_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            event = json.loads(line)
            contracts.validate_trace(event, f"trace:{number}")
            if event["event_type"] == "case_finalized":
                finalized.add(event["case_id"])
    completed = set()
    for path in (root / "outputs").glob("*.json"):
        output = json.loads(path.read_text(encoding="utf-8"))
        contracts.validate_output(output, path.name)
        if output["case_id"] != path.stem:
            raise ValueError(f"{path.name}: mismatched case_id")
        if path.stem in finalized:
            completed.add(path.stem)
    return completed


async def _solve_connected(settings, contracts, case, trace, *, llm=None):
    # A transport TaskGroup failure invalidates the session. Retry outside its
    # context, never reuse the cancelled session or the previous attempt's refs.
    budget = {"attempts": 0}
    for attempt in range(3):
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                gateway.case_budget = budget
                output = await solve_case(case, gateway, trace, llm=llm)
                contracts.validate_output(output, f"outputs/{case['case_id']}.json")
                if output.get("case_id") != case["case_id"]:
                    raise ValueError("solver returned a mismatched case_id")
            return output
        except Exception as exc:
            if not _transport_failure(exc):
                raise
            if attempt == 2:
                raise RuntimeError(
                    f"{case['case_id']}: MCP connection failed after 3 attempts. "
                    "Existing outputs preserved; retry with day09 run --resume."
                ) from exc
            print(
                f"{case['case_id']}: MCP disconnected; reconnect {attempt + 1}/2",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.sleep(2**attempt)


async def _run(root: Path, *, resume: bool = False) -> None:
    # Validate the LLM credentials before touching any previous artifacts.
    llm = OpenAIReviewer.from_env(root)
    try:
        await _run_cases(root, resume=resume, llm=llm)
    finally:
        await llm.close()


async def _run_cases(root: Path, *, resume: bool, llm) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    completed = _completed_cases(root, contracts) if resume else set()
    if not resume:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    for index, case_id in enumerate(case_set.case_ids, 1):
        if case_id in completed:
            print(f"[{index}/{len(case_set.case_ids)}] {case_id}: skipped", flush=True)
            continue
        print(f"[{index}/{len(case_set.case_ids)}] {case_id}: running", flush=True)
        case = case_set.cases[case_id]
        trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
        output = await _solve_connected(settings, contracts, case, trace, llm=llm)
        contracts.validate_output(output, f"outputs/{case_id}.json")
        if output.get("case_id") != case_id:
            raise ValueError(f"solver returned a mismatched case_id for {case_id}")
        target = output_root / f"{case_id}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(target)
        trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
        assessment = output["assessment"]
        print(
            f"{case_id}: saved | {assessment['primary_issue']} | "
            f"{assessment['case_status']} | {len(output['evidence_refs'])} evidence refs",
            flush=True,
        )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--resume",
        action="store_true",
        help="preserve output/trace and skip validated finalized cases",
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, resume=args.resume))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
