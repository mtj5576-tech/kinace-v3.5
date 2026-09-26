"""Coverage and leakage audit for a materialized point-in-time universe."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import PROJECT_ROOT, database_path, validate_database_path
from .feature_snapshot import canonical_json_bytes, load_feature_snapshot_artifact, sha256_bytes
from .history_features import load_compact_history_pit_context
from .pit_universe import select_pit_universe_from_context
from .portfolio_backtest import discover_snapshots


SCHEMA_VERSION = 1
DEFAULT_PIT_AUDIT_REPORT_DIR = PROJECT_ROOT / "reports" / "pit"


class PitAuditError(ValueError):
    """Raised when a PIT universe audit cannot be produced."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json(payload: Mapping[str, Any], path: str | Path) -> Path:
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(canonical_json_bytes(payload))
    temporary.replace(target)
    return target


def _write_text(text: str, path: str | Path) -> Path:
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(target)
    return target


def _metadata_coverage(context: Mapping[str, Any]) -> dict[str, Any]:
    metadata = context["metadata"]
    list_dates = sum(bool(value.get("list_date")) for value in metadata.values())
    delisted = sum(bool(value.get("delist_date")) for value in metadata.values())
    industries = sum(bool(value.get("industry")) for value in metadata.values())
    return {
        "metadata_count": len(metadata),
        "list_date_present": list_dates,
        "delist_date_present": delisted,
        "current_industry_present": industries,
        "list_date_ratio": round(list_dates / len(metadata), 8) if metadata else 0.0,
        "delist_date_ratio": round(delisted / len(metadata), 8) if metadata else 0.0,
    }


