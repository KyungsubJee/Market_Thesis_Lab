"""Validate an editorial presentation against a saved decision without altering it."""


def validate_presentation(original, presentation):
    section = original["sections"][0]
    final = section["final"]
    if not final or (
        presentation.get("source_run_id") != original["run_id"]
        or presentation.get("ticker") != section["ticker"]
    ):
        raise ValueError("Presentation must match the source run and its saved decision")

    def require_text(value, fields):
        if any(not isinstance(value.get(field), str) or not value[field].strip() for field in fields):
            raise ValueError(f"Presentation requires nonempty text in {', '.join(fields)}")

    require_text(presentation, ("applicability",))
    require_text(presentation["weighing"], ("text",))
    checks = presentation["checks"]
    if not checks or len(checks) != len(final["reassessment_conditions"]):
        raise ValueError("Presentation must include one row per original reassessment condition")
    for row in checks:
        require_text(row, ("when", "criterion", "action"))
    for row in [presentation["weighing"], *checks]:
        citations = row["evidence_ids"]
        if not citations or set(citations) - section["sources"].keys():
            raise ValueError("Presentation must cite saved evidence IDs")
    return presentation
