"""Run the normal portfolio CLI once, retaining incremental phase and call evidence."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import functools
import json
from pathlib import Path
import sys
from threading import Lock, local
from time import perf_counter
import traceback

from framework_validation.compact_model import CompactCodexModel
from framework_validation.live_runner import CodexModel
from research_prototype import business_discovery, integrated
from research_prototype.us_data import PublicClient
from research_prototype.week3 import measurement


def stamp():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


class Monitor:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "calls").mkdir()
        self.started_at = stamp()
        self.started = perf_counter()
        self.lock = Lock()
        self.context = local()
        self.calls = {}
        self.records = {}
        self.live_records = {}
        self.phases = []

    def write(self, filename, value):
        path = self.directory / filename
        temporary = path.with_suffix(path.suffix + ".partial")
        temporary.write_text(encode(value), encoding="utf-8")
        temporary.replace(path)

    def event(self, event, **fields):
        row = {"event": event, "at_utc": stamp(), "elapsed_seconds": round(perf_counter() - self.started, 3), **fields}
        with self.lock:
            with (self.directory / "events.jsonl").open("a", encoding="utf-8") as output:
                output.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        print(json.dumps(row, ensure_ascii=False, default=str), flush=True)
        return row

    def phase(self, name, function, checkpoint=None):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            begin = perf_counter()
            self.event("phase_started", phase=name)
            try:
                result = function(*args, **kwargs)
                if checkpoint:
                    value = checkpoint(result, args)
                    with self.lock:
                        self.write(f"checkpoint-{name}.json", value)
                row = self.event("phase_completed", phase=name, seconds=round(perf_counter() - begin, 3))
                self.phases.append(row)
                return result
            except BaseException as exc:
                row = self.event("phase_error", phase=name, seconds=round(perf_counter() - begin, 3), error=str(exc))
                self.phases.append(row)
                raise
        return wrapped

    def refresh(self, record):
        with self.lock:
            identifier = self.records.get(id(record))
            if identifier is None:
                return
            call = self.calls[identifier]
            call["record"] = deepcopy(record)
            call["usage"] = record.get("usage")
            call["validation_status"] = "error" if record.get("error") else "complete"
            self.write(f"calls/{identifier:04d}.json", call)
        self.event("model_error" if record.get("error") else "model_validated", call_id=identifier,
                   ticker=call.get("ticker"), role=record.get("role"), stage=record.get("stage"),
                   usage=record.get("usage"), error=record.get("error"))

    def install(self):
        original_invoke = CodexModel.invoke
        monitor = self

        def invoke(model, prompt, schema, record):
            begin = perf_counter()
            with monitor.lock:
                identifier = len(monitor.calls) + 1
                row = {"call_id": identifier, "ticker": getattr(monitor.context, "ticker", None),
                       "role": record.get("role"), "stage": record.get("stage"),
                       "started_at": stamp(), "prompt": prompt, "output_schema": deepcopy(schema),
                       "transport_status": "running", "validation_status": "pending", "record": deepcopy(record)}
                monitor.calls[identifier] = row
                monitor.records[id(record)] = identifier
                monitor.live_records[identifier] = record
                monitor.write(f"calls/{identifier:04d}.json", row)
            monitor.event("model_started", call_id=identifier, ticker=row["ticker"], role=row["role"], stage=row["stage"], prompt_chars=len(prompt))
            try:
                output = original_invoke(model, prompt, schema, record)
                row.update(raw_output=deepcopy(output), transport_status="complete")
                return output
            except BaseException as exc:
                row.update(transport_status="error", error=str(exc))
                raise
            finally:
                row.update(seconds=round(perf_counter() - begin, 3), usage=record.get("usage"),
                           finished_at=stamp(), record=deepcopy(record))
                with monitor.lock:
                    monitor.write(f"calls/{identifier:04d}.json", row)
                monitor.event("model_completed", call_id=identifier, ticker=row["ticker"], role=row["role"],
                              stage=row["stage"], seconds=row["seconds"], usage=row.get("usage"),
                              status=row["transport_status"], error=row.get("error"))

        CodexModel.invoke = invoke
        original_run = CompactCodexModel.run

        def role_run(model, role, stage, context):
            prior = getattr(monitor.context, "ticker", None)
            monitor.context.ticker = context.get("ticker")
            count = len(model.calls)
            try:
                return original_run(model, role, stage, context)
            finally:
                if len(model.calls) > count:
                    monitor.refresh(model.calls[-1])
                monitor.context.ticker = prior

        CompactCodexModel.run = role_run
        original_stage = business_discovery.invoke_stage

        def business_stage(run, key, model, *args, **kwargs):
            try:
                return original_stage(run, key, model, *args, **kwargs)
            finally:
                for record in run.get(key, {}).get("calls", []):
                    monitor.refresh(record)

        business_discovery.invoke_stage = business_stage
        for name, checkpoint in (
            ("collect_inputs", lambda result, args: result),
            ("enrich_events", lambda result, args: args[0]),
            ("run_reports", lambda result, args: {"run_id": result[0], "holdings": result[1], "discovery": result[2], "operations": result[3]}),
            ("collect_public_context", lambda result, args: result),
            ("add_public_context", lambda result, args: args[0]),
            ("save_run", lambda result, args: {"exit_code": result, "output_dir": str(args[0])}),
        ):
            setattr(integrated, name, monitor.phase(name, getattr(integrated, name), checkpoint))
        original_prepare = integrated.prepare_business
        integrated.prepare_business = monitor.phase("prepare_business", original_prepare)
        original_run_business = integrated.run_business
        integrated.run_business = monitor.phase("business_discovery", original_run_business, lambda result, args: result)
        original_public = PublicClient.text

        def request(client, url, *, sec=False):
            begin = perf_counter()
            try:
                result = original_public(client, url, sec=sec)
            except BaseException as exc:
                monitor.event("source_request", url=url, sec=sec, status="unavailable", error=str(exc), seconds=round(perf_counter() - begin, 3))
                raise
            monitor.event("source_request", url=url, sec=sec, status="available", collected_at=result[1], seconds=round(perf_counter() - begin, 3))
            return result

        PublicClient.text = request

    def finish(self, exit_code, error=None):
        # Refresh late-added validation errors/outputs from the retained record
        # references via checkpoint files and final evidence, not guessed tokens.
        for record in self.live_records.values():
            self.refresh(record)
        measured = [row.get("record", {}) for row in self.calls.values()]
        for record, row in zip(measured, self.calls.values()):
            record.setdefault("seconds", row.get("seconds"))
            record.setdefault("prompt_chars", len(row.get("prompt", "")))
        summary = {"started_at": self.started_at, "finished_at": stamp(),
                   "wall_seconds": round(perf_counter() - self.started, 3), "exit_code": exit_code,
                   "status": "complete" if exit_code == 0 else "error", "error": error,
                   "model_operations": measurement(measured), "call_count": len(self.calls), "phase_events": self.phases}
        with self.lock:
            self.write("summary.json", summary)
        self.event("run_finished", exit_code=exit_code, status=summary["status"], seconds=summary["wall_seconds"], calls=len(self.calls))


def main():
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--monitor-dir", type=Path, required=True)
    args, forwarded = parser.parse_known_args()
    if "--holdings" not in forwarded:
        parser.error("This monitor runs the fresh normal --holdings workflow")
    monitor = Monitor(args.monitor_dir)
    monitor.install()
    monitor.event("run_started", mode="fresh_normal", command_arguments=forwarded)
    sys.argv = ["research_prototype.integrated", *forwarded]
    code, error = 1, None
    try:
        code = integrated.main() or 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        error = str(exc) if code else None
    except BaseException as exc:
        error = str(exc)
        (monitor.directory / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        monitor.finish(code, error)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