def run_pit_universe_audit(
    database: str | Path | None,
    *,
    snapshot_dir: str | Path,
    policy: Mapping[str, Any],
    start_date: str | None = None,
    end_date: str | None = None,
    output_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
) -> dict[str, Any]:
    target = database_path() if database is None else validate_database_path(database)
    snapshots = discover_snapshots(
        snapshot_dir,
        start_date=start_date,
        end_date=end_date,
    )
    context = load_compact_history_pit_context(
        target,
        decision_dates=[str(item["decision_date"]) for item in snapshots],
        end_date=end_date,
    )
    decisions: list[dict[str, Any]] = []
    exclusion_counts: Counter[str] = Counter()
    total_selected = 0
    total_snapshot_rows = 0
    replay_ready_rows = 0
    unexpected_bindings = 0
    missing_bindings = 0
    industry_covered = 0
    conflicts = 0
    unsafe_industry_dates = 0
    for item in snapshots:
        decision_date = str(item["decision_date"])
        selection = select_pit_universe_from_context(
            context,
            decision_date=decision_date,
            policy=policy,
        )
        payload = load_feature_snapshot_artifact(item["path"])
        rows = [row for row in payload.get("rows") or [] if isinstance(row, Mapping)]
        snapshot_codes = {str(row.get("code") or "") for row in rows}
        eligible = set(selection["eligible_codes"])
        unexpected = sorted(snapshot_codes - eligible)
        missing = sorted(eligible - snapshot_codes)
        unexpected_bindings += len(unexpected)
        missing_bindings += len(missing)
        industry_map = selection["industry_by_code"]
        covered = sum(1 for code in snapshot_codes if industry_map.get(code))
        industry_covered += covered
        total_selected += len(eligible)
        total_snapshot_rows += len(rows)
        replay_ready_rows += sum(bool(row.get("replay_ready")) for row in rows)
        conflicts += len(selection.get("industry_conflicts") or [])
        unsafe_industry_dates += int(not selection["industry_lookahead_safe"])
        for reasons in selection["excluded"].values():
            exclusion_counts.update(reasons)
        decisions.append(
            {
                "decision_date": decision_date,
                "candidate_count": selection["candidate_count"],
                "eligible_count": selection["eligible_count"],
                "excluded_count": selection["excluded_count"],
                "snapshot_row_count": len(rows),
                "replay_ready_rows": sum(bool(row.get("replay_ready")) for row in rows),
                "industry_covered_rows": covered,
                "unexpected_snapshot_codes": unexpected,
                "missing_snapshot_codes": missing,
                "industry_conflicts": selection.get("industry_conflicts") or [],
                "industry_snapshot_dates": selection.get("industry_snapshot_dates") or {},
            }
        )
    eligible_counts = [int(item["eligible_count"]) for item in decisions]
    industry_ratio = (
        industry_covered / total_snapshot_rows if total_snapshot_rows else 0.0
    )
    policy_industry_safe = str(policy.get("industry_mode") or "") != "current_proxy"
    checks = {
        "decision_dates_materialized": len(decisions) == len(snapshots),
        "eligible_universe_nonempty": bool(eligible_counts)
        and min(eligible_counts) > 0,
        "snapshot_bindings_complete": unexpected_bindings == 0
        and missing_bindings == 0,
        "pit_industry_coverage_complete": industry_ratio == 1.0,
        "industry_lookahead_safe": policy_industry_safe
        and unsafe_industry_dates == 0,
        "no_industry_conflicts": conflicts == 0,
    }
    status = "passed" if all(checks.values()) else "review_required"
    identity = {
        "database": str(target),
        "snapshot_dir": str(Path(snapshot_dir).resolve(strict=False)),
        "policy": dict(policy),
        "start_date": start_date,
        "end_date": end_date,
    }
    audit_id = sha256_bytes(canonical_json_bytes(identity, trailing_newline=False))
    target_output = (
        Path(output_path).resolve(strict=False)
        if output_path
        else Path(snapshot_dir).resolve(strict=False) / "pit-audit.json"
    )
    markdown_target = (
        Path(markdown_path).resolve(strict=False)
        if markdown_path
        else target_output.with_suffix(".md")
    )
    payload = {
        "artifact_type": "kinace_v322_pit_universe_audit",
        "schema_version": SCHEMA_VERSION,
        "pit_audit_id": audit_id,
        "status": status,
        "database": str(target),
        "snapshot_dir": str(Path(snapshot_dir).resolve(strict=False)),
        "policy": dict(policy),
        "date_range": {"start": start_date, "end": end_date},
        "decision_dates": len(decisions),
        "snapshot_rows": total_snapshot_rows,
        "replay_ready_rows": replay_ready_rows,
        "eligible_count_min": min(eligible_counts) if eligible_counts else 0,
        "eligible_count_max": max(eligible_counts) if eligible_counts else 0,
        "eligible_count_mean": round(sum(eligible_counts) / len(eligible_counts), 6)
        if eligible_counts
        else 0.0,
        "industry_coverage_ratio": round(industry_ratio, 8),
        "unexpected_binding_count": unexpected_bindings,
        "missing_binding_count": missing_bindings,
        "industry_conflict_count": conflicts,
        "metadata_coverage": _metadata_coverage(context),
        "exclusion_reasons": dict(exclusion_counts.most_common()),
        "checks": checks,
        "decisions": decisions,
        "production_ready": False,
        "production_changed": False,
        "generated_at_utc": _utc_now(),
    }
    lines = [
        "# Kinace V3.3 Full PIT Universe Audit",
        "",
        f"- Status: `{status}`",
        f"- Decision dates: `{len(decisions)}`",
        f"- Snapshot rows: `{total_snapshot_rows}`",
        f"- Eligible rows per date: min `{payload['eligible_count_min']}`, "
        f"max `{payload['eligible_count_max']}`, mean `{payload['eligible_count_mean']}`",
        f"- Industry coverage: `{industry_ratio:.4%}`",
        f"- Unexpected bindings: `{unexpected_bindings}`",
        f"- Missing bindings: `{missing_bindings}`",
        f"- Industry conflicts: `{conflicts}`",
        "",
        "## Checks",
        "",
        "| Check | Passed |",
        "|---|---:|",
    ]
    lines.extend(f"| `{key}` | {value} |" for key, value in checks.items())
    lines.extend(
        [
            "",
            "## Exclusion Reasons",
            "",
            "| Reason | Count |",
            "|---|---:|",
        ]
    )
    lines.extend(
        f"| `{reason}` | {count} |"
        for reason, count in exclusion_counts.most_common(30)
    )
    lines.append("")
    _write_text("\n".join(lines), markdown_target)
    written = _write_json(payload, target_output)
    payload["artifact_path"] = str(written)
    payload["markdown_path"] = str(markdown_target)
    return payload


__all__ = [
    "DEFAULT_PIT_AUDIT_REPORT_DIR",
    "PitAuditError",
    "run_pit_universe_audit",
]

