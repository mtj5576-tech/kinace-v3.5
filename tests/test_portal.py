from __future__ import annotations

import json
import sqlite3
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kinace_v3.portal import _strategy_history, collect_state, create_server
from kinace_v3.portal_readonly import create_readonly_server


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _seed_portal_files(tmp_path):
    root = tmp_path / "project"
    data_root = tmp_path / "data"
    database = tmp_path / "v32.sqlite3"
    run_dir = root / "outputs" / "daily" / "run-1"
    portfolio_path = run_dir / "portfolio.json"
    manifest_path = run_dir / "daily-run.json"
    report_path = root / "reports" / "daily" / "run-1.md"
    manifest = {
        "run_id": "run-1",
        "decision_date": "2026-09-16",
        "portfolio": {
            "metrics": {"total_return": 0.1, "max_drawdown": 0.2},
            "output_path": str(portfolio_path),
        },
        "strategy": {
            "picks": [
                {
                    "code": "000001",
                    "score": 0.8,
                    "adjusted_score": 0.7,
                    "reference_price": 10.0,
                    "atr": 0.5,
                    "stop_loss": 9.0,
                    "take_profit": 11.5,
                }
            ],
            "selection_rule": "test",
            "trade_plan": {
                "price_basis": "decision_close",
                "atr_period": 14,
                "stop_atr_multiple": 2.0,
                "target_atr_multiple": 3.0,
            },
        },
    }
    _write_json(
        portfolio_path,
        {
            "equity": [
                {"trade_date": "2026-09-15", "total_assets": 100.0},
                {"trade_date": "2026-09-16", "total_assets": 110.0},
            ],
            "trades": [
                {"exit_date": "2026-09-16", "pnl": 20.0},
                {"exit_date": "2026-09-16", "pnl": -10.0},
            ],
        },
    )
    _write_json(manifest_path, manifest)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("# daily\n", encoding="utf-8")
    _write_json(
        root / "reports" / "scheduler" / "latest-status.json",
        {"ready": False, "unattended_trading_days": 1, "incomplete_lineage": []},
    )
    _write_json(
        root / "reports" / "research_audit_canonical.json",
        {
            "status": "passed",
            "research_eligible": True,
            "live_sync_eligible": True,
            "universe": {"list_date_present": 1},
            "price_continuity": {"severe_jump_count": 0},
            "execution": {"violation_count": 0},
        },
    )
    _write_json(
        root / "reports" / "research_loop_scheduled.json",
        {"status": "no_eligible_challenger", "holdout_status": "holdout_rejected"},
    )
    _write_json(
        data_root / "parity" / "latest.json",
        {"status": "passed", "overlap_records": 10, "mismatch_records": 0},
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE daily_runs (run_id TEXT, decision_date TEXT, status TEXT, "
            "completed_at_utc TEXT, candidate_id TEXT, backtest_run_id TEXT, "
            "replay_id TEXT, snapshot_id TEXT, manifest_path TEXT, report_path TEXT)"
        )
        connection.execute(
            "INSERT INTO daily_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "run-1",
                "2026-09-16",
                "completed",
                "2026-09-16T11:00:00Z",
                "candidate-1",
                "backtest-1",
                "replay-1",
                "snapshot-1",
                str(manifest_path),
                str(report_path),
            ),
        )
        connection.execute(
            "CREATE TABLE scheduler_runs (run_id TEXT, mode TEXT, scheduled_at_utc TEXT, "
            "started_at_utc TEXT, completed_at_utc TEXT, status TEXT, decision_date TEXT, "
            "command_json TEXT, log_path TEXT, error_text TEXT)"
        )
        connection.execute(
            "INSERT INTO scheduler_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "scheduler-1",
                "postclose",
                "2026-09-16T11:00:00Z",
                "2026-09-16T11:00:00Z",
                "2026-09-16T11:05:00Z",
                "completed",
                "2026-09-16",
                "{}",
                "test.log",
                None,
            ),
        )
    return root, database, data_root


def test_collect_state_reads_existing_artifacts(tmp_path):
    root, database, data_root = _seed_portal_files(tmp_path)

    state = collect_state(project_root=root, database=database, data_root=data_root)

    assert state["system"]["read_only"] is True
    assert state["gate"]["unattended_trading_days"] == 1
    assert state["latest_run"]["strategy"]["picks"][0]["code"] == "000001"
    assert state["latest_run"]["strategy"]["picks"][0]["stop_loss"] == 9.0
    assert state["strategy_history"][0]["trade_plan"]["atr_period"] == 14
    assert state["latest_run"]["metrics"]["total_return"] == 0.1
    assert state["latest_run"]["today_pnl"]["profit_loss_ratio"] == 2.0
    assert state["latest_run"]["today_pnl"]["today_pnl"] == 10.0
    assert state["strategy_history"][0]["decision_date"] == "2026-09-16"


