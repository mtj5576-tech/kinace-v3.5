from __future__ import annotations

import hashlib
import json
import math
import statistics
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PROJECT_ROOT, database_path, load_config
from .data_collection.gate import DEFAULT_RELEASE_OUTPUT_PATH
from .experiments import record_experiment, record_replay
from .feature_snapshot import canonical_json_bytes, load_feature_snapshot_artifact
from .score_replay import candidate_id_for, recompute_candidate_scores, replay_candidate


DEFAULT_SEARCH_OUTPUT_PATH = PROJECT_ROOT / "outputs" / "search.json"
MAX_SEARCH_CANDIDATES = 200


class SearchGateError(RuntimeError):
    """Raised when model search is attempted without a ready data gate."""


class SearchError(ValueError):
    """Raised when a gated search request is invalid."""


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SearchError(f"{field} must be an object")
    return value


def _load_json(path: str | Path, label: str) -> Any:
    target = Path(path).resolve(strict=False)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SearchError(f"{label} is missing: {target}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SearchError(f"cannot load {label}: {exc}") from exc


def load_ready_data_gate(
    path: str | Path = DEFAULT_RELEASE_OUTPUT_PATH,
    *,
    development: bool = False,
) -> dict[str, Any]:
    """Load a release gate by default; a development gate requires opt-in."""
    target = Path(path).resolve(strict=False)
    if not target.is_file():
        raise SearchGateError(f"data gate is missing: {target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SearchGateError(f"data gate is unreadable: {target}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SearchGateError("data gate root must be an object")

    status = str(payload.get("status") or "missing")
    failed = payload.get("failed_hard_gates")
    gate_mode = payload.get("mode")
    development_only = payload.get("development_only") is True
    accepted_status = (
        {"ready", "release_ready"}
        if not development
        else {"ready", "development_ready"}
    )
    mode_ready = (
        development
        and gate_mode == "development"
        and development_only
        and payload.get("development_eligible") is True
    ) or (
        not development
        and not development_only
        and (
            gate_mode is None
            or (
                gate_mode == "release"
                and payload.get("production_eligible") is True
            )
        )
    )
    if (
        payload.get("ready") is not True
        or status not in accepted_status
        or failed
        or not mode_ready
    ):
        failed_text = ", ".join(str(item) for item in failed) if failed else "none"
        raise SearchGateError(
            "data gate is blocked or missing; "
            f"mode={gate_mode or 'release-compatible'}, status={status}, "
            f"ready={bool(payload.get('ready'))}, "
            f"failed_hard_gates={failed_text}"
        )
    return payload


def _set_dotted(target: dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = [part for part in dotted_key.split(".") if part]
    if not parts:
        raise SearchError("search_space keys must be non-empty")
    current = target
    for part in parts[:-1]:
        child = current.get(part)
        if child is None:
            child = {}
            current[part] = child
        if not isinstance(child, dict):
            raise SearchError(f"search_space path is not an object: {dotted_key}")
        current = child
    current[parts[-1]] = deepcopy(value)


def _grid_candidates(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    base = deepcopy(dict(_mapping(payload.get("base_config"), "base_config")))
    search_space = _mapping(payload.get("search_space"), "search_space")
    if not search_space:
        return [base]

    keys = sorted(str(key) for key in search_space)
    values_by_key: list[list[Any]] = []
    for key in keys:
        values = search_space[key]
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or not values:
            raise SearchError(f"search_space.{key} must be a non-empty list")
        values_by_key.append(list(values))

    result: list[dict[str, Any]] = []

    def visit(index: int, current: dict[str, Any]) -> None:
        if len(result) > MAX_SEARCH_CANDIDATES:
            raise SearchError(f"search space exceeds {MAX_SEARCH_CANDIDATES} candidates")
        if index == len(keys):
            result.append(deepcopy(current))
            return
        key = keys[index]
        for value in values_by_key[index]:
            candidate = deepcopy(current)
            _set_dotted(candidate, key, value)
            visit(index + 1, candidate)

    visit(0, base)
    return result


def candidate_configs_from_payload(payload: Any) -> list[dict[str, Any]]:
    """Accept an explicit candidate list or a deterministic pre-registered grid."""
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, Mapping):
        if "candidates" in payload:
            candidates = payload["candidates"]
        elif "search_space" in payload:
            candidates = _grid_candidates(payload)
        else:
            candidates = [payload]
    else:
        raise SearchError("candidate payload must be an object or list")

    if not isinstance(candidates, list) or not candidates:
        raise SearchError("at least one candidate configuration is required")
    if len(candidates) > MAX_SEARCH_CANDIDATES:
        raise SearchError(f"candidate count exceeds {MAX_SEARCH_CANDIDATES}")
    return [
        deepcopy(dict(_mapping(item, f"candidates[{index}]")))
        for index, item in enumerate(candidates)
    ]


def _numeric_label(row: Mapping[str, Any]) -> float | None:
    labels = row.get("labels")
    if not isinstance(labels, Mapping):
        return None
    for key in ("forward_return", "return", "label"):
        value = labels.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def _average_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + 1 + end) / 2.0
        for index in order[start:end]:
            ranks[index] = rank
        start = end
    return ranks


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right)
    )
    left_ss = sum((value - left_mean) ** 2 for value in left)
    right_ss = sum((value - right_mean) ** 2 for value in right)
    if left_ss <= 0 or right_ss <= 0:
        return None
    return numerator / math.sqrt(left_ss * right_ss)


