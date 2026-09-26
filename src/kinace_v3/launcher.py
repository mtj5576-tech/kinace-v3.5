"""One-click launcher for the Kinace V3.3 daily run and read-only portal."""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import io
import os
import sqlite3
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import DEFAULT_DATABASE_PATH, PROJECT_ROOT
from .portal import DEFAULT_HOST, DEFAULT_PORT, create_server
from .schedule_runner import CHINA_TZ, SUCCESS_STATUSES, execute_mode


def application_root() -> Path:
    environment = os.environ.get("KINACE_V32_ROOT")
    candidates = []
    if environment:
        candidates.append(Path(environment).expanduser().resolve(strict=False))
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates.extend([executable_dir, executable_dir.parent])
    candidates.extend([Path.cwd(), PROJECT_ROOT])
    for candidate in candidates:
        if (candidate / "src" / "kinace_v3").is_dir():
            return candidate
    for candidate in candidates:
        if (candidate / "reports" / "scheduler" / "latest-status.json").is_file():
            return candidate
    return Path.cwd()


def _load_user_environment() -> None:
    if os.name != "nt" or os.environ.get("TUSHARE_TOKEN"):
        return
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, "TUSHARE_TOKEN")
    except (ImportError, FileNotFoundError, OSError):
        return
    if isinstance(value, str) and value.strip():
        os.environ["TUSHARE_TOKEN"] = value.strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _today_postclose_run(database: str | Path) -> dict[str, Any] | None:
    target = Path(database)
    if not target.is_file():
        return None
    try:
        connection = sqlite3.connect(str(target), timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduler_runs'"
            ).fetchone()
            if exists is None:
                return None
            rows = connection.execute(
                "SELECT * FROM scheduler_runs WHERE mode='postclose' "
                "ORDER BY scheduled_at_utc DESC LIMIT 20"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    today = datetime.now(CHINA_TZ).date()
    for row in rows:
        value = str(row["scheduled_at_utc"] or "")
        try:
            scheduled = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        if scheduled.astimezone(CHINA_TZ).date() == today:
            return dict(row)
    return None


def _set_update_state(
    state: dict[str, Any],
    lock: threading.Lock,
    **changes: Any,
) -> None:
    with lock:
        state.update(changes)


def _update_state_snapshot(
    state: Mapping[str, Any],
    lock: threading.Lock,
) -> dict[str, Any]:
    with lock:
        return dict(state)


def _run_daily_update(
    *,
    project_root: Path,
    database: str | Path,
    state: dict[str, Any],
    lock: threading.Lock,
    force: bool = False,
    latest_only: bool = False,
    backup: bool = True,
    updater: Any = execute_mode,
) -> None:
    try:
        existing = _today_postclose_run(database)
        today = datetime.now(CHINA_TZ).date().isoformat()
        checked_today = bool(
            existing
            and str(existing.get("status") or "") in SUCCESS_STATUSES
            and (
                str(existing.get("decision_date") or "") == today
                or (
                    latest_only
                    and str(existing.get("status") or "") == "no_new_data"
                )
            )
        )
        if (
            not force
            and checked_today
        ):
            _set_update_state(
                state,
                lock,
                status="completed",
                message="今日数据更新完成",
                decision_date=existing.get("decision_date"),
                scheduler_run_id=existing.get("run_id"),
                started_at_utc=existing.get("started_at_utc"),
                completed_at_utc=existing.get("completed_at_utc"),
                error_text=None,
            )
            return
        _set_update_state(
            state,
            lock,
            status="updating",
            message=(
                "正在同步最新交易日数据"
                if latest_only
                else "正在更新市场数据并生成今日策略"
            ),
            started_at_utc=_utc_now(),
            completed_at_utc=None,
            error_text=None,
        )
        result = updater(
            "postclose",
            database=database,
            report_dir=project_root / "reports" / "scheduler",
            runner=_embedded_runner,
            force=force,
            latest_only=latest_only,
            backup=backup,
        )
        status = str(result.get("status") or "completed")
        message = (
            "数据已是最新，无新增交易日"
            if status == "no_new_data"
            else "今日数据更新完成"
        )
        _set_update_state(
            state,
            lock,
            status="completed",
            message=message,
            decision_date=result.get("decision_date"),
            scheduler_run_id=result.get("scheduler_run_id"),
            completed_at_utc=_utc_now(),
            log_path=result.get("log_path"),
            error_text=None,
        )
    except Exception as exc:
        _set_update_state(
            state,
            lock,
            status="failed",
            message="今日数据更新失败",
            completed_at_utc=_utc_now(),
            error_text=f"{type(exc).__name__}: {exc}",
        )


def _embedded_runner(
    arguments: Sequence[str],
    *,
    log_path: Path,
    allowed_exit_codes: Sequence[int] = (0,),
) -> int:
    from .cli import main as cli_main

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"command: {list(arguments)}\n")
        handle.flush()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = int(cli_main(list(arguments)))
        text = output.getvalue()
        handle.write(text)
        if text and not text.endswith("\n"):
            handle.write("\n")
    if code not in {int(item) for item in allowed_exit_codes}:
        detail = text.strip().splitlines()[-8:]
        detail_text = " | ".join(detail)
        raise RuntimeError(
            f"command failed with exit code {code}: {' '.join(arguments)}"
            + (f"; output: {detail_text}" if detail_text else "")
        )
    return code


