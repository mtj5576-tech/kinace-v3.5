"""Deterministic daily orchestration for the Kinace V3.2 research loop."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import PROJECT_ROOT, database_path, validate_database_path
from .data_collection import (
    DEFAULT_DEVELOPMENT_GATE_PATH,
    DEFAULT_RELEASE_GATE_PATH,
    RevisionStore,
    TencentMarketProvider,
    TushareMarketProvider,
    V2BootstrapProvider,
    run_gate,
)
from .feature_snapshot import canonical_json_bytes, sha256_bytes
from .history_features import DEFAULT_MODEL_CONFIG, build_history_snapshots
from .market_sync import sync_market
from .pit_universe import HISTORICAL_SNAPSHOT_MODE, PitUniverseError, resolve_pit_policy
from .portfolio_backtest import (
    PortfolioBacktestError,
    load_market_bars,
    run_portfolio_backtest,
)
from .score_replay import (
    candidate_id_for,
    load_replay_artifact,
    validate_candidate_config,
)
from .search import run_gated_search
from .trial_backtest import _atr_as_of, normalize_backtest_config


SCHEMA_VERSION = 1
DEFAULT_V32_SNAPSHOT_DIR = Path(r"D:\kinacesqlv3.2\snapshots")
DEFAULT_PARITY_PATH = Path(r"D:\kinacesqlv3.2\parity\latest.json")
DEFAULT_DAILY_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "daily"
DEFAULT_DAILY_REPORT_DIR = PROJECT_ROOT / "reports" / "daily"
DEFAULT_V2_DATABASE = Path(r"D:\kinacesql\kinace_v2_1.sqlite3")


class DailyRunError(RuntimeError):
    """Raised when a daily research run cannot complete safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normal_date(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if len(text) < 10 or text[4] != "-" or text[7] != "-":
        raise DailyRunError(f"{field} must be YYYY-MM-DD")
    try:
        return datetime.fromisoformat(text[:10]).date().isoformat()
    except ValueError as exc:
        raise DailyRunError(f"{field} must be YYYY-MM-DD") from exc


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
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(target)
    return target


def _connect(database: str | Path) -> sqlite3.Connection:
    target = validate_database_path(database)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_daily_run_schema(database: str | Path | None = None) -> Path:
    target = database_path() if database is None else validate_database_path(database)
    with _connect(target) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS daily_runs (
                run_id TEXT PRIMARY KEY,
                decision_date TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at_utc TEXT NOT NULL,
                completed_at_utc TEXT,
                candidate_id TEXT,
                gate_status TEXT,
                snapshot_id TEXT,
                replay_id TEXT,
                backtest_run_id TEXT,
                manifest_path TEXT,
                report_path TEXT,
                stage_json TEXT NOT NULL,
                error_text TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_daily_runs_decision_date
                ON daily_runs(decision_date, started_at_utc);
            """
        )
    return target


def _record_daily_run(
    database: str | Path,
    *,
    run_id: str,
    decision_date: str,
    status: str,
    started_at_utc: str,
    completed_at_utc: str | None,
    candidate_id: str | None,
    gate_status: str | None,
    snapshot_id: str | None,
    replay_id: str | None,
    backtest_run_id: str | None,
    manifest_path: str | None,
    report_path: str | None,
    stages: Mapping[str, Any],
    error_text: str | None,
) -> None:
    initialize_daily_run_schema(database)
    ledger_stages = {
        str(name): {
            "status": value.get("status"),
            "detail": value.get("detail"),
        }
        for name, value in stages.items()
        if isinstance(value, Mapping)
    }
    with _connect(database) as connection:
        connection.execute(
            """
            INSERT INTO daily_runs (
                run_id, decision_date, status, started_at_utc, completed_at_utc,
                candidate_id, gate_status, snapshot_id, replay_id, backtest_run_id,
                manifest_path, report_path, stage_json, error_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                decision_date=excluded.decision_date,
                status=excluded.status,
                started_at_utc=excluded.started_at_utc,
                completed_at_utc=excluded.completed_at_utc,
                candidate_id=excluded.candidate_id,
                gate_status=excluded.gate_status,
                snapshot_id=excluded.snapshot_id,
                replay_id=excluded.replay_id,
                backtest_run_id=excluded.backtest_run_id,
                manifest_path=excluded.manifest_path,
                report_path=excluded.report_path,
                stage_json=excluded.stage_json,
                error_text=excluded.error_text
            """,
            (
                run_id,
                decision_date,
                status,
                started_at_utc,
                completed_at_utc,
                candidate_id,
                gate_status,
                snapshot_id,
                replay_id,
                backtest_run_id,
                manifest_path,
                report_path,
                canonical_json_bytes(ledger_stages, trailing_newline=False).decode("utf-8"),
                error_text,
            ),
        )


class _DailyRunLock(AbstractContextManager["_DailyRunLock"]):
    def __init__(self, path: str | Path, *, stale_after_seconds: float) -> None:
        self.path = Path(path).resolve(strict=False)
        self.stale_after_seconds = float(stale_after_seconds)
        self.token: str | None = None

    def __enter__(self) -> "_DailyRunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                handle = os.open(
                    str(self.path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                token = f"{os.getpid()}-{time.time_ns()}"
                os.write(handle, token.encode("ascii"))
                os.close(handle)
                self.token = token
                return self
            except FileExistsError as exc:
                if attempt == 0 and self._is_stale():
                    self.path.unlink(missing_ok=True)
                    continue
                raise DailyRunError(
                    f"another daily run holds the lock: {self.path}"
                ) from exc
        raise DailyRunError(f"cannot acquire daily run lock: {self.path}")

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        return age > self.stale_after_seconds

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.token is None:
            return
        try:
            current = self.path.read_text(encoding="ascii")
        except OSError:
            return
        if current == self.token:
            self.path.unlink(missing_ok=True)


def _execute_with_retries(
    stage: str,
    function: Callable[[], Any],
    *,
    retries: int,
    retry_delay_seconds: float,
) -> Any:
    attempts = max(1, int(retries) + 1)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return function()
        except Exception as exc:  # Stage boundaries are recorded by the caller.
            last_error = exc
            if attempt + 1 < attempts and retry_delay_seconds > 0:
                time.sleep(float(retry_delay_seconds) * (attempt + 1))
    raise DailyRunError(
        f"stage {stage} failed after {attempts} attempt(s): {last_error}"
    ) from last_error


def _latest_trade_date(store: RevisionStore) -> str:
    dates = [
        str(record.get("trade_date") or "")
        for row in store.current_records("daily_bars")
        if (record := (row.get("record") if isinstance(row.get("record"), Mapping) else row))
        and str(record.get("trade_date") or "")
    ]
    if not dates:
        raise DailyRunError("daily bars are empty; provide --decision-date after data ingestion")
    return max(dates)


def _default_codes(v2_database: str | Path) -> list[str]:
    provider = V2BootstrapProvider(v2_database)
    return [record.ts_code for record in provider.fetch_stock_metadata()]


def _run_sync_stage(
    *,
    database: str | Path,
    codes: Sequence[str] | None,
    v2_database: str | Path,
    start_date: str | None,
    end_date: str | None,
    count: int,
    parity_output: str | Path,
    timeout: float,
    provider_name: str,
    provider_max_retries: int,
    provider_retry_backoff: float,
) -> dict[str, Any]:
    resolved_codes = list(codes) if codes else _default_codes(v2_database)
    if provider_name == "tushare":
        provider = TushareMarketProvider(
            timeout=timeout,
            default_count=count,
            max_retries=provider_max_retries,
            retry_backoff=provider_retry_backoff,
        )
    elif provider_name == "tencent":
        provider = TencentMarketProvider(timeout=timeout, default_count=count)
    else:
        raise DailyRunError(f"unsupported market provider: {provider_name}")
    return sync_market(
        database,
        provider,
        resolved_codes,
        start_date=start_date,
        end_date=end_date,
        count=count,
        parity_output=parity_output,
    )


def _run_snapshot_stage(
    *,
    database: str | Path,
    snapshot_dir: str | Path,
    start_date: str,
    end_date: str,
    model_config: Mapping[str, Any],
    min_history: int,
    pit_policy: Mapping[str, Any] | None,
    pit_policy_path: str | Path | None,
    snapshot_mode: str,
) -> dict[str, Any]:
    try:
        policy_resolution = resolve_pit_policy(
            pit_policy,
            policy_path=pit_policy_path,
            mode=snapshot_mode,
        )
    except PitUniverseError as exc:
        raise DailyRunError(str(exc)) from exc
    return build_history_snapshots(
        RevisionStore(database),
        database=database,
        output_dir=snapshot_dir,
        start_date=start_date,
        end_date=end_date,
        min_history=min_history,
        model_config=model_config,
        pit_policy=policy_resolution["policy"],
        pit_policy_path=policy_resolution["policy_path"],
        snapshot_mode=policy_resolution["mode"],
    )


def _run_gate_stage(
    *,
    database: str | Path,
    gate_output: str | Path,
    mode: str = "release",
) -> dict[str, Any]:
    return run_gate(RevisionStore(database), mode=mode, output_path=gate_output)


def _run_search_stage(
    *,
    database: str | Path,
    gate_output: str | Path,
    snapshot_path: str | Path,
    candidate_path: str | Path,
    output_path: str | Path,
    code_version: str,
    development: bool = False,
) -> dict[str, Any]:
    return run_gated_search(
        data_gate_path=gate_output,
        snapshot_path=snapshot_path,
        candidates_path=candidate_path,
        output_path=output_path,
        database=database,
        code_version=code_version,
        development=development,
    )


def _strategy_payload(
    search_result: Mapping[str, Any],
    *,
    database: str | Path,
    decision_date: str,
    top_n: int,
    backtest_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidates = search_result.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise DailyRunError("search result contains no candidates")
    selected = candidates[0]
    replay_path = selected.get("replay_path")
    if not isinstance(replay_path, str) or not replay_path:
        raise DailyRunError("selected search candidate has no replay path")
    replay = load_replay_artifact(replay_path)
    scores = [
        dict(row)
        for row in replay.get("scores", [])
        if str(row.get("decision_date") or "") == decision_date
    ]
    scores.sort(key=lambda row: (-float(row.get("score") or 0.0), str(row.get("code") or "")))
    selected_scores = scores[: max(0, int(top_n))]
    settings = normalize_backtest_config(backtest_config)
    try:
        market_bars = load_market_bars(database)
    except PortfolioBacktestError:
        market_bars = {}
    atr_period = int(settings["atr_period"])
    stop_multiple = float(settings["stop_atr_multiple"])
    target_multiple = float(settings["target_atr_multiple"])
    enriched_picks: list[dict[str, Any]] = []
    for score in selected_scores:
        pick = dict(score)
        code = str(pick.get("code") or "")
        bars = market_bars.get(code, [])
        reference_bar = next(
            (
                bar
                for bar in reversed(bars)
                if str(bar.get("trade_date") or "") <= decision_date
            ),
            None,
        )
        atr = _atr_as_of(bars, decision_date, atr_period) if bars else 0.0
        reference_price = (
            float(reference_bar["close"]) if reference_bar is not None else None
        )
        if reference_price is not None and atr > 0:
            stop_loss = reference_price - stop_multiple * atr
            take_profit = reference_price + target_multiple * atr
            pick.update(
                {
                    "atr": round(atr, 6),
                    "atr_period": atr_period,
                    "reference_price": round(reference_price, 6),
                    "stop_loss": round(stop_loss, 6),
                    "take_profit": round(take_profit, 6),
                    "stop_loss_pct": round(stop_loss / reference_price - 1.0, 8),
                    "take_profit_pct": round(take_profit / reference_price - 1.0, 8),
                }
            )
        else:
            pick.update(
                {
                    "atr": round(atr, 6) if atr else None,
                    "atr_period": atr_period,
                    "reference_price": reference_price,
                    "stop_loss": None,
                    "take_profit": None,
                    "stop_loss_pct": None,
                    "take_profit_pct": None,
                }
            )
        enriched_picks.append(pick)
    return {
        "artifact_type": "kinace_v3_daily_strategy",
        "decision_date": decision_date,
        "candidate_id": selected.get("candidate_id"),
        "replay_id": selected.get("replay_id"),
        "selection_rule": "highest_candidate_scores_for_decision_date",
        "trade_plan": {
            "price_basis": "decision_close",
            "atr_period": atr_period,
            "stop_atr_multiple": stop_multiple,
            "target_atr_multiple": target_multiple,
            "max_hold_sessions": int(settings["max_hold_sessions"]),
            "execution_note": (
                "Reference lines use the decision-date close and ATR visible on "
                "that date. Recalculate final stop and target from the actual "
                "next-session entry price with the same ATR before executing."
            ),
        },
        "production_ready": False,
        "production_changed": False,
        "picks": enriched_picks,
        "scored_rows": len(scores),
    }


def _selected_replay_id(search_result: Mapping[str, Any] | None) -> str | None:
    if not isinstance(search_result, Mapping):
        return None
    candidates = search_result.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    selected = candidates[0]
    if not isinstance(selected, Mapping):
        return None
    replay_id = selected.get("replay_id")
    return str(replay_id) if isinstance(replay_id, str) and replay_id else None


def _selected_snapshot_id(
    snapshot_result: Mapping[str, Any] | None,
    decision_date: str,
) -> str | None:
    if not isinstance(snapshot_result, Mapping):
        return None
    snapshots = snapshot_result.get("snapshots")
    if not isinstance(snapshots, list):
        return None
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping):
            continue
        if str(snapshot.get("decision_date") or "") != decision_date:
            continue
        snapshot_id = snapshot.get("snapshot_id")
        return str(snapshot_id) if isinstance(snapshot_id, str) and snapshot_id else None
    return None


def _report_markdown(payload: Mapping[str, Any]) -> str:
    stages = payload.get("stages") or {}
    strategy = payload.get("strategy") or {}
    portfolio = payload.get("portfolio") or {}
    lines = [
        "# Kinace V3.3 Daily Run",
        "",
        f"- Run ID: `{payload.get('run_id')}`",
        f"- Decision date: `{payload.get('decision_date')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Candidate: `{payload.get('candidate_id')}`",
        "- Production changed: `false`",
        "",
        "## Stages",
        "",
        "| Stage | Status | Detail |",
        "|---|---|---|",
    ]
    for name, stage in stages.items():
        status = stage.get("status") if isinstance(stage, Mapping) else "unknown"
        detail = stage.get("detail") if isinstance(stage, Mapping) else str(stage)
        lines.append(f"| `{name}` | `{status}` | {detail or ''} |")
    lines.extend(["", "## Daily Strategy", ""])
    picks = strategy.get("picks") if isinstance(strategy, Mapping) else None
    if isinstance(picks, list) and picks:
        lines.extend(
            [
                "| Code | Score | Reference | ATR | Stop | Target |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for pick in picks:
            lines.append(
                f"| `{pick.get('code')}` | {pick.get('score')} | "
                f"{pick.get('reference_price')} | {pick.get('atr')} | "
                f"{pick.get('stop_loss')} | {pick.get('take_profit')} |"
            )
    else:
        lines.append("No strategy candidates were produced.")
    metrics = portfolio.get("metrics") if isinstance(portfolio, Mapping) else None
    lines.extend(["", "## Continuous Portfolio", ""])
    if isinstance(metrics, Mapping):
        lines.extend(
            [
                f"- Trades: `{metrics.get('trade_count')}`",
                f"- Final equity: `{metrics.get('final_equity')}`",
                f"- Total return: `{metrics.get('total_return')}`",
                f"- Max drawdown: `{metrics.get('max_drawdown')}`",
                f"- Win rate: `{metrics.get('win_rate')}`",
                f"- Profit factor: `{metrics.get('profit_factor')}`",
            ]
        )
    else:
        lines.append("Continuous portfolio backtest was skipped or unavailable.")
    lines.append("")
    return "\n".join(lines)


def run_daily(
    *,
    database: str | Path | None = None,
    decision_date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    candidate_config: Mapping[str, Any] | None = None,
    snapshot_dir: str | Path = DEFAULT_V32_SNAPSHOT_DIR,
    output_dir: str | Path = DEFAULT_DAILY_OUTPUT_DIR,
    report_dir: str | Path = DEFAULT_DAILY_REPORT_DIR,
    gate_output: str | Path = DEFAULT_RELEASE_GATE_PATH,
    sync_market: bool = True,
    market_start_date: str | None = None,
    market_end_date: str | None = None,
    market_count: int = 520,
    codes: Sequence[str] | None = None,
    v2_database: str | Path = DEFAULT_V2_DATABASE,
    parity_output: str | Path = DEFAULT_PARITY_PATH,
    provider_timeout: float = 20.0,
    provider_name: str = "tushare",
    provider_max_retries: int = 4,
    provider_retry_backoff: float = 1.0,
    run_purpose: str = "full",
    skip_portfolio: bool = False,
    backtest_config: Mapping[str, Any] | None = None,
    portfolio_output: str | Path | None = None,
    top_n: int = 10,
    min_history: int = 65,
    code_version: str = "kinace-v3",
    retries: int = 1,
    retry_delay_seconds: float = 0.5,
    lock_stale_after_seconds: float = 21_600.0,
    pit_policy: Mapping[str, Any] | None = None,
    pit_policy_path: str | Path | None = None,
    snapshot_mode: str = HISTORICAL_SNAPSHOT_MODE,
    runner_overrides: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Run the complete daily research chain with deterministic, atomic outputs."""
    target_database = database_path() if database is None else validate_database_path(database)
    candidate = validate_candidate_config(candidate_config or DEFAULT_MODEL_CONFIG)
    candidate_id = candidate_id_for(candidate)
    resolved_snapshot_dir = Path(snapshot_dir).resolve(strict=False)
    resolved_output_dir = Path(output_dir).resolve(strict=False)
    resolved_report_dir = Path(report_dir).resolve(strict=False)
    from .synthetic_sandbox import TRACK, active_track

    track = active_track(
        target_database,
        resolved_snapshot_dir,
        resolved_output_dir,
        resolved_report_dir,
        gate_output,
    )
    store = RevisionStore(target_database)

    if sync_market and not any((decision_date, start_date, end_date)):
        raise DailyRunError(
            "decision-date, start-date, or end-date is required when market sync is enabled"
        )
    if decision_date is None and start_date is None and end_date is None:
        decision_date = _latest_trade_date(store)
    decision_start = _normal_date(start_date or decision_date, "start_date")
    decision_end = _normal_date(end_date or decision_date or decision_start, "end_date")
    if decision_start > decision_end:
        raise DailyRunError("start_date must not be later than end_date")
    resolved_decision_date = _normal_date(
        decision_date or decision_end, "decision_date"
    )
    if not decision_start <= resolved_decision_date <= decision_end:
        raise DailyRunError("decision_date must be inside the snapshot date range")
    operational_run = run_purpose in {"preopen", "postclose"}
    gate_mode = "development" if operational_run else "release"
    if operational_run and Path(gate_output).resolve(strict=False) == Path(
        DEFAULT_RELEASE_GATE_PATH
    ).resolve(strict=False):
        gate_output = DEFAULT_DEVELOPMENT_GATE_PATH

    run_id = sha256_bytes(
        canonical_json_bytes(
            {
                "decision_date": resolved_decision_date,
                "start_date": decision_start,
                "end_date": decision_end,
                "candidate_id": candidate_id,
                "database": str(target_database),
                "snapshot_dir": str(resolved_snapshot_dir),
                "run_purpose": str(run_purpose),
                "track": track,
            },
            trailing_newline=False,
        )
    )
    if track == TRACK:
        run_id = f"sandbox-{run_id}"
    run_dir = resolved_output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = _write_json(candidate, run_dir / "candidate.json")
    search_output = run_dir / "search.json"
    strategy_output = run_dir / "strategy.json"
    manifest_path = run_dir / "daily-run.json"
    report_path = resolved_report_dir / f"{run_id}.md"
    started_at = _utc_now()
    stages: dict[str, dict[str, Any]] = {
        "sync": {"status": "pending", "detail": None},
        "snapshots": {"status": "pending", "detail": None},
        "gate": {"status": "pending", "detail": None},
        "search": {"status": "pending", "detail": None},
        "strategy": {"status": "pending", "detail": None},
        "portfolio": {"status": "pending", "detail": None},
    }
    overrides = runner_overrides or {}
    _record_daily_run(
        target_database,
        run_id=run_id,
        decision_date=resolved_decision_date,
        status="running",
        started_at_utc=started_at,
        completed_at_utc=None,
        candidate_id=candidate_id,
        gate_status=None,
        snapshot_id=None,
        replay_id=None,
        backtest_run_id=None,
        manifest_path=str(manifest_path),
        report_path=str(report_path),
        stages=stages,
        error_text=None,
    )

    sync_result: dict[str, Any] | None = None
    snapshot_result: dict[str, Any] | None = None
    gate_result: dict[str, Any] | None = None
    search_result: dict[str, Any] | None = None
    strategy_result: dict[str, Any] | None = None
    portfolio_result: dict[str, Any] | None = None
    status = "completed"
    error_text: str | None = None

    try:
        with _DailyRunLock(
            resolved_output_dir / ".daily-run.lock",
            stale_after_seconds=lock_stale_after_seconds,
        ):
            if sync_market:
                sync_runner = overrides.get("sync", _run_sync_stage)
                sync_result = _execute_with_retries(
                    "sync",
                    lambda: sync_runner(
                        database=target_database,
                        codes=codes,
                        v2_database=v2_database,
                        start_date=market_start_date,
                        end_date=market_end_date,
                        count=market_count,
                        parity_output=parity_output,
                        timeout=provider_timeout,
                        provider_name=provider_name,
                        provider_max_retries=provider_max_retries,
                        provider_retry_backoff=provider_retry_backoff,
                    ),
                    retries=retries,
                    retry_delay_seconds=retry_delay_seconds,
                )
                stages["sync"] = {
                    "status": "completed",
                    "detail": f"{len(sync_result.get('codes') or [])} codes",
                    "result": sync_result,
                }
            else:
                stages["sync"] = {"status": "skipped", "detail": "sync_market=false"}

            snapshot_runner = overrides.get("snapshots", _run_snapshot_stage)
            snapshot_result = _execute_with_retries(
                "snapshots",
                lambda: snapshot_runner(
                    database=target_database,
                    snapshot_dir=resolved_snapshot_dir,
                    start_date=decision_start,
                    end_date=decision_end,
                    model_config=candidate,
                    min_history=min_history,
                    pit_policy=pit_policy,
                    pit_policy_path=pit_policy_path,
                    snapshot_mode=snapshot_mode,
                ),
                retries=retries,
                retry_delay_seconds=retry_delay_seconds,
            )
            if int(snapshot_result.get("snapshot_count") or 0) < 1:
                raise DailyRunError(
                    f"no feature snapshot was produced for {decision_start}..{decision_end}"
                )
            if (
                snapshot_result.get("pit_ready") is not None
                and snapshot_mode == HISTORICAL_SNAPSHOT_MODE
                and (
                    snapshot_result.get("pit_ready") is not True
                    or snapshot_result.get("industry_lookahead_safe") is not True
                )
            ):
                raise DailyRunError(
                    "historical snapshots are not PIT-ready or are lookahead-unsafe"
                )
            if snapshot_mode != HISTORICAL_SNAPSHOT_MODE:
                if (
                    snapshot_result.get("pit_ready") is not False
                    or snapshot_result.get("industry_lookahead_safe") is not False
                    or snapshot_result.get("industry_source") != "current_metadata"
                ):
                    raise DailyRunError(
                        "live_current snapshots must be explicitly marked "
                        "industry_lookahead_safe=false, pit_ready=false, "
                        "industry_source=current_metadata"
                    )
                if not skip_portfolio:
                    raise DailyRunError(
                        "live_current snapshots cannot enter historical portfolio "
                        "research; use skip_portfolio=true"
                    )
            stages["snapshots"] = {
                "status": "completed",
                "detail": (
                    f"{snapshot_result.get('snapshot_count')} snapshots, "
                    f"{snapshot_result.get('row_count')} rows"
                ),
                "result": snapshot_result,
            }

            gate_runner = overrides.get("gate", _run_gate_stage)
            gate_result = _execute_with_retries(
                "gate",
                lambda: gate_runner(
                    database=target_database,
                    gate_output=gate_output,
                    mode=gate_mode,
                ),
                retries=retries,
                retry_delay_seconds=retry_delay_seconds,
            )
            if operational_run:
                gate_ready = bool(
                    gate_result.get("ready")
                    and gate_result.get("development_eligible") is True
                )
            else:
                gate_ready = bool(
                    gate_result.get("ready")
                    and not gate_result.get("development_only")
                    and gate_result.get("production_eligible") is not False
                )
            stages["gate"] = {
                "status": "completed" if gate_ready else "blocked",
                "detail": str(gate_result.get("status") or "missing"),
                "result": gate_result,
            }
            if not gate_ready:
                status = "blocked_data_gate"
                stages["search"] = {"status": "skipped", "detail": "release gate blocked"}
                stages["strategy"] = {"status": "skipped", "detail": "release gate blocked"}
                stages["portfolio"] = {"status": "skipped", "detail": "release gate blocked"}
            else:
                snapshot_path = resolved_snapshot_dir / f"{resolved_decision_date}.json"
                if not snapshot_path.is_file():
                    raise DailyRunError(
                        f"selected decision snapshot is missing: {snapshot_path}"
                    )
                search_runner = overrides.get("search", _run_search_stage)
                search_result = _execute_with_retries(
                    "search",
                    lambda: search_runner(
                        database=target_database,
                        gate_output=gate_output,
                        snapshot_path=snapshot_path,
                        candidate_path=candidate_path,
                        output_path=search_output,
                        code_version=code_version,
                        development=operational_run,
                    ),
                    retries=retries,
                    retry_delay_seconds=retry_delay_seconds,
                )
                stages["search"] = {
                    "status": "completed",
                    "detail": (
                        f"{search_result.get('candidate_count')} candidates, "
                        f"selected={search_result.get('selected_candidate_id')}"
                    ),
                    "result": search_result,
                }
                strategy_result = _strategy_payload(
                    search_result,
                    database=target_database,
                    decision_date=resolved_decision_date,
                    top_n=top_n,
                    backtest_config=backtest_config,
                )
                _write_json(strategy_result, strategy_output)
                strategy_result["output_path"] = str(strategy_output)
                stages["strategy"] = {
                    "status": "completed",
                    "detail": f"{len(strategy_result.get('picks') or [])} picks",
                    "result": {
                        "output_path": str(strategy_output),
                        "scored_rows": strategy_result.get("scored_rows"),
                    },
                }

                if skip_portfolio:
                    stages["portfolio"] = {
                        "status": "skipped",
                        "detail": "skip_portfolio=true",
                    }
                else:
                    portfolio_runner = overrides.get("portfolio", run_portfolio_backtest)
                    portfolio_result = _execute_with_retries(
                        "portfolio",
                        lambda: portfolio_runner(
                            target_database,
                            candidate,
                            resolved_snapshot_dir,
                            config=backtest_config,
                            output_path=portfolio_output,
                            code_version=code_version,
                        ),
                        retries=retries,
                        retry_delay_seconds=retry_delay_seconds,
                    )
                    metrics = portfolio_result.get("metrics") or {}
                    stages["portfolio"] = {
                        "status": "completed",
                        "detail": (
                            f"{metrics.get('trade_count')} trades, "
                            f"return={metrics.get('total_return')}"
                        ),
                        "result": {
                            "run_id": portfolio_result.get("run_id"),
                            "output_path": portfolio_result.get("output_path"),
                            "metrics": metrics,
                        },
                    }
    except Exception as exc:
        status = "failed"
        error_text = str(exc)
        for stage_name, stage in stages.items():
            if stage.get("status") == "pending":
                stages[stage_name] = {"status": "not_run", "detail": error_text}
        completed_at = _utc_now()
        payload = {
            "artifact_type": "kinace_v3_daily_run",
            "schema_version": SCHEMA_VERSION,
            "track": track,
            "run_id": run_id,
            "decision_date": resolved_decision_date,
            "date_range": {"start": decision_start, "end": decision_end},
            "status": status,
            "candidate_id": candidate_id,
            "started_at_utc": started_at,
            "completed_at_utc": completed_at,
            "stages": stages,
            "error": error_text,
            "production_ready": False,
            "production_changed": False,
        }
        _write_json(payload, manifest_path)
        _record_daily_run(
            target_database,
            run_id=run_id,
            decision_date=resolved_decision_date,
            status=status,
            started_at_utc=started_at,
            completed_at_utc=completed_at,
            candidate_id=candidate_id,
            gate_status=(gate_result or {}).get("status"),
            snapshot_id=_selected_snapshot_id(snapshot_result, resolved_decision_date),
            replay_id=_selected_replay_id(search_result),
            backtest_run_id=(portfolio_result or {}).get("run_id"),
            manifest_path=str(manifest_path),
            report_path=str(report_path),
            stages=stages,
            error_text=error_text,
        )
        raise

    completed_at = _utc_now()
    payload = {
        "artifact_type": "kinace_v3_daily_run",
        "schema_version": SCHEMA_VERSION,
        "track": track,
        "run_id": run_id,
        "decision_date": resolved_decision_date,
        "date_range": {"start": decision_start, "end": decision_end},
        "status": status,
        "run_purpose": str(run_purpose),
        "candidate_id": candidate_id,
        "started_at_utc": started_at,
        "completed_at_utc": completed_at,
        "stages": stages,
        "sync": sync_result,
        "snapshots": snapshot_result,
        "gate": gate_result,
        "search": search_result,
        "strategy": strategy_result,
        "portfolio": portfolio_result,
        "manifest_path": str(manifest_path),
        "report_path": str(report_path),
        "production_ready": False,
        "production_changed": False,
    }
    _write_text(_report_markdown(payload), report_path)
    _write_json(payload, manifest_path)
    _record_daily_run(
        target_database,
        run_id=run_id,
        decision_date=resolved_decision_date,
        status=status,
        started_at_utc=started_at,
        completed_at_utc=completed_at,
        candidate_id=candidate_id,
        gate_status=(gate_result or {}).get("status"),
        snapshot_id=_selected_snapshot_id(snapshot_result, resolved_decision_date),
        replay_id=_selected_replay_id(search_result),
        backtest_run_id=(portfolio_result or {}).get("run_id"),
        manifest_path=str(manifest_path),
        report_path=str(report_path),
        stages=stages,
        error_text=error_text,
    )
    return payload


__all__ = [
    "DailyRunError",
    "SCHEMA_VERSION",
    "initialize_daily_run_schema",
    "run_daily",
]
