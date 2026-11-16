"""Synthesize a completed secondary stage without its subsequent debate."""

import argparse
from hashlib import sha256
import json
from pathlib import Path

from .live_runner import CodexModel
from .runner import ANALYSIS_ROLES, CASE_IDS, FIXTURE_DIR, load_fixture, parse_timestamp


def restore_context(fixture, natural):
    if natural["errors"] or natural["branches"]["escalated"]["path"] != "secondary+debate":
        raise ValueError("A successful natural secondary+debate run is required")
    keys = ("ticker", "as_of", "horizon", "question", "evidence", "missing_fields")
    snapshot = {key: fixture[key] for key in keys}
    digest = sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if digest != natural["snapshot_sha256"] or fixture["case_id"] != natural["case_id"]:
        raise ValueError("Fixture does not match the natural run's input snapshot")
    calls = natural["calls"]
    cutoff = next(index for index, call in enumerate(calls) if call["stage"] == "rereview")
    earlier = calls[:cutoff]
    analyses = {
        call["role"]: call["output"] for call in earlier if call["stage"] == "analysis"
    }
    if tuple(analyses) != ANALYSIS_ROLES:
        raise ValueError("Natural run is missing shared analysis outputs")
    draft = next(call["output"] for call in earlier if call["stage"] == "draft")
    additional = [call["output"] for call in earlier if call["stage"] == "secondary"]
    if not additional:
        raise ValueError("Natural run is missing secondary outputs")
    rereview = calls[cutoff]["output"]
    unresolved = [issue for issue in rereview["issues"] if issue["material"]]
    as_of = parse_timestamp(snapshot["as_of"])
    snapshot["evidence"] = {
        item["id"]: item for item in snapshot["evidence"]
        if parse_timestamp(item["available_at"]) <= as_of
    }
    return {
        **snapshot,
        "analyses": analyses,
        "draft": draft,
        "additional": additional,
        "unresolved_issues": unresolved,
        "status": "review_required" if unresolved else "complete",
        "initial_review": natural["initial_review"],
    }, rereview


def synthesize(fixture, natural, model):
    context, rereview = restore_context(fixture, natural)
    final = model.run("synthesis", "final", context)
    if set(final["evidence_ids"]) - context["evidence"].keys():
        raise ValueError("Secondary-only synthesis cited evidence outside the snapshot")
    baseline = natural["branches"]["baseline"]["final"]
    combined = natural["branches"]["escalated"]["final"]
    return {
        "case_id": fixture["case_id"],
        "as_of": fixture["as_of"],
        "model_requested": natural["model_requested"],
        "snapshot_sha256": natural["snapshot_sha256"],
        "mode": "secondary_only_comparison",
        "initial_review": natural["initial_review"],
        "secondary_rereview": rereview,
        "baseline": baseline,
        "secondary_only": {"status": context["status"], "final": final},
        "secondary_and_debate": combined,
        "changed_fields_from_baseline": [key for key in final if final[key] != baseline[key]],
        "changed_fields_after_debate": [key for key in final if final[key] != combined[key]],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=CASE_IDS)
    parser.add_argument("--natural", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    natural = json.loads(args.natural.read_text(encoding="utf-8"))
    fixture = load_fixture(FIXTURE_DIR / f"{args.case}.json")
    model = CodexModel(natural["model_requested"], timeout=90, max_calls=1)
    result = synthesize(fixture, natural, model)
    result["calls"] = model.calls
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
