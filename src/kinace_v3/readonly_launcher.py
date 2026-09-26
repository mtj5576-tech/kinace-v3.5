"""One-click launcher for the additional Kinace V3.5 read-only portal."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import DEFAULT_DATABASE_PATH, PROJECT_ROOT
from .launcher import (
    _load_user_environment,
    _run_daily_update,
    _set_update_state,
    _update_state_snapshot,
    _utc_now,
)
from .portal import DEFAULT_HOST
from .portal_readonly import DEFAULT_READONLY_PORT, ReadOnlyPortalServer, create_readonly_server


def application_root() -> Path:
    environment = os.environ.get("KINACE_V32_ROOT")
    candidates: list[Path] = []
    if environment:
        candidates.append(Path(environment).expanduser().resolve(strict=False))
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates.extend([executable_dir, executable_dir.parent, executable_dir.parent.parent])
    candidates.extend([Path.cwd(), PROJECT_ROOT])
    for candidate in candidates:
        if (candidate / "src" / "kinace_v3").is_dir():
            return candidate
    for candidate in candidates:
        if (candidate / "reports" / "scheduler" / "latest-status.json").is_file():
            return candidate
    return Path.cwd()


def latest_readonly_database(root: str | Path) -> Path:
    candidate = (
        Path(root)
        / "outputs"
        / "reconstruction"
        / "kinace_v3_2.sqlite3"
    ).resolve(strict=False)
    return candidate if candidate.is_file() else DEFAULT_DATABASE_PATH


def _available_server(
    *,
    host: str,
    port: int,
    project_root: Path,
    database: str | Path,
    data_root: str | Path,
    state_provider: Callable[[], Mapping[str, Any]] | None = None,
    refresh_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> ReadOnlyPortalServer:
    last_error: Exception | None = None
    for candidate in range(int(port), int(port) + 20):
        try:
            return create_readonly_server(
                host=host,
                port=candidate,
                project_root=project_root,
                database=database,
                data_root=data_root,
                state_provider=state_provider,
                refresh_provider=refresh_provider,
            )
        except OSError as exc:
            last_error = exc
    raise RuntimeError(f"no read-only portal port is available from {port}") from last_error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_READONLY_PORT)
    parser.add_argument(
        "--database",
        help="database override; defaults to the latest reconstruction when present",
    )
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
    database = (
        Path(args.database).expanduser().resolve(strict=False)
        if args.database
        else latest_readonly_database(root)
    )
    os.environ["KINACE_V32_ROOT"] = str(root)
    os.environ["KINACE_V32_CONFIG"] = str(root / "config" / "config.v3.json")

    state_lock = threading.Lock()
    refresh_lock = threading.Lock()
    refresh_worker: dict[str, threading.Thread | None] = {"thread": None}
    update_state: dict[str, Any] = {
        "status": "idle",
        "message": "等待手动刷新",
        "started_at_utc": None,
        "completed_at_utc": None,
        "decision_date": None,
        "scheduler_run_id": None,
        "error_text": None,
    }

    def state_provider() -> Mapping[str, Any]:
        snapshot = _update_state_snapshot(update_state, state_lock)
        worker = refresh_worker["thread"]
        if worker is None or (not worker.is_alive() and snapshot.get("status") == "completed"):
            return {}
        return snapshot if snapshot.get("status") != "idle" else {}

    def start_refresh() -> Mapping[str, Any]:
        with refresh_lock:
            worker = refresh_worker["thread"]
            if worker is not None and worker.is_alive():
                return _update_state_snapshot(update_state, state_lock)
            _set_update_state(
                update_state,
                state_lock,
                status="updating",
                message="手动刷新已启动",
                started_at_utc=_utc_now(),
                completed_at_utc=None,
                error_text=None,
            )
            worker = threading.Thread(
                target=_run_daily_update,
                kwargs={
                    "project_root": root,
                    "database": database,
                    "state": update_state,
                    "lock": state_lock,
                    "latest_only": True,
                    "backup": False,
                },
                name="kinace-readonly-manual-refresh",
                daemon=True,
            )
            refresh_worker["thread"] = worker
            worker.start()
            return _update_state_snapshot(update_state, state_lock)

    server = _available_server(
        host=args.host,
        port=args.port,
        project_root=root,
        database=database,
        data_root=args.data_root,
        state_provider=state_provider,
        refresh_provider=start_refresh,
    )
    actual_port = int(server.server_address[1])
    url = f"http://{args.host}:{actual_port}/"
    url_path = root / "reports" / "scheduler" / "readonly-portal-url.txt"
    url_path.parent.mkdir(parents=True, exist_ok=True)
    url_path.write_text(url + "\n", encoding="utf-8")
    print(f"Kinace V3.5 read-only portal: {url}")
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
