"""Point-in-time universe selection for the minimal Kinace pilot."""

from __future__ import annotations

import hashlib
import json
import statistics
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PROJECT_ROOT
from .data_collection.models import (
    MEMBERSHIP_KIND_INTERVAL,
    MEMBERSHIP_KIND_SNAPSHOT,
    MEMBERSHIP_KINDS,
    RevisionStatus,
)
from .data_collection.store import RevisionStore
from .price_basis import RAW_PRICE_BASIS, PriceBasisError, normalize_price_basis


DEFAULT_PIT_POLICY: dict[str, Any] = {
    "index_codes": [],
    "require_index_membership": True,
    "min_history_sessions": 65,
    "min_close": 1.0,
    "min_20d_amount": 10_000_000.0,
    "require_listing_date": True,
    "require_security_state": True,
    "require_decision_bar": True,
    "require_decision_tradable": True,
    "industry_mode": "pit_membership",
    "industry_by_code": {},
}
DEFAULT_PIT_POLICY_PATH = PROJECT_ROOT / "config" / "pit-universe.v32.json"
HISTORICAL_SNAPSHOT_MODE = "historical"
LIVE_CURRENT_SNAPSHOT_MODE = "live_current"
REAL_STATUSES = {RevisionStatus.OBSERVED.value, RevisionStatus.BACKFILLED_RELIABLE.value}


class PitUniverseError(ValueError):
    """Raised when a PIT universe policy cannot be evaluated safely."""


def _bare(code: Any) -> str:
    text = str(code or "").strip().upper()
    return text.split(".", 1)[0] if "." in text else text


