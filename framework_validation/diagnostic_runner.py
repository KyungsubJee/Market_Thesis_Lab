"""Run a labeled escalation diagnostic after a completed natural comparison."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess

from .live_runner import CodexModel, ISSUE, validate_output
from .runner import ANALYSIS_ROLES, CASE_IDS, FIXTURE_DIR, load_fixture, run_case


class DiagnosticModel:
    def __init__(self, model, natural, issue):
        self.model = model
        self.initial_review = natural["initial_review"]
        self.issue = issue
        self.common = {
            (call["role"], call["stage"]): call["output"]
            for call in natural["calls"]
            if call["stage"] in {"analysis", "draft"} and "output" in call
        }

    def run(self, role, stage, context):
        if stage in {"analysis", "draft"}:
            return self.common[role, stage]
        if stage == "review":
            return {"issues": [self.issue]}
        context = {**context, "initial_review": self.initial_review, "diagnostic_issue": self.issue}
        return self.model.run(role, stage, context)


def run_diagnostic(fixture, model, natural, issue):
    validate_output(issue, ISSUE)
    if issue["kind"] not in {"uncertain_assumption", "interpretation"} or not issue["material"]:
        raise ValueError("The diagnostic issue must be a material assumption or interpretation")
    if issue["kind"] == "uncertain_assumption" and not issue["roles"]:
        raise ValueError("An assumption diagnostic requires at least one analysis role")
    snapshot = sha256(json.dumps(
        {key: fixture[key] for key in ("ticker", "as_of", "horizon", "question", "evidence", "missing_fields")},
        sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    if natural.get("snapshot_sha256") != snapshot or natural.get("case_id") != fixture["case_id"]:
        raise ValueError("The natural report used a different fixture snapshot")
    baseline = natural.get("branches", {}).get("baseline", {})
    if natural.get("errors") or natural.get("initial_review") is None or baseline.get("final") is None:
        raise ValueError("A finished natural comparison with a baseline final is required")

    result = None
    error = None
    try:
        result = run_case(fixture, DiagnosticModel(model, natural, issue))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        error = str(exc)
    final = result.get("final") if result else None
    return {
        "case_id": fixture["case_id"],
        "as_of": fixture["as_of"],
        "snapshot_sha256": snapshot,
        "model_requested": natural.get("model_requested"),
        "mode": "forced_diagnostic",
        "natural_initial_review": natural["initial_review"],
        "externally_supplied_issue": issue,
        "baseline": baseline,
        "diagnostic": result,
        "changed_fields": [
            key for key, value in baseline["final"].items() if value != final.get(key)
        ] if final else [],
        "error": error,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=CASE_IDS)
    parser.add_argument("--natural", type=Path, required=True, help="Finished natural comparison JSON")
    parser.add_argument("--kind", choices=("uncertain_assumption", "interpretation"), required=True)
    parser.add_argument("--claim", required=True, help="Neutral, externally specified concern to test")
    parser.add_argument("--role", choices=ANALYSIS_ROLES, action="append", default=[])
    parser.add_argument("--evidence-id", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--max-calls", type=int, default=12)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.timeout < 1 or args.max_calls < 1:
        parser.error("timeout and max-calls must be positive")
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    fixture = load_fixture(FIXTURE_DIR / f"{args.case}.json")
    natural = json.loads(args.natural.read_text(encoding="utf-8"))
    if not natural.get("model_requested"):
        parser.error("natural comparison must have been run with an explicit model")
    issue = {
        "id": "externally_supplied_diagnostic",
        "kind": args.kind,
        "material": True,
        "claim": args.claim,
        "roles": args.role,
        "evidence_ids": args.evidence_id,
    }
    model = CodexModel(natural["model_requested"], args.timeout, args.max_calls)
    result = run_diagnostic(fixture, model, natural, issue)
    result["calls"] = model.calls
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({
        "mode": result["mode"],
        "case": args.case,
        "path": (result["diagnostic"] or {}).get("path"),
        "calls": len(model.calls),
        "error": result["error"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False))
    return int(result["error"] is not None)


if __name__ == "__main__":
    raise SystemExit(main())
