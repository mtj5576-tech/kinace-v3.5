"""Additional read-only research portal for Kinace V3.5."""

from __future__ import annotations

from http.server import ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from .portal import _PortalHandler


DEFAULT_HOST = "127.0.0.1"
DEFAULT_READONLY_PORT = 18766


def _asset(path: str) -> str:
    resource = resources.files("kinace_v3.portal_v33_assets").joinpath(path)
    return resource.read_text(encoding="utf-8")


class _ReadOnlyPortalHandler(_PortalHandler):
    server_version = "KinaceReadOnlyPortal/1"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        try:
            super()._send(status, body, content_type)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
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
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/refresh":
            super().do_POST()
            return
        provider = getattr(self.server, "refresh_provider", None)
        if not callable(provider):
            self._json({"error": "refresh_not_configured"}, status=503)
            return
        try:
            update = dict(provider())
        except Exception as exc:
            self._json(
                {
                    "error": "refresh_start_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                },
                status=500,
            )
            return
        self._json({"status": "started", "update": update}, status=202)


class ReadOnlyPortalServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        *,
        project_root: str | Path,
        database: str | Path,
        data_root: str | Path,
        state_provider: Callable[[], Mapping[str, Any]] | None = None,
        refresh_provider: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        super().__init__(address, _ReadOnlyPortalHandler)
        self.project_root = str(project_root)
        self.database = str(database)
        self.data_root = str(data_root)
        self.state_provider = state_provider
        self.refresh_provider = refresh_provider


def create_readonly_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_READONLY_PORT,
    project_root: str | Path,
    database: str | Path,
    data_root: str | Path,
    state_provider: Callable[[], Mapping[str, Any]] | None = None,
    refresh_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> ReadOnlyPortalServer:
    return ReadOnlyPortalServer(
        (host, int(port)),
        project_root=project_root,
        database=database,
        data_root=data_root,
        state_provider=state_provider,
        refresh_provider=refresh_provider,
    )


def serve_readonly(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_READONLY_PORT,
    project_root: str | Path,
    database: str | Path,
    data_root: str | Path,
) -> None:
    server = create_readonly_server(
        host=host,
        port=port,
        project_root=project_root,
        database=database,
        data_root=data_root,
    )
    server.serve_forever()


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_READONLY_PORT",
    "ReadOnlyPortalServer",
    "create_readonly_server",
    "serve_readonly",
]