def _record(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("record")
    return value if isinstance(value, Mapping) else row


def normalize_pit_policy(policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = dict(policy or {})
    unknown = sorted(set(source) - set(DEFAULT_PIT_POLICY))
    if unknown:
        raise PitUniverseError("unknown PIT policy keys: " + ", ".join(unknown))
    result = dict(DEFAULT_PIT_POLICY)
    result.update(source)
    result["index_codes"] = sorted(
        {str(value).strip().upper() for value in result["index_codes"] if str(value).strip()}
    )
    result["min_history_sessions"] = int(result["min_history_sessions"])
    result["min_close"] = float(result["min_close"])
    result["min_20d_amount"] = float(result["min_20d_amount"])
    if result["min_history_sessions"] < 1:
        raise PitUniverseError("min_history_sessions must be positive")
    if result["min_close"] < 0 or result["min_20d_amount"] < 0:
        raise PitUniverseError("PIT price and amount thresholds cannot be negative")
    if not isinstance(result["require_index_membership"], bool):
        raise PitUniverseError("require_index_membership must be a boolean")
    if not isinstance(result["require_decision_bar"], bool):
        raise PitUniverseError("require_decision_bar must be a boolean")
    if result["industry_mode"] not in {
        "zero",
        "pit_map",
        "pit_membership",
        "current_proxy",
    }:
        raise PitUniverseError(
            "industry_mode must be zero, pit_map, pit_membership, or current_proxy"
        )
    if result["industry_mode"] == "pit_map":
        industry_map = result.get("industry_by_code")
        if not isinstance(industry_map, Mapping) or not industry_map:
            raise PitUniverseError("pit_map industry mode requires industry_by_code")
        result["industry_by_code"] = {
            _bare(code): str(value).strip()
            for code, value in industry_map.items()
            if _bare(code) and str(value).strip()
        }
    else:
        result["industry_by_code"] = {}
    return result


def _policy_hash(policy: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        policy,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _industry_source(industry_mode: str) -> str:
    if industry_mode == "current_proxy":
        return "current_metadata"
    if industry_mode in {"pit_membership", "pit_map"}:
        return industry_mode
    return "unavailable_zero"


def resolve_pit_policy(
    policy: Mapping[str, Any] | None = None,
    *,
    policy_path: str | Path | None = None,
    mode: str = HISTORICAL_SNAPSHOT_MODE,
) -> dict[str, Any]:
    """Resolve one explicit PIT policy for historical or live-current snapshots."""
    if mode not in {HISTORICAL_SNAPSHOT_MODE, LIVE_CURRENT_SNAPSHOT_MODE}:
        raise PitUniverseError(
            "snapshot mode must be historical or live_current"
        )

    resolved_path: Path | None = None
    if mode == LIVE_CURRENT_SNAPSHOT_MODE:
        source = dict(policy or {})
        requested_mode = str(source.get("industry_mode") or "current_proxy")
        if requested_mode != "current_proxy":
            raise PitUniverseError(
                "live_current snapshots must use industry_mode=current_proxy"
            )
        source["industry_mode"] = "current_proxy"
        normalized = normalize_pit_policy(source)
        industry_lookahead_safe = False
        pit_ready = False
    else:
        if policy is not None:
            normalized = normalize_pit_policy(policy)
        else:
            resolved_path = Path(
                policy_path or DEFAULT_PIT_POLICY_PATH
            ).resolve(strict=False)
            if not resolved_path.is_file():
                raise PitUniverseError(
                    "historical snapshot construction requires an explicit PIT policy "
                    f"or {DEFAULT_PIT_POLICY_PATH}"
                )
            try:
                loaded = json.loads(resolved_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PitUniverseError(
                    f"cannot load historical PIT policy: {resolved_path}: {exc}"
                ) from exc
            if not isinstance(loaded, Mapping):
                raise PitUniverseError("historical PIT policy root must be an object")
            normalized = normalize_pit_policy(loaded)
        if normalized["industry_mode"] == "current_proxy":
            raise PitUniverseError(
                "historical snapshots cannot use industry_mode=current_proxy"
            )
        industry_lookahead_safe = True
        pit_ready = True

    return {
        "mode": mode,
        "policy": normalized,
        "policy_path": str(resolved_path) if resolved_path is not None else None,
        "pit_policy_hash": _policy_hash(normalized),
        "industry_source": _industry_source(normalized["industry_mode"]),
        "industry_lookahead_safe": industry_lookahead_safe,
        "pit_ready": pit_ready,
    }


def _active_index_members(
    memberships: Sequence[Mapping[str, Any]],
    decision_date: str,
    index_codes: Sequence[str],
) -> tuple[set[str], dict[str, str]]:
    by_index: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in memberships:
        index_code = str(row.get("index_code") or "").strip().upper()
        if index_code:
            by_index[index_code].append(row)
    active_codes: set[str] = set()
    selected_dates: dict[str, str] = {}
    for index_code in index_codes:
        index_rows = by_index.get(index_code, [])
        if not index_rows:
            selected_dates[index_code] = ""
            continue

        kinds = set()
        for row in index_rows:
            kind = str(row.get("membership_kind") or "").strip().lower()
            if not kind:
                raise PitUniverseError(
                    f"index membership for {index_code} is missing membership_kind"
                )
            if kind not in MEMBERSHIP_KINDS:
                raise PitUniverseError(
                    f"index membership for {index_code} has invalid membership_kind: {kind}"
                )
            kinds.add(kind)
        if len(kinds) != 1:
            raise PitUniverseError(
                f"index membership for {index_code} has conflicting membership_kind values: "
                + ", ".join(sorted(kinds))
            )
        membership_kind = next(iter(kinds))

        if membership_kind == MEMBERSHIP_KIND_INTERVAL:
            active_rows = [
                row
                for row in index_rows
                if str(row.get("in_date") or "") <= decision_date
                and (
                    not row.get("out_date")
                    or decision_date < str(row.get("out_date") or "")
                )
            ]
            if not active_rows:
                selected_dates[index_code] = ""
                continue
            selected_dates[index_code] = max(
                str(row.get("in_date") or "") for row in active_rows
            )
            for row in active_rows:
                code = _bare(row.get("con_code") or row.get("ts_code"))
                if code:
                    active_codes.add(code)
            continue

        rows = [
            row
            for row in index_rows
            if str(row.get("in_date") or "") <= decision_date
        ]
        if not rows:
            selected_dates[index_code] = ""
            continue
        latest_date = max(str(row.get("in_date") or "") for row in rows)
        selected_dates[index_code] = latest_date
        for row in rows:
            if str(row.get("in_date") or "") != latest_date:
                continue
            if row.get("out_date") and str(row["out_date"]) <= decision_date:
                continue
            code = _bare(row.get("con_code") or row.get("ts_code"))
            if code:
                active_codes.add(code)
    return active_codes, selected_dates


def _active_industry_members(
    memberships: Sequence[Mapping[str, Any]],
    decision_date: str,
) -> tuple[dict[str, str], dict[str, str], list[dict[str, Any]]]:
    by_code: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in memberships:
        industry_name = str(row.get("industry_name") or "").strip()
        if not industry_name:
            continue
        code = _bare(row.get("con_code") or row.get("ts_code"))
        in_date = str(row.get("in_date") or "")
        out_date = str(row.get("out_date") or "")
        if not code or not in_date or in_date > decision_date:
            continue
        if out_date and out_date <= decision_date:
            continue
        by_code[code].append(row)

    industry_by_code: dict[str, str] = {}
    snapshot_dates: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    for code, rows in by_code.items():
        kinds = set()
        for row in rows:
            kind = str(row.get("membership_kind") or "").strip().lower()
            if not kind:
                raise PitUniverseError(
                    f"industry membership for {code} is missing membership_kind"
                )
            if kind not in MEMBERSHIP_KINDS:
                raise PitUniverseError(
                    f"industry membership for {code} has invalid membership_kind: {kind}"
                )
            kinds.add(kind)
        if len(kinds) != 1:
            raise PitUniverseError(
                f"industry membership for {code} has conflicting membership_kind values: "
                + ", ".join(sorted(kinds))
            )
        membership_kind = next(iter(kinds))

        if membership_kind == MEMBERSHIP_KIND_INTERVAL:
            selected_rows = [
                row
                for row in rows
                if str(row.get("in_date") or "") <= decision_date
                and (
                    not row.get("out_date")
                    or decision_date < str(row.get("out_date") or "")
                )
            ]
        else:
            available_rows = [
                row
                for row in rows
                if str(row.get("in_date") or "") <= decision_date
            ]
            if not available_rows:
                continue
            latest_date = max(
                str(row.get("in_date") or "") for row in available_rows
            )
            selected_rows = [
                row
                for row in available_rows
                if str(row.get("in_date") or "") == latest_date
            ]

        if not selected_rows:
            continue
        latest_date = max(str(row.get("in_date") or "") for row in selected_rows)
        latest_rows = [
            row
            for row in selected_rows
            if str(row.get("in_date") or "") == latest_date
        ]
        industries = sorted(
            {
                str(row.get("industry_name") or "").strip()
                for row in latest_rows
                if str(row.get("industry_name") or "").strip()
            }
        )
        if not industries:
            continue
        if len(industries) > 1:
            conflicts.append(
                {
                    "code": code,
                    "decision_date": decision_date,
                    "industries": industries,
                    "selected": industries[0],
                }
            )
        industry_by_code[code] = industries[0]
        snapshot_dates[code] = latest_date
    return industry_by_code, snapshot_dates, conflicts


def build_pit_context(
    store: RevisionStore | str,
    *,
    price_basis: str = RAW_PRICE_BASIS,
) -> dict[str, Any]:
    try:
        resolved_basis = normalize_price_basis(price_basis)
    except PriceBasisError as exc:
        raise PitUniverseError(str(exc)) from exc
    if resolved_basis != RAW_PRICE_BASIS:
        raise PitUniverseError("PIT filtering requires raw price bars")
    resolved_store = store if isinstance(store, RevisionStore) else RevisionStore(store)
    rows = resolved_store.current_records()
    metadata: dict[str, Mapping[str, Any]] = {}
    daily: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    states: dict[tuple[str, str], Mapping[str, Any]] = {}
    memberships: list[Mapping[str, Any]] = []
    for row in rows:
        if str(row.get("effective_status") or row.get("status")) not in REAL_STATUSES:
            continue
        dataset = str(row.get("dataset") or "")
        record = _record(row)
        if dataset == "stock_metadata":
            metadata[_bare(record.get("ts_code"))] = record
        elif dataset == "daily_bars":
            code = _bare(record.get("ts_code"))
            if code:
                daily[code].append(record)
        elif dataset == "security_state":
            states[(_bare(record.get("ts_code")), str(record.get("trade_date") or ""))] = record
        elif dataset == "sector_membership":
            memberships.append(record)

    for history in daily.values():
        history.sort(key=lambda item: str(item.get("trade_date") or ""))
    return {
        "price_basis": resolved_basis,
        "metadata": metadata,
        "daily": daily,
        "states": states,
        "memberships": memberships,
    }


def select_pit_universe_from_context(
    context: Mapping[str, Any],
    *,
    decision_date: str,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        resolved_price_basis = normalize_price_basis(
            context.get("price_basis", RAW_PRICE_BASIS),
            field="pit_context.price_basis",
        )
    except PriceBasisError as exc:
        raise PitUniverseError(str(exc)) from exc
    if resolved_price_basis != RAW_PRICE_BASIS:
        raise PitUniverseError("PIT filtering requires raw price bars")
    settings = normalize_pit_policy(policy)
    metadata = context["metadata"]
    all_daily = context.get("daily", {})
    daily_summary = context.get("daily_summary")
    states = context["states"]
    memberships = context["memberships"]

    active_index_codes, index_snapshot_dates = (
        _active_index_members(
            memberships,
            decision_date,
            settings["index_codes"],
        )
        if settings["index_codes"]
        else (set(metadata), {})
    )
    active_industries: dict[str, str] = {}
    industry_snapshot_dates: dict[str, str] = {}
    industry_conflicts: list[dict[str, Any]] = []
    if settings["industry_mode"] == "pit_membership":
        active_industries, industry_snapshot_dates, industry_conflicts = (
            _active_industry_members(memberships, decision_date)
        )
    candidates = set(metadata)

    eligible: list[str] = []
    excluded: dict[str, list[str]] = {}
    industry_by_code: dict[str, str] = {}
    for code in sorted(candidates):
        reasons: list[str] = []
        info = metadata.get(code, {})
        if (
            settings["index_codes"]
            and settings["require_index_membership"]
            and code not in active_index_codes
        ):
            reasons.append("not_index_member")
        list_date = str(info.get("list_date") or "")
        delist_date = str(info.get("delist_date") or "")
        if settings["require_listing_date"] and not list_date:
            reasons.append("missing_list_date")
        if list_date and list_date > decision_date:
            reasons.append("not_listed")
        if delist_date and delist_date <= decision_date:
            reasons.append("delisted")

        if daily_summary is not None:
            series = daily_summary.get(code, [])
            position = bisect_right(series, decision_date, key=lambda item: item[0]) - 1
            if position < 0:
                history_count = 0
                latest_trade_date = ""
                latest_close = None
                liquidity = 0.0
            else:
                latest_trade_date, history_count, latest_close, liquidity = series[position]
            if settings["require_decision_bar"] and latest_trade_date != decision_date:
                reasons.append("missing_decision_bar")
            if history_count < settings["min_history_sessions"]:
                reasons.append("insufficient_history")
            if history_count:
                if latest_close is not None and latest_close < settings["min_close"]:
                    reasons.append("below_min_close")
                if liquidity < settings["min_20d_amount"]:
                    reasons.append("below_min_20d_amount")
        else:
            history = [
                item
                for item in all_daily.get(code, [])
                if str(item.get("trade_date") or "") <= decision_date
            ]
            if len(history) < settings["min_history_sessions"]:
                reasons.append("insufficient_history")
            if history:
                if (
                    settings["require_decision_bar"]
                    and str(history[-1].get("trade_date") or "") != decision_date
                ):
                    reasons.append("missing_decision_bar")
                latest_close = float(history[-1].get("close") or 0.0)
                if latest_close < settings["min_close"]:
                    reasons.append("below_min_close")
                amounts = [
                    (
                        float(item.get("amount"))
                        if item.get("amount") is not None
                        and float(item.get("amount") or 0.0) > 0
                        else float(item.get("vol") or 0.0)
                        * statistics.fmean(
                            (
                                float(item.get("open") or 0.0),
                                float(item.get("high") or 0.0),
                                float(item.get("low") or 0.0),
                                float(item.get("close") or 0.0),
                            )
                        )
                    )
                    for item in history[-20:]
                ]
                liquidity = statistics.fmean(amounts) if amounts else 0.0
                if liquidity < settings["min_20d_amount"]:
                    reasons.append("below_min_20d_amount")

        state = states.get((code, decision_date))
        if settings["require_security_state"] and state is None:
            reasons.append("missing_security_state")
        if settings["require_decision_tradable"] and state is not None:
            if state.get("is_suspended"):
                reasons.append("suspended")
            if state.get("is_limit_up"):
                reasons.append("limit_up")

        industry = None
        if settings["industry_mode"] == "pit_map":
            industry = settings["industry_by_code"].get(code)
            if not industry:
                reasons.append("missing_pit_industry")
        elif settings["industry_mode"] == "pit_membership":
            industry = active_industries.get(code)
            if not industry:
                reasons.append("missing_pit_industry")
        elif settings["industry_mode"] == "current_proxy":
            industry = str(info.get("industry") or "").strip() or None
            if not industry:
                reasons.append("missing_current_industry_proxy")
        if industry:
            industry_by_code[code] = industry

        if reasons:
            excluded[code] = reasons
        else:
            eligible.append(code)

    return {
        "artifact_type": "kinace_v3_pit_universe",
        "schema_version": 1,
        "price_basis": resolved_price_basis,
        "decision_date": decision_date,
        "policy": settings,
        "index_snapshot_dates": index_snapshot_dates,
        "industry_snapshot_dates": {
            code: industry_snapshot_dates.get(code, "") for code in eligible
        },
        "industry_conflicts": industry_conflicts,
        "candidate_count": len(candidates),
        "eligible_count": len(eligible),
        "excluded_count": len(excluded),
        "eligible_codes": eligible,
        "industry_by_code": industry_by_code,
        "excluded": excluded,
        "industry_lookahead_safe": settings["industry_mode"] != "current_proxy",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def select_pit_universe(
    store: RevisionStore | str,
    *,
    decision_date: str,
    policy: Mapping[str, Any] | None = None,
    price_basis: str = RAW_PRICE_BASIS,
) -> dict[str, Any]:
    context = build_pit_context(store, price_basis=price_basis)
    return select_pit_universe_from_context(
        context,
        decision_date=decision_date,
        policy=policy,
    )


__all__ = [
    "DEFAULT_PIT_POLICY",
    "DEFAULT_PIT_POLICY_PATH",
    "HISTORICAL_SNAPSHOT_MODE",
    "LIVE_CURRENT_SNAPSHOT_MODE",
    "PitUniverseError",
    "build_pit_context",
    "normalize_pit_policy",
    "resolve_pit_policy",
    "select_pit_universe",
    "select_pit_universe_from_context",
]