def evaluate_scores(
    snapshot: Mapping[str, Any],
    scores: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate replay scores against frozen labels without invoking production gates."""
    rows = snapshot.get("rows")
    if not isinstance(rows, list):
        raise SearchError("snapshot rows must be a list")
    labels: dict[tuple[str, str], float] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        date = str(row.get("decision_date") or "")
        code = str(row.get("code") or "")
        label = _numeric_label(row)
        if date and code and label is not None:
            labels[(date, code)] = label

    grouped: dict[str, list[tuple[float, float]]] = {}
    for score_row in scores:
        key = (str(score_row.get("decision_date") or ""), str(score_row.get("code") or ""))
        label = labels.get(key)
        if label is None:
            continue
        score = score_row.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        grouped.setdefault(key[0], []).append((float(score), label))

    rank_ics: list[float] = []
    top_returns: list[float] = []
    top_hits: list[float] = []
    for date in sorted(grouped):
        pairs = grouped[date]
        if len(pairs) < 2:
            continue
        score_ranks = _average_ranks([pair[0] for pair in pairs])
        label_ranks = _average_ranks([pair[1] for pair in pairs])
        rank_ic = _pearson(score_ranks, label_ranks)
        if rank_ic is not None:
            rank_ics.append(rank_ic)
        top_label = max(pairs, key=lambda pair: (pair[0], pair[1]))[1]
        top_returns.append(top_label)
        top_hits.append(1.0 if top_label > 0 else 0.0)

    return {
        "dates_scored": len(grouped),
        "dates_with_rank_ic": len(rank_ics),
        "mean_rank_ic": statistics.fmean(rank_ics) if rank_ics else None,
        "mean_top_return": statistics.fmean(top_returns) if top_returns else None,
        "top_hit_rate": statistics.fmean(top_hits) if top_hits else None,
    }


def _write_json(payload: Mapping[str, Any], output_path: str | Path) -> Path:
    target = Path(output_path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(canonical_json_bytes(payload))
    temporary.replace(target)
    return target


def run_gated_search(
    *,
    data_gate_path: str | Path = DEFAULT_RELEASE_OUTPUT_PATH,
    snapshot_path: str | Path,
    candidates_path: str | Path,
    output_path: str | Path = DEFAULT_SEARCH_OUTPUT_PATH,
    database: str | Path | None = None,
    code_version: str = "kinace-v3",
    objective_config: Mapping[str, Any] | None = None,
    development: bool = False,
) -> dict[str, Any]:
    """Run pre-registered candidate scoring only after the data gate is ready."""
    gate = load_ready_data_gate(data_gate_path, development=development)
    snapshot_file = Path(snapshot_path).resolve(strict=False)
    snapshot = load_feature_snapshot_artifact(snapshot_file)
    candidate_payload = _load_json(candidates_path, "candidate configurations")
    candidates = candidate_configs_from_payload(candidate_payload)
    target_database = database_path(load_config()) if database is None else database
    from .synthetic_sandbox import active_track

    track = active_track(target_database, snapshot_file, output_path, data_gate_path)
    objective = deepcopy(dict(objective_config or {}))
    data_snapshot_hash = str(
        gate.get("data_snapshot_hash")
        or hashlib.sha256(snapshot_file.read_bytes()).hexdigest()
    )

    results: list[dict[str, Any]] = []
    for index, candidate_config in enumerate(candidates):
        candidate_id = candidate_id_for(candidate_config)
        replay = replay_candidate(snapshot_file, candidate_config)
        replay_id = record_replay(target_database, replay, code_version=code_version)
        scores = recompute_candidate_scores(snapshot_file, candidate_config)
        metrics = evaluate_scores(snapshot, scores)
        search_space = {"candidate_index": [index], "explicit_candidates": True}
        experiment_id = record_experiment(
            target_database,
            code_version=code_version,
            data_snapshot_hash=data_snapshot_hash,
            search_space=search_space,
            seed=index,
            objective_config=objective,
            candidate_id=candidate_id,
            replay_id=replay_id,
            artifact_paths={
                "snapshot": str(snapshot_file),
                "replay": replay["artifact_path"],
            },
        )
        results.append(
            {
                "candidate_id": candidate_id,
                "replay_id": replay_id,
                "experiment_id": experiment_id,
                "replay_path": replay["artifact_path"],
                "metrics": metrics,
            }
        )

    ranked = sorted(
        results,
        key=lambda item: (
            item["metrics"]["mean_rank_ic"] is not None,
            item["metrics"]["mean_rank_ic"]
            if item["metrics"]["mean_rank_ic"] is not None
            else float("-inf"),
            item["candidate_id"],
        ),
        reverse=True,
    )
    result = {
        "artifact_type": "kinace_v3_search_run",
        "track": track,
        "status": "research_only",
        "data_gate": {
            "status": gate.get("status"),
            "generated_at_utc": gate.get("generated_at_utc"),
            "path": str(Path(data_gate_path).resolve(strict=False)),
        },
        "data_snapshot_hash": data_snapshot_hash,
        "candidate_count": len(results),
        "selected_candidate_id": ranked[0]["candidate_id"] if ranked else None,
        "candidates": ranked,
        "production_ready": False,
        "production_eligible": False,
        "development_only": development,
        "production_changed": False,
        "parity_claimed": False,
        "note": "Search output is research evidence only; promotion remains dry-run only.",
    }
    target = _write_json(result, output_path)
    result["output_path"] = str(target)
    return result


__all__ = [
    "DEFAULT_SEARCH_OUTPUT_PATH",
    "MAX_SEARCH_CANDIDATES",
    "SearchError",
    "SearchGateError",
    "candidate_configs_from_payload",
    "evaluate_scores",
    "load_ready_data_gate",
    "run_gated_search",
]
