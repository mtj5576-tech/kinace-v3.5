"""Read-only local operations portal for Kinace V3.5."""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import DEFAULT_DATABASE_PATH, PROJECT_ROOT


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18765
READ_ONLY = True
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
STRATEGY_HISTORY_START_DATE = "2026-09-16"


@lru_cache(maxsize=256)
def _read_json_cached(path: str, modified_ns: int, size: int) -> dict[str, Any]:
    del modified_ns, size
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_json_stable(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        stat = target.stat()
    except OSError:
        return {}
    return _read_json_cached(str(target), stat.st_mtime_ns, stat.st_size)


def _newest_existing_json(*paths: str | Path) -> Path | None:
    candidates: list[tuple[int, Path]] = []
    for raw in paths:
        target = Path(raw)
        try:
            stat = target.stat()
        except OSError:
            continue
        if target.is_file():
            candidates.append((stat.st_mtime_ns, target))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _latest_portal_sources(root: Path) -> dict[str, Any]:
    reconstruction = root / "reports" / "execution_framework" / "reconstruction"
    integration = root / "reports" / "execution_framework" / "integration"
    gate_source = _newest_existing_json(
        root / "reports" / "scheduler" / "latest-status.json",
        integration / "final-delivery-report.json",
    )
    audit_source = _newest_existing_json(
        root / "reports" / "research_audit_canonical.json",
        reconstruction / "research-audit.json",
    )
    research_source = _newest_existing_json(
        root / "reports" / "research_loop_scheduled.json",
        reconstruction / "research-loop.json",
    )
    gate = _read_json_stable(gate_source) if gate_source is not None else {}
    if gate.get("artifact_type") == "kinace_v3_final_delivery_report":
        gate = {
            "ready": bool(gate.get("release_ready")),
            "status": str(gate.get("status") or ""),
            "unattended_trading_days": int(
                ((gate.get("completed") or {}).get("unattended_trading_days") or 0)
            ),
            "production_ready": bool(gate.get("production_ready")),
            "production_changed": bool(gate.get("production_changed")),
            "source_path": str(gate_source),
        }
    return {
        "gate": gate,
        "audit": _read_json_stable(audit_source) if audit_source is not None else {},
        "research": _read_json_stable(research_source) if research_source is not None else {},
        "sources": {
            "gate": str(gate_source) if gate_source is not None else "",
            "audit": str(audit_source) if audit_source is not None else "",
            "research": str(research_source) if research_source is not None else "",
        },
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath((str(path), str(root)))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(str(root))


def _daily_report_path(
    database: str | Path,
    project_root: str | Path,
    run_id: str,
) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(run_id or "")):
        return None
    connection = sqlite3.connect(str(database), timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        if not _table_exists(connection, "daily_runs"):
            return None
        row = connection.execute(
            "SELECT report_path FROM daily_runs WHERE run_id=? ORDER BY completed_at_utc DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None or not row["report_path"]:
        return None
    allowed_root = (Path(project_root) / "reports" / "daily").resolve(strict=False)
    try:
        target = Path(str(row["report_path"])).resolve(strict=True)
    except OSError:
        return None
    if not _is_within(target, allowed_root) or not target.is_file():
        return None
    return target


def _report_html(markdown: str, *, run_id: str) -> str:
    title = f"Kinace V3.5 Daily Report - {run_id[:12]}"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ margin: 0; background: #f4f6f8; color: #17202a; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; }}
    header {{ position: sticky; top: 0; padding: 14px 22px; background: #fff; border-bottom: 1px solid #d7dee5; }}
    h1 {{ margin: 0; font-size: 18px; }}
    main {{ max-width: 1200px; margin: 22px auto; padding: 0 18px 36px; }}
    pre {{ margin: 0; padding: 20px; background: #fff; border: 1px solid #d7dee5; border-radius: 8px; overflow: auto; white-space: pre-wrap; line-height: 1.55; font: 14px/1.55 Consolas, "Microsoft YaHei", monospace; }}
  </style>
</head>
<body>
  <header><h1>{html.escape(title)}</h1></header>
  <main><pre>{html.escape(markdown)}</pre></main>
</body>
</html>
"""


def _read_json(path: Path) -> dict[str, Any]:
    return _read_json_stable(path)


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _database_rows(
    database: str | Path,
    table: str,
    *,
    order_by: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if not Path(database).is_file():
        return []
    connection = sqlite3.connect(str(database))
    connection.row_factory = sqlite3.Row
    try:
        if not _table_exists(connection, table):
            return []
        rows = connection.execute(
            f"SELECT * FROM {table} ORDER BY {order_by} DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [_row_dict(row) for row in rows]
    finally:
        connection.close()


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _today_update_state(scheduler_runs: list[dict[str, Any]]) -> dict[str, Any]:
    today = datetime.now(CHINA_TZ).date()
    today_runs: list[dict[str, Any]] = []
    for row in scheduler_runs:
        if str(row.get("mode") or "") != "postclose":
            continue
        scheduled = _parse_utc(row.get("scheduled_at_utc"))
        if scheduled is not None and scheduled.astimezone(CHINA_TZ).date() == today:
            today_runs.append(row)
    latest = today_runs[0] if today_runs else None
    if latest is None:
        return {
            "status": "idle",
            "message": "等待今日数据更新",
            "decision_date": None,
            "scheduler_run_id": None,
            "started_at_utc": None,
            "completed_at_utc": None,
            "error_text": None,
        }
    status = str(latest.get("status") or "unknown")
    if status == "running":
        state, message = "updating", "正在更新今日数据"
    elif status in {"completed", "no_new_data"}:
        state, message = "completed", "今日数据更新完成"
    elif status == "failed":
        state, message = "failed", "今日数据更新失败"
    else:
        state, message = status, "今日数据更新状态未知"
    return {
        "status": state,
        "message": message,
        "decision_date": latest.get("decision_date"),
        "scheduler_run_id": latest.get("run_id"),
        "started_at_utc": latest.get("started_at_utc"),
        "completed_at_utc": latest.get("completed_at_utc"),
        "error_text": latest.get("error_text"),
    }


def _today_pnl(
    portfolio: Mapping[str, Any],
    *,
    decision_date: str | None,
) -> dict[str, Any]:
    equity = portfolio.get("equity")
    trades = portfolio.get("trades")
    equity_rows = [row for row in equity if isinstance(row, Mapping)] if isinstance(equity, list) else []
    trade_rows = [row for row in trades if isinstance(row, Mapping)] if isinstance(trades, list) else []
    latest_date = decision_date
    if equity_rows:
        latest_date = str(equity_rows[-1].get("trade_date") or latest_date or "")
    closed = [
        row
        for row in trade_rows
        if str(row.get("exit_date") or "") == latest_date
    ]
    profits = [
        float(row.get("pnl") or 0.0)
        for row in closed
        if float(row.get("pnl") or 0.0) > 0
    ]
    losses = [
        -float(row.get("pnl") or 0.0)
        for row in closed
        if float(row.get("pnl") or 0.0) < 0
    ]
    gross_profit = sum(profits)
    gross_loss = sum(losses)
    ratio = gross_profit / gross_loss if gross_loss > 0 else None
    equity_return = None
    if len(equity_rows) >= 2:
        previous = float(equity_rows[-2].get("total_assets") or 0.0)
        current = float(equity_rows[-1].get("total_assets") or 0.0)
        if previous > 0:
            equity_return = current / previous - 1.0
    return {
        "trade_date": latest_date or None,
        "profit_loss_ratio": ratio,
        "today_pnl": round(sum(float(row.get("pnl") or 0.0) for row in closed), 6),
        "gross_profit": round(gross_profit, 6),
        "gross_loss": round(gross_loss, 6),
        "win_count": len(profits),
        "loss_count": len(losses),
        "closed_trade_count": len(closed),
        "equity_change_pct": equity_return,
    }


def _strategy_history(
    daily_runs: list[dict[str, Any]],
    *,
    limit: int = 365,
) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    for run in daily_runs:
        manifest_path = str(run.get("manifest_path") or "")
        if not manifest_path:
            continue
        manifest = _read_json_stable(manifest_path)
        if not manifest:
            continue
        strategy = manifest.get("strategy") if isinstance(manifest.get("strategy"), Mapping) else {}
        portfolio = manifest.get("portfolio") if isinstance(manifest.get("portfolio"), Mapping) else {}
        metrics = portfolio.get("metrics") if isinstance(portfolio.get("metrics"), Mapping) else {}
        portfolio_artifact = {}
        if portfolio.get("output_path"):
            portfolio_artifact = _read_json_stable(str(portfolio["output_path"]))
        decision_date = str(manifest.get("decision_date") or run.get("decision_date") or "")
        if not decision_date or decision_date < STRATEGY_HISTORY_START_DATE:
            continue
        if decision_date in seen_dates:
            continue
        seen_dates.add(decision_date)
        history.append(
            {
                "run_id": run.get("run_id"),
                "decision_date": decision_date or None,
                "status": run.get("status"),
                "completed_at_utc": run.get("completed_at_utc"),
                "report_path": run.get("report_path"),
                "picks": list(strategy.get("picks") or []),
                "selection_rule": strategy.get("selection_rule"),
                "trade_plan": dict(strategy.get("trade_plan") or {}),
                "scored_rows": strategy.get("scored_rows"),
                "metrics": dict(metrics),
                "today_pnl": _today_pnl(portfolio_artifact, decision_date=decision_date or None),
            }
        )
        if len(history) >= int(limit):
            break
    return history


def _scheduled_tasks() -> list[dict[str, Any]]:
    script = r"""
$items = foreach ($task in Get-ScheduledTask -TaskPath '\Kinace\' -ErrorAction SilentlyContinue) {
  $info = Get-ScheduledTaskInfo -TaskPath $task.TaskPath -TaskName $task.TaskName
  [pscustomobject]@{
    name = $task.TaskName
    state = [string]$task.State
    last_run = if ($info.LastRunTime) { $info.LastRunTime.ToString('o') } else { $null }
    last_result = $info.LastTaskResult
    next_run = if ($info.NextRunTime) { $info.NextRunTime.ToString('o') } else { $null }
    missed_runs = $info.NumberOfMissedRuns
  }
}
$items | ConvertTo-Json -Compress -Depth 4
""".strip()
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        decoded = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(decoded, dict):
        return [decoded]
    return decoded if isinstance(decoded, list) else []


def collect_state(
    *,
    project_root: str | Path = PROJECT_ROOT,
    database: str | Path = DEFAULT_DATABASE_PATH,
    data_root: str | Path = Path(r"D:\kinacesqlv3.2"),
) -> dict[str, Any]:
    root = Path(project_root)
    data = Path(data_root)
    daily_runs = _database_rows(
        database,
        "daily_runs",
        order_by="completed_at_utc",
        limit=500,
    )
    scheduler_runs = _database_rows(
        database,
        "scheduler_runs",
        order_by="scheduled_at_utc",
        limit=50,
    )
    latest_daily = daily_runs[0] if daily_runs else {}
    manifest = _read_json(Path(str(latest_daily.get("manifest_path") or "")))
    portfolio = manifest.get("portfolio") if isinstance(manifest, Mapping) else None
    metrics = portfolio.get("metrics") if isinstance(portfolio, Mapping) else {}
    strategy = manifest.get("strategy") if isinstance(manifest, Mapping) else {}
    portfolio_artifact = {}
    if isinstance(portfolio, Mapping) and portfolio.get("output_path"):
        portfolio_artifact = _read_json_stable(str(portfolio["output_path"]))
    strategy_history = _strategy_history(daily_runs)
    portal_sources = _latest_portal_sources(root)
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "system": {
            "name": "Kinace V3.5",
            "version": __version__,
            "read_only": READ_ONLY,
            "tushare_token_configured": bool(os.environ.get("TUSHARE_TOKEN")),
            "database": str(database),
            "project_root": str(root),
        },
        "gate": portal_sources["gate"],
        "audit": portal_sources["audit"],
        "parity": _read_json(data / "parity" / "latest.json"),
        "research": portal_sources["research"],
        "sources": portal_sources["sources"],
        "daily_runs": daily_runs,
        "scheduler_runs": scheduler_runs,
        "update": _today_update_state(scheduler_runs),
        "strategy_history": strategy_history,
        "strategy_history_start_date": STRATEGY_HISTORY_START_DATE,
        "latest_run": {
            "daily": latest_daily,
            "manifest": manifest,
            "metrics": metrics,
            "strategy": strategy,
            "portfolio": portfolio_artifact,
            "today_pnl": _today_pnl(
                portfolio_artifact,
                decision_date=str(latest_daily.get("decision_date") or ""),
            ),
        },
        "scheduled_tasks": _scheduled_tasks(),
    }


def _asset(path: str, *, binary: bool = False) -> bytes | str:
    resource = resources.files("kinace_v3.portal_assets").joinpath(path)
    return resource.read_bytes() if binary else resource.read_text(encoding="utf-8")


class _PortalHandler(BaseHTTPRequestHandler):
    server_version = "KinacePortal/1"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Mapping[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/daily-report":
            query = parse_qs(parsed.query)
            run_id = str((query.get("run_id") or [""])[0])
            target = _daily_report_path(
                self.server.database,  # type: ignore[attr-defined]
                self.server.project_root,  # type: ignore[attr-defined]
                run_id,
            )
            if target is None:
                self._json({"error": "report_not_found"}, status=404)
                return
            try:
                markdown = target.read_text(encoding="utf-8")
            except OSError:
                self._json({"error": "report_unavailable"}, status=404)
                return
            if (query.get("format") or [""])[0] == "html":
                self._send(
                    200,
                    _report_html(markdown, run_id=run_id).encode("utf-8"),
                    "text/html; charset=utf-8",
                )
            else:
                self._json({"run_id": run_id, "report_path": str(target), "markdown": markdown})
            return
        if path == "/api/status":
            payload = collect_state(
                project_root=self.server.project_root,  # type: ignore[attr-defined]
                database=self.server.database,  # type: ignore[attr-defined]
                data_root=self.server.data_root,  # type: ignore[attr-defined]
            )
            provider = getattr(self.server, "state_provider", None)
            if callable(provider):
                override = provider()
                if isinstance(override, Mapping) and override:
                    payload["update"] = dict(override)
            self._json(payload)
            return
        if path in {"/", "/index.html"}:
            self._send(200, _asset("index.html").encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/assets/styles.css":
            self._send(200, _asset("styles.css").encode("utf-8"), "text/css; charset=utf-8")
            return
        if path == "/assets/app.js":
            self._send(
                200,
                _asset("app.js").encode("utf-8"),
                "application/javascript; charset=utf-8",
            )
            return
        self._json({"error": "not_found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        self._json({"error": "read_only_portal"}, status=405)

    def do_PUT(self) -> None:  # noqa: N802
        self._json({"error": "read_only_portal"}, status=405)

    def do_DELETE(self) -> None:  # noqa: N802
        self._json({"error": "read_only_portal"}, status=405)

    def log_message(self, format: str, *args: Any) -> None:
        return


class PortalServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        *,
        project_root: str | Path,
        database: str | Path,
        data_root: str | Path,
        state_provider: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        super().__init__(address, _PortalHandler)
        self.project_root = str(project_root)
        self.database = str(database)
        self.data_root = str(data_root)
        self.state_provider = state_provider


def create_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    project_root: str | Path = PROJECT_ROOT,
    database: str | Path = DEFAULT_DATABASE_PATH,
    data_root: str | Path = Path(r"D:\kinacesqlv3.2"),
    state_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> PortalServer:
    return PortalServer(
        (host, int(port)),
        project_root=project_root,
        database=database,
        data_root=data_root,
        state_provider=state_provider,
    )


def serve(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    project_root: str | Path = PROJECT_ROOT,
    database: str | Path = DEFAULT_DATABASE_PATH,
    data_root: str | Path = Path(r"D:\kinacesqlv3.2"),
) -> None:
    server = create_server(
        host=host,
        port=port,
        project_root=project_root,
        database=database,
        data_root=data_root,
    )
    server.serve_forever()


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "READ_ONLY",
    "CHINA_TZ",
    "STRATEGY_HISTORY_START_DATE",
    "PortalServer",
    "collect_state",
    "create_server",
    "serve",
]
