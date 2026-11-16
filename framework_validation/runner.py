"""Local, mock-only validation of the research framework's control flow."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Protocol


FIXTURE_DIR = Path(__file__).parent / "fixtures"
CASE_IDS = ("coca_cola", "samsung", "meta")
ANALYSIS_ROLES = ("macro_market", "sector", "fundamental", "price_trading", "event_attention")
ISSUE_KINDS = {"data_error", "uncertain_assumption", "interpretation"}


class Model(Protocol):
    def run(self, role: str, stage: str, context: dict) -> dict: ...


def load_fixture(path: Path | str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError(f"Timestamp requires a time zone: {value}")
    return parsed


class MockModel:
    """Return scripted review issues and neutral, clearly marked mock results."""

    def __init__(self, fixture: dict):
        self.reviews = iter(fixture["mock_reviews"])
        self.last_review = {"issues": []}

    def run(self, role: str, stage: str, context: dict) -> dict:
        if role == "reviewer":
            self.last_review = next(self.reviews, self.last_review)
            return self.last_review
        issue = context.get("issue")
        evidence_ids = issue["evidence_ids"] if issue else list(context["evidence"])
        if stage in {"debate_open", "debate_reply"}:
            return {"evidence_ids": evidence_ids, "issue_id": issue["id"], "stance": role, "mock": True}
        return {"evidence_ids": evidence_ids, "horizon": context["horizon"], "mock": True}


def run_case(fixture: dict, model: Model) -> dict:
    as_of = parse_timestamp(fixture["as_of"])
    evidence = {}
    excluded = []
    seen = set()
    for item in fixture["evidence"]:
        evidence_id = item["id"]
        if evidence_id in seen:
            raise ValueError(f"Duplicate evidence ID: {evidence_id}")
        seen.add(evidence_id)
        if parse_timestamp(item["available_at"]) > as_of:
            excluded.append(evidence_id)
        else:
            evidence[evidence_id] = item

    context = {
        "as_of": fixture["as_of"],
        "ticker": fixture["ticker"],
        "horizon": fixture["horizon"],
        "question": fixture["question"],
        "evidence": evidence,
        "missing_fields": fixture["missing_fields"],
    }
    trace = []
    result = {
        "case_id": fixture["case_id"],
        "path": "blocked",
        "status": "review_required",
        "trace": trace,
        "excluded_evidence": excluded,
        "missing_fields": context["missing_fields"],
        "unresolved_issues": [],
        "final": None,
    }
    missing_required = set(fixture.get("required_evidence_ids", [])) - evidence.keys()
    if not evidence or missing_required:
        result["unresolved_issues"] = sorted(missing_required) or ["no_eligible_evidence"]
        return result

    def call(role: str, stage: str, **inputs) -> dict:
        output = model.run(role, stage, {**context, **inputs})
        evidence_ids = output.get("evidence_ids", [])
        if role == "reviewer":
            evidence_ids = [ref for issue in output["issues"] for ref in issue["evidence_ids"]]
        if set(evidence_ids) - evidence.keys():
            raise ValueError(f"{role} cited evidence outside the snapshot: {evidence_ids}")
        entry = {"role": role, "stage": stage, "evidence_ids": evidence_ids}
        if role == "reviewer":
            entry["issues"] = output["issues"]
        if "issue" in inputs:
            entry["issue_id"] = inputs["issue"]["id"]
        trace.append(entry)
        return output

    def review(stage: str, analyses: dict, draft: dict, additional: list) -> list:
        output = call("reviewer", stage, analyses=analyses, draft=draft, additional=additional)
        for issue in output["issues"]:
            if issue["kind"] not in ISSUE_KINDS or not isinstance(issue["material"], bool):
                raise ValueError(f"Invalid review issue: {issue['id']}")
            if issue["kind"] == "uncertain_assumption" and (
                not issue["roles"] or set(issue["roles"]) - set(ANALYSIS_ROLES)
            ):
                raise ValueError(f"Invalid analysis roles in issue: {issue['id']}")
        return [issue for issue in output["issues"] if issue["material"]]

    analyses = {}
    analyses["macro_market"] = call("macro_market", "analysis")
    analyses["sector"] = call("sector", "analysis", macro_market=analyses["macro_market"])
    for role in ANALYSIS_ROLES[2:]:
        analyses[role] = call(
            role, "analysis", macro_market=analyses["macro_market"], sector=analyses["sector"]
        )
    draft = call("synthesis", "draft", analyses=analyses)
    additional = []
    issues = review("review", analyses, draft, additional)
    paths = []

    # A bad input stops the affected case before the system can debate it.
    if not any(issue["kind"] == "data_error" for issue in issues):
        uncertain = [issue for issue in issues if issue["kind"] == "uncertain_assumption"]
        if uncertain:
            paths.append("secondary")
            for issue in uncertain:
                for role in dict.fromkeys(issue["roles"]):
                    additional.append(call(role, "secondary", issue=issue))
            issues = review("rereview", analyses, draft, additional)

    if not any(issue["kind"] == "data_error" for issue in issues):
        disputes = [issue for issue in issues if issue["kind"] == "interpretation"]
        if disputes:
            paths.append("debate")
            for issue in disputes:
                bull = call("bull", "debate_open", issue=issue)
                bear = call("bear", "debate_open", issue=issue)
                bull_reply = call("bull", "debate_reply", issue=issue, opposing_case=bear)
                bear_reply = call("bear", "debate_reply", issue=issue, opposing_case=bull)
                additional.extend([bull, bear, bull_reply, bear_reply])
            issues = review("rereview", analyses, draft, additional)

    result["unresolved_issues"] = [issue["id"] for issue in issues]
    if any(issue["kind"] == "data_error" for issue in issues):
        return result
    result["path"] = "+".join(paths) or "basic"
    result["status"] = "review_required" if issues else "complete"
    result["final"] = call(
        "synthesis", "final", analyses=analyses, draft=draft, additional=additional,
        unresolved_issues=issues, status=result["status"],
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=("all", *CASE_IDS), nargs="?", default="all")
    args = parser.parse_args()
    case_ids = CASE_IDS if args.case == "all" else (args.case,)
    for case_id in case_ids:
        fixture = load_fixture(FIXTURE_DIR / f"{case_id}.json")
        result = run_case(fixture, MockModel(fixture))
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
