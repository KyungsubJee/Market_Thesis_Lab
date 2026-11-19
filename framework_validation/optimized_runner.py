"""Run the compact, review-gated research framework on one fixed case."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from time import perf_counter

from .runner import ANALYSIS_ROLES, CASE_IDS, FIXTURE_DIR, load_fixture, parse_timestamp


IMPACT_ORDER = {"action": 0, "downside": 1, "reassessment": 2}
REUSABLE_ANALYSIS_ROLES = ("macro_market", "sector", "price_trading", "event_attention")
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens")
CONTEXT_KEYS = ("prompt_chars", "output_chars")
SNAPSHOT_INPUT_FIELDS = ("ticker", "as_of", "horizon", "question", "evidence", "missing_fields")


def snapshot_digest(snapshot):
    input_snapshot = {key: snapshot[key] for key in SNAPSHOT_INPUT_FIELDS}
    if "news_as_of" in snapshot:
        input_snapshot["news_as_of"] = snapshot["news_as_of"]
    return sha256(json.dumps(input_snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def evidence_cutoff(fixture, item):
    base = parse_timestamp(fixture["as_of"])
    if item.get("category") == "news_context" and fixture.get("news_as_of"):
        public = parse_timestamp(fixture["news_as_of"])
        if public < base:
            raise ValueError("News cutoff precedes the frozen market cutoff")
        if item["id"] not in fixture.get("required_evidence_ids", []):
            return public
    return base


def issue_key(issue):
    return (
        issue["kind"], issue["horizon"].strip().casefold(),
        " ".join(issue["subject_key"].casefold().split()),
    )


def update_issues(previous, review):
    """Keep material issues open until the reviewer explicitly resolves them."""
    resolved = set(review["resolved_ids"])
    current = {}
    seen_ids = {}
    for issue in [*previous, *review["issues"]]:
        if issue["id"] in resolved:
            continue
        key = issue_key(issue)
        prior_key = seen_ids.setdefault(issue["id"], key)
        if prior_key != key:
            raise ValueError(f"Issue ID refers to distinct findings: {issue['id']}")
        old = current.get(key)
        if old and old["material"] and not issue["material"]:
            continue
        if old and old["material"] and issue["material"]:
            old_impact = IMPACT_ORDER.get(old["impact"], len(IMPACT_ORDER))
            new_impact = IMPACT_ORDER.get(issue["impact"], len(IMPACT_ORDER))
            if new_impact > old_impact:
                continue
        current[key] = {**issue, "id": old["id"] if old else issue["id"]}
    return list(current.values())


def summarize_calls(calls):
    return {
        "total_calls": len(calls),
        "usage": {
            key: sum((call["usage"] or {}).get(key, 0) for call in calls) for key in USAGE_KEYS
        },
        "context_metrics": {key: sum(call.get(key, 0) for call in calls) for key in CONTEXT_KEYS},
    }


def run_optimized(fixture, model, *, replay=None):
    input_snapshot = {
        key: fixture[key]
        for key in SNAPSHOT_INPUT_FIELDS
    }
    if "news_as_of" in fixture:
        input_snapshot["news_as_of"] = fixture["news_as_of"]
    digest = snapshot_digest(input_snapshot)
    reused = {}
    if replay is not None:
        if replay.get("snapshot_sha256") != digest or replay.get("case_id") != fixture["case_id"]:
            raise ValueError("Reused role outputs must use the same case and evidence snapshot")
        if replay.get("error"):
            raise ValueError("Reused role outputs must come from a successful run")
        reused = {
            call["role"]: call for call in replay["calls"]
            if call["stage"] == "analysis" and call["role"] in REUSABLE_ANALYSIS_ROLES and "output" in call
        }
        if set(reused) != set(REUSABLE_ANALYSIS_ROLES):
            raise ValueError("Reused run must contain all four required analysis outputs")
    as_of = parse_timestamp(fixture["as_of"])
    evidence = {}
    excluded = []
    seen = set()
    for item in fixture["evidence"]:
        evidence_id = item["id"]
        if evidence_id in seen:
            raise ValueError(f"Duplicate evidence ID: {evidence_id}")
        seen.add(evidence_id)
        if parse_timestamp(item["available_at"]) <= evidence_cutoff(fixture, item):
            evidence[evidence_id] = item
        else:
            excluded.append(evidence_id)
    context = {**input_snapshot, "evidence": evidence}
    result = {
        "case_id": fixture["case_id"],
        "as_of": fixture["as_of"],
        "snapshot_sha256": digest,
        "snapshot_evidence_ids": list(evidence),
        "excluded_evidence": excluded,
        "missing_fields": fixture["missing_fields"],
        "path": "blocked",
        "status": "review_required",
        "trace": [],
        "initial_review": None,
        "gate_decisions": [],
        "unresolved_issues": [],
        "final": None,
        "error": None,
    }
    if reused:
        result["reused_analysis"] = {
            "roles": list(reused),
            **summarize_calls(list(reused.values())),
        }
    required_missing = set(fixture.get("required_evidence_ids", [])) - evidence.keys()
    if required_missing or not evidence:
        result["gate_decisions"].append({
            "decision": "blocked_missing_evidence",
            "evidence_ids": sorted(required_missing),
        })
        return result

    analyses = {}
    additional = []
    issues = []
    paths = []
    draft = None

    def call(role, stage, **inputs):
        entry = {"role": role, "stage": stage}
        if "issue" in inputs:
            entry["issue_id"] = inputs["issue"]["id"]
        result["trace"].append(entry)
        if stage == "analysis" and role in reused:
            entry["reused"] = True
            output = reused[role]["output"]
        else:
            output = model.run(role, stage, {**context, **inputs})
        if role == "reviewer":
            cited = [ref for issue in output["issues"] for ref in issue["evidence_ids"]]
        else:
            cited = output["evidence_ids"]
        if set(cited) - evidence.keys():
            raise ValueError(f"{role} cited evidence outside the snapshot")
        entry["evidence_ids"] = sorted(set(cited))
        entry["output"] = output
        return output

    def material_issues():
        return [issue for issue in issues if issue["material"]]

    def review(stage):
        nonlocal issues
        findings = call(
            "reviewer", stage, analyses=analyses, draft=draft,
            additional=additional, previous_issues=issues,
        )
        if stage == "review":
            result["initial_review"] = findings
        issues = update_issues(issues, findings)
        result["unresolved_issues"] = material_issues()

    def blocked_by_data_error():
        blocking = [issue["id"] for issue in material_issues() if issue["kind"] == "data_error"]
        if blocking:
            result["gate_decisions"].append({"decision": "blocked_data_error", "issue_ids": blocking})
        return bool(blocking)

    def eligible(issue):
        return (
            issue["horizon"].strip().casefold() == fixture["horizon"].strip().casefold()
            and issue["impact"] in IMPACT_ORDER
            and bool(issue["subject_key"].strip())
        )

    def reconsider():
        nonlocal draft
        draft = call(
            "synthesis", "reconcile", analyses=analyses, draft=draft,
            additional=additional, unresolved_issues=material_issues(),
            status="review_required",
        )
        review("rereview")

    try:
        analyses["macro_market"] = call("macro_market", "analysis")
        analyses["sector"] = call("sector", "analysis", macro_market=analyses["macro_market"])
        for role in ANALYSIS_ROLES[2:]:
            analyses[role] = call(
                role, "analysis", macro_market=analyses["macro_market"], sector=analyses["sector"],
            )
        draft = call("synthesis", "draft", analyses=analyses)
        review("review")
        if blocked_by_data_error():
            return result
        if any(eligible(issue) and issue["handling"] == "reconcile" for issue in material_issues()):
            reconsider()
            if blocked_by_data_error():
                return result

        for kind, path in (("uncertain_assumption", "secondary"), ("interpretation", "debate")):
            candidates = sorted(
                (
                    issue for issue in material_issues()
                    if issue["kind"] == kind and issue["handling"] == "specialist" and eligible(issue)
                ),
                key=lambda issue: IMPACT_ORDER[issue["impact"]],
            )
            if not candidates:
                continue
            issue = candidates[0]
            result["gate_decisions"].append({
                "decision": path,
                "issue_id": issue["id"],
                "deferred_issue_ids": [candidate["id"] for candidate in candidates[1:]],
            })
            paths.append(path)
            if path == "secondary":
                role = next((role for role in issue["roles"] if role in ANALYSIS_ROLES), None)
                if role is None:
                    raise ValueError(f"No valid specialist for {issue['id']}")
                additional.append({"role": role, "issue_id": issue["id"], **call(role, "secondary", issue=issue)})
            else:
                for role in ("bull", "bear"):
                    additional.append({
                        "role": role, "issue_id": issue["id"], **call(role, "debate_open", issue=issue),
                    })
            reconsider()
            if blocked_by_data_error():
                return result
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result.update(path="+".join(paths) or "error", status="execution_error", error=str(exc))
        return result

    for issue in material_issues():
        if not eligible(issue):
            result["gate_decisions"].append({"decision": "unroutable_issue", "issue_id": issue["id"]})
    result.update(
        path="+".join(paths) or "basic",
        status="review_required" if material_issues() or draft["action"] == "review_required" else "complete",
        final=draft,
    )
    return result


def main():
    from .compact_model import CompactCodexModel

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=CASE_IDS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--reuse-analysis", type=Path,
        help="Reuse the four stable analysis roles from a prior result for the same snapshot and model",
    )
    args = parser.parse_args()
    if args.output.exists() or args.timeout < 1:
        parser.error("output must be new and timeout must be positive")
    fixture = load_fixture(FIXTURE_DIR / f"{args.case}.json")
    replay = None
    if args.reuse_analysis:
        replay = json.loads(args.reuse_analysis.read_text(encoding="utf-8"))
        if replay.get("model_requested") != args.model:
            parser.error("reused analysis must use the same model")
    model = CompactCodexModel(args.model, args.timeout, 20)
    start = perf_counter()
    result = run_optimized(fixture, model, replay=replay)
    result.update(
        **summarize_calls(model.calls),
        model_requested=args.model,
        debate_format="one independent bull brief and one bear brief; unresolved findings remain open",
        elapsed_seconds=round(perf_counter() - start, 3),
        calls=model.calls,
    )
    if replay is not None:
        reused_calls = [
            call for call in replay["calls"]
            if call["stage"] == "analysis" and call["role"] in REUSABLE_ANALYSIS_ROLES
        ]
        result["effective_run"] = summarize_calls([*reused_calls, *model.calls])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({
        "case": args.case,
        "path": result["path"],
        "status": result["status"],
        "calls": result["total_calls"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False))
    return int(result["error"] is not None)


if __name__ == "__main__":
    raise SystemExit(main())
