"""Generate a holdings report from fixed or live US evidence."""

import argparse
import json
import os
from pathlib import Path
from time import perf_counter

from framework_validation.runner import FIXTURE_DIR, parse_timestamp

from .model import PrototypeModel
from .presentation import validate_presentation
from .report import collect, load_holdings, load_snapshots, write_report
from .us_data import PublicClient, SEC_AGENT, collect_us_snapshots
from .week3 import attach_validation, restore_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--holdings", type=Path, help="CSV with a ticker column and optional held flag")
    inputs.add_argument("--replay", type=Path, help="Saved single-stock US evidence.json; render without model calls")
    parser.add_argument("--fixtures", type=Path, default=FIXTURE_DIR, help="Directory of fixed evidence snapshots")
    parser.add_argument("--source", choices=("fixed", "us-live"))
    parser.add_argument("--week3", action="store_true", help="Enable single-stock coverage and operational validation")
    parser.add_argument("--reanalyze", action="store_true", help="Run the saved snapshot through the original model")
    parser.add_argument("--presentation", type=Path, help="Editorial decision layout tied to the saved run")
    parser.add_argument("--audit-inputs", action="store_true", help="Save exact role payloads and prompts in evidence.json")
    parser.add_argument("--as-of", help="Optional timezone-aware ISO cutoff for US live evidence")
    parser.add_argument("--sec-user-agent", default=os.environ.get("SEC_USER_AGENT", SEC_AGENT))
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory for report.md and evidence.json")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout in seconds per Codex call")
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("timeout must be positive")
    if args.reanalyze and not args.replay:
        parser.error("--reanalyze requires --replay")
    if args.presentation and (not args.replay or args.reanalyze):
        parser.error("--presentation requires a replay without --reanalyze")
    if args.replay and (args.source or args.as_of or args.model):
        parser.error("replay takes the source, cutoff, and model from its capture")
    source = args.source or ("us-live" if args.replay else "fixed")
    if args.as_of and source != "us-live":
        parser.error("fixed sources already declare an as-of time in their evidence snapshot")
    if args.week3 and source != "us-live":
        parser.error("Week 3 validation requires US live evidence or a replay")
    if args.output_dir.exists():
        parser.error("output directory already exists")

    started = perf_counter()
    original = None
    presentation = None
    try:
        if args.replay:
            original = json.loads(args.replay.read_text(encoding="utf-8"))
            fixture = restore_snapshot(original)
            if args.presentation:
                presentation = validate_presentation(
                    original, json.loads(args.presentation.read_text(encoding="utf-8")),
                )
            holdings = [fixture["ticker"]]
            snapshots = {fixture["ticker"]: (None, fixture)}
            model_name = original["model_requested"]
        else:
            holdings = load_holdings(args.holdings)
            if args.week3 and len(holdings) != 1:
                raise ValueError("Week 3 validation requires exactly one held ticker")
            cutoff = parse_timestamp(args.as_of) if args.as_of else None
            snapshots = (
                collect_us_snapshots(holdings, requested_cutoff=cutoff, client=PublicClient(args.sec_user_agent))
                if source == "us-live" else load_snapshots(args.fixtures)
            )
            model_name = args.model or "gpt-6.1-sol"
        args.output_dir.mkdir(parents=True, exist_ok=False)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))

    if original and not args.reanalyze:
        run = {**original}
        attach_validation(run, mode="render_replay", source_run=original)
        if presentation is not None:
            run["presentation"] = presentation
    else:
        def model_factory():
            if args.audit_inputs:
                return PrototypeModel(model_name, args.timeout, 20, audit_inputs=True)
            return PrototypeModel(model_name, args.timeout, 20)

        run = collect(holdings, snapshots, model_factory, model_name, source_mode=source)
        if args.week3 or original:
            attach_validation(
                run, mode="frozen_reanalysis" if original else "fresh", source_run=original,
                wall_seconds=round(perf_counter() - started, 3),
            )
    write_report(args.output_dir, run)
    print(args.output_dir.resolve() / "report.md")
    print(args.output_dir.resolve() / "evidence.json")
    return int(any(
        section["status"] == "execution_error" or (source == "us-live" and section.get("path") == "blocked")
        for section in run["sections"]
    ))


if __name__ == "__main__":
    raise SystemExit(main())
