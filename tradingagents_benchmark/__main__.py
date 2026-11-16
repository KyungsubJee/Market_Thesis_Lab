"""Benchmark native TradingAgents graph nodes on one captured SOXL snapshot."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import perf_counter

from research_prototype.week3 import restore_snapshot

from .engine import Recorder, run_native, summarize
from .frozen import FrozenData


CAVEATS = [
    "Original checkout GraphSetup, Propagator, ConditionalLogic, ToolNode, role factories, and prompts execute.",
    "The package initializers and outer TradingAgentsGraph runner are bypassed. No memory settlement, checkpoint, provider clients or native output files.",
    "Vendor functions use projected views of the same frozen SOXL evidence, not live market, macro, news, social or issuer providers.",
    "Model requests use the same Codex CLI transport and model as the prototype. Native prompts are serialized with a fixed-evidence guard, native tool calls are encoded as JSON, and native Pydantic output is translated to a strict JSON schema.",
    "Unsupported technical series, corporate EPS, social posts, news articles, official index/NAV tracking and prediction markets are missing; absence is not evidence of neutrality.",
    "SOXX and SPY represent observed ETF market prices. Neither is an official index total-return series.",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, default=Path("TradingAgents-main"))
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-tool-rounds", type=int, default=20)
    parser.add_argument("--max-calls", type=int, default=96)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    if min(args.timeout, args.max_tool_rounds, args.max_calls) < 1:
        parser.error("Timeout and budgets must be positive")
    if args.output.exists():
        parser.error("Output file already exists")
    capture = json.loads(args.evidence.read_text(encoding="utf-8"))
    fixture = restore_snapshot(capture)
    if fixture["ticker"] != "SOXL" or fixture["horizon"] != "short":
        parser.error("This benchmark adapter requires the captured short-horizon SOXL fixture")
    if capture["model_requested"] != args.model:
        parser.error("Select the same model that produced the prototype capture")
    result = {
        "status": "error", "ticker": fixture["ticker"], "horizon": fixture["horizon"],
        "as_of": fixture["as_of"], "snapshot_sha256": capture["sections"][0]["snapshot_sha256"],
        "source_run_id": capture["run_id"], "model_requested": args.model,
        "mode": "offline_dry_run" if args.dry_run else "actual_codex",
        "evidence_ids": [item["id"] for item in fixture["evidence"]],
        "settings": {
            "analysts": ["market", "social", "news", "fundamentals"],
            "max_debate_rounds": 1, "max_risk_discuss_rounds": 1,
            "max_tool_rounds": args.max_tool_rounds, "max_calls": args.max_calls,
        },
        "caveats": CAVEATS,
    }
    frozen = FrozenData(fixture)
    recorder = None
    started = perf_counter()
    try:
        recorder = Recorder(args.model, args.timeout, args.max_calls, fixture, dry=args.dry_run)
        result["checkout_source_sha256"] = {
            file: sha256((args.checkout / "tradingagents" / file).read_bytes()).hexdigest()
            for file in ("graph/setup.py", "graph/conditional_logic.py", "graph/propagation.py")
        }
        version, final = run_native(args.checkout.resolve(), recorder, frozen, max_tool_rounds=args.max_tool_rounds)
        result.update(status="complete", tradingagents_version=version, final_state=final)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    calls = recorder.calls if recorder else []
    result.update(
        calls=calls, tool_calls=frozen.calls, operations=summarize(calls),
        wall_seconds=round(perf_counter() - started, 3),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")
    print(json.dumps({"status": result["status"], "output": str(args.output), "calls": len(calls),
                      "error": result.get("error")}, ensure_ascii=False), flush=True)
    return int(result["status"] != "complete")


if __name__ == "__main__":
    raise SystemExit(main())