def test_strategy_history_starts_on_20260916(tmp_path):
    runs = []
    for decision_date in ("2026-09-15", "2026-09-16"):
        manifest_path = tmp_path / f"{decision_date}.json"
        _write_json(
            manifest_path,
            {
                "decision_date": decision_date,
                "strategy": {"picks": [{"code": decision_date.replace("-", "")}]},
            },
        )
        runs.append(
            {
                "run_id": decision_date,
                "decision_date": decision_date,
                "status": "completed",
                "manifest_path": str(manifest_path),
            }
        )

    history = _strategy_history(runs)

    assert [item["decision_date"] for item in history] == ["2026-09-16"]


def test_collect_state_prefers_latest_reconstruction_artifacts(tmp_path):
    root, database, data_root = _seed_portal_files(tmp_path)
    reconstruction = root / "reports" / "execution_framework" / "reconstruction"
    integration = root / "reports" / "execution_framework" / "integration"
    _write_json(
        reconstruction / "research-audit.json",
        {"status": "passed", "research_eligible": True, "source": "reconstruction"},
    )
    _write_json(
        reconstruction / "research-loop.json",
        {"status": "no_eligible_challenger", "candidate_count": 3, "source": "reconstruction"},
    )
    _write_json(
        integration / "final-delivery-report.json",
        {
            "artifact_type": "kinace_v3_final_delivery_report",
            "status": "awaiting_data_and_human_review",
            "release_ready": False,
            "production_ready": False,
            "production_changed": False,
            "completed": {},
        },
    )

    state = collect_state(project_root=root, database=database, data_root=data_root)

    assert state["audit"]["source"] == "reconstruction"
    assert state["research"]["source"] == "reconstruction"
    assert state["research"]["candidate_count"] == 3
    assert state["gate"]["status"] == "awaiting_data_and_human_review"
    assert state["sources"]["audit"].endswith("research-audit.json")
    assert state["sources"]["research"].endswith("research-loop.json")


def test_portal_is_read_only_and_serves_dashboard(tmp_path):
    root, database, data_root = _seed_portal_files(tmp_path)
    server = create_server(
        host="127.0.0.1",
        port=0,
        project_root=root,
        database=database,
        data_root=data_root,
        state_provider=lambda: {"status": "updating", "message": "正在更新测试数据"},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urlopen(f"{base}/", timeout=5) as response:
            html = response.read().decode("utf-8")
        with urlopen(f"{base}/api/status", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        with urlopen(f"{base}/api/daily-report?run_id=run-1&format=html", timeout=5) as response:
            report_html = response.read().decode("utf-8")
        assert "Read-only Dashboard" in html
        assert payload["system"]["name"] == "Kinace V3.5"
        assert payload["update"]["status"] == "updating"
        assert "Kinace V3.5 Daily Report" in report_html
        assert "# daily" in report_html
        try:
            urlopen(f"{base}/api/daily-report?run_id=unknown", timeout=5)
        except HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("unknown report must return 404")
        try:
            urlopen(Request(f"{base}/api/status", method="POST"), timeout=5)
        except HTTPError as exc:
            assert exc.code == 405
        else:
            raise AssertionError("POST must be rejected")
    finally:
        server.shutdown()
        server.server_close()
def test_additional_readonly_portal_serves_research_ledger(tmp_path):
    root, database, data_root = _seed_portal_files(tmp_path)
    server = create_readonly_server(
        host="127.0.0.1",
        port=0,
        project_root=root,
        database=database,
        data_root=data_root,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urlopen(f"{base}/", timeout=5) as response:
            html = response.read().decode("utf-8")
        with urlopen(f"{base}/assets/styles.css", timeout=5) as response:
            styles = response.read().decode("utf-8")
        with urlopen(f"{base}/assets/app.js", timeout=5) as response:
            script = response.read().decode("utf-8")
        with urlopen(f"{base}/api/status", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert "Read-only Research Ledger" in html
        assert "只读研究账本" in html
        assert "--accent: #1b6558" in styles
        assert "renderPortfolio" in script
        assert "/api/refresh" in script
        assert "正在更新" in script
        assert "更新完成" in script
        assert payload["system"]["read_only"] is True
        assert payload["latest_run"]["strategy"]["picks"][0]["code"] == "000001"
        try:
            urlopen(Request(f"{base}/api/status", method="POST"), timeout=5)
        except HTTPError as exc:
            assert exc.code == 405
        else:
            raise AssertionError("POST must be rejected by the additional portal")
    finally:
        server.shutdown()
        server.server_close()


def test_additional_readonly_portal_starts_manual_refresh(tmp_path):
    root, database, data_root = _seed_portal_files(tmp_path)
    calls: list[str] = []
    server = create_readonly_server(
        host="127.0.0.1",
        port=0,
        project_root=root,
        database=database,
        data_root=data_root,
        refresh_provider=lambda: calls.append("refresh")
        or {"status": "updating", "message": "手动刷新已启动"},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        request = Request(f"{base}/api/refresh", method="POST", data=b"")
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert response.status == 202
        assert calls == ["refresh"]
        assert payload["status"] == "started"
        assert payload["update"]["status"] == "updating"
    finally:
        server.shutdown()
        server.server_close()
