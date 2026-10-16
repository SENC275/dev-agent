"""Loopback-only local dashboard, using existing CLI and journal formats."""

import json
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

FIELDS = "ticket_id,state,current_step,updated_at,worktree_path,branch,error"


def runs(repository: Path) -> list[dict[str, Any]]:
    path = repository / ".dev-agent/state.sqlite3"
    if not path.exists():
        return []
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Symlink journals are not supported")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            f"SELECT {FIELDS} FROM runs ORDER BY updated_at DESC"
        )]


class Dashboard:
    def __init__(self, projects: list[Path]):
        self.projects = list(dict.fromkeys(p.resolve(strict=True) for p in projects))
        self.token = secrets.token_urlsafe(32)
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def project(self, key: str) -> Path:
        if not key.isdecimal() or int(key) >= len(self.projects):
            raise ValueError("Unknown project")
        return self.projects[int(key)]

    def overview(self) -> dict[str, Any]:
        projects = []
        for index, path in enumerate(self.projects):
            error = None
            try:
                tickets = runs(path)
            except (ValueError, sqlite3.Error, OSError) as exc:
                tickets, error = [], str(exc)
            projects.append({"id": str(index), "name": path.name, "path": str(path),
                             "tickets": tickets, "error": error})
        with self.lock:
            jobs = {key: dict(value, elapsed=round(time.monotonic() - value["started"], 1)
                              if value["running"] else value["elapsed"])
                    for key, value in self.jobs.items()}
        return {"projects": projects, "jobs": jobs}

    def detail(self, key: str, ticket: str) -> dict[str, Any]:
        path = self.project(key)
        if ticket not in {r["ticket_id"] for r in runs(path)}:
            raise ValueError("Unknown ticket")
        result = subprocess.run(
            [sys.executable, "-m", "dev_agent.cli", "summary", ticket, "--json"],
            cwd=path, capture_output=True, text=True, timeout=60,
        )
        if result.returncode:
            raise ValueError(result.stdout[-4000:] or result.stderr[-4000:])
        report = json.loads(result.stdout)
        database = path / ".dev-agent/state.sqlite3"
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            report["steps"] = [dict(row) for row in db.execute(
                "SELECT name,status,started_at,finished_at,steps.error FROM steps "
                "JOIN runs USING(run_id) WHERE ticket_id=? ORDER BY id", (ticket,)
            )]
        return dict(report)

    def test(self, key: str) -> None:
        path = self.project(key)
        from dev_agent.config import CONFIG_NAME, load_config
        config = load_config(path / CONFIG_NAME)
        with self.lock:
            if self.jobs.get(key, {}).get("running"):
                raise ValueError("A dashboard test is already running for this project")
            self.jobs[key] = {"running": True, "started": time.monotonic(), "elapsed": 0,
                              "output": "Starting configured test…", "exit_code": None,
                              "command": config.commands.test}
        threading.Thread(
            target=self._test,
            args=(key, path, config.commands.test, config.validation.timeout_seconds),
            daemon=False,
        ).start()

    def _test(self, key: str, path: Path, command: str, timeout: float) -> None:
        import asyncio
        import shlex

        from dev_agent.process import run_process
        code = 1
        try:
            result = asyncio.run(run_process(shlex.split(command), cwd=path, timeout=timeout))
            code = 124 if result.timed_out else result.exit_code
            output = result.stdout + result.stderr
            if result.timed_out:
                output += "\nTest timed out."
        except Exception as exc:
            output = str(exc)
        with self.lock:
            job = self.jobs[key]
            job.update(running=False, exit_code=code, output=output[-64000:],
                       elapsed=round(time.monotonic() - job["started"], 1))


def handler(dashboard: Dashboard) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def respond(self, status: int, data: Any, html: bool = False) -> None:
            payload = data.encode() if html else json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html
                             else "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; "
                             "script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "frame-ancestors 'none'; base-uri 'none'")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def allowed(self) -> bool:
            port = cast(ThreadingHTTPServer, self.server).server_port
            return self.headers.get("Host") == f"127.0.0.1:{port}"

        def do_GET(self) -> None:
            if not self.allowed():
                self.respond(403, {"error": "Invalid host"})
                return
            url = urlsplit(self.path)
            if url.path == "/":
                page = Path(__file__).with_name("dashboard.html").read_text()
                self.respond(200, page, html=True)
                return
            if not secrets.compare_digest(self.headers.get("X-Dashboard-Token", ""),
                                          dashboard.token):
                self.respond(403, {"error": "Open the URL printed by dev-agent dashboard"})
                return
            try:
                if url.path == "/api/projects":
                    self.respond(200, dashboard.overview())
                elif url.path == "/api/ticket":
                    query = parse_qs(url.query)
                    self.respond(200, dashboard.detail(query["project"][0], query["ticket"][0]))
                else:
                    self.respond(404, {"error": "Not found"})
            except (ValueError, KeyError, OSError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
                self.respond(400, {"error": str(exc)})

        def do_POST(self) -> None:
            origin = f"http://127.0.0.1:{cast(ThreadingHTTPServer, self.server).server_port}"
            if (not self.allowed() or self.headers.get("Origin") != origin
                    or not secrets.compare_digest(self.headers.get("X-Dashboard-Token", ""),
                                                  dashboard.token)):
                self.respond(403, {"error": "Invalid origin or token"})
                return
            try:
                if self.path != "/api/test":
                    self.respond(404, {"error": "Not found"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    raise ValueError("Invalid request size")
                data = json.loads(self.rfile.read(length))
                dashboard.test(str(data["project"]))
                self.respond(202, {"started": True})
            except (ValueError, KeyError, TypeError, OSError) as exc:
                self.respond(400, {"error": str(exc)})

    return Handler


def serve(projects: list[Path], port: int, open_browser: bool) -> None:
    dashboard = Dashboard(projects)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler(dashboard))
    url = f"http://127.0.0.1:{server.server_port}/#{dashboard.token}"
    print(f"Local dashboard: {url}\nCtrl+C to stop.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if any(job["running"] for job in dashboard.overview()["jobs"].values()):
            print("Waiting for configured tests to finish or time out…", flush=True)