def _available_server(
    *,
    host: str,
    port: int,
    project_root: Path,
    database: str | Path,
    data_root: str | Path,
    state_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> Any:
    last_error: Exception | None = None
    for candidate in range(int(port), int(port) + 20):
        try:
            return create_server(
                host=host,
                port=candidate,
                project_root=project_root,
                database=database,
                data_root=data_root,
                state_provider=state_provider,
            )
        except OSError as exc:
            last_error = exc
    raise RuntimeError(f"no portal port is available from {port}") from last_error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("daily", "portal-only", "preopen", "postclose", "weekly", "status"),
        default="daily",
        help="daily opens the read-only portal and starts postclose in the background",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--data-root", default=r"D:\kinacesqlv3.2")
    parser.add_argument("--project-root", help="Kinace project root containing reports and outputs")
    parser.add_argument("--no-browser", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _load_user_environment()
    root = (
        Path(args.project_root).expanduser().resolve(strict=False)
        if args.project_root
        else application_root()
    )
    os.environ["KINACE_V32_ROOT"] = str(root)
    os.environ["KINACE_V32_CONFIG"] = str(root / "config" / "config.v3.json")
    state_lock = threading.Lock()
    update_state: dict[str, Any] = {
        "status": "idle",
        "message": "等待今日数据更新",
        "started_at_utc": None,
        "completed_at_utc": None,
        "decision_date": None,
        "scheduler_run_id": None,
        "error_text": None,
    }
    if args.mode == "daily":
        _set_update_state(
            update_state,
            state_lock,
            status="checking",
            message="正在检查今日数据更新状态",
        )
    elif args.mode != "portal-only":
        _set_update_state(
            update_state,
            state_lock,
            status="updating",
            message=f"正在执行 {args.mode}",
            started_at_utc=_utc_now(),
        )

    server = _available_server(
        host=args.host,
        port=args.port,
        project_root=root,
        database=args.database,
        data_root=args.data_root,
        state_provider=(
            None
            if args.mode == "portal-only"
            else lambda: _update_state_snapshot(update_state, state_lock)
        ),
    )

    if args.mode == "daily":
        worker = threading.Thread(
            target=_run_daily_update,
            kwargs={
                "project_root": root,
                "database": args.database,
                "state": update_state,
                "lock": state_lock,
            },
            name="kinace-daily-update",
            daemon=True,
        )
        worker.start()
    elif args.mode != "portal-only":
        scheduler_mode = args.mode
        try:
            result = execute_mode(
                scheduler_mode,
                database=args.database,
                report_dir=root / "reports" / "scheduler",
                runner=_embedded_runner,
            )
            _set_update_state(
                update_state,
                state_lock,
                status="completed",
                message=f"{args.mode} 执行完成",
                decision_date=result.get("decision_date"),
                scheduler_run_id=result.get("scheduler_run_id"),
                completed_at_utc=_utc_now(),
            )
        except Exception as exc:
            _set_update_state(
                update_state,
                state_lock,
                status="failed",
                message=f"{args.mode} 执行失败",
                completed_at_utc=_utc_now(),
                error_text=f"{type(exc).__name__}: {exc}",
            )
            print(f"Kinace run failed: {exc}", file=sys.stderr)
            print("Starting the read-only portal so the failure can be inspected.")
    actual_port = int(server.server_address[1])
    url = f"http://{args.host}:{actual_port}/"
    url_path = root / "reports" / "scheduler" / "portal-url.txt"
    url_path.parent.mkdir(parents=True, exist_ok=True)
    url_path.write_text(url + "\n", encoding="utf-8")
    print(f"Kinace V3.3 portal: {url}")
    print("Read-only portal. Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
