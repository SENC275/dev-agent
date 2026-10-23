"""Loopback-only local dashboard, using existing CLI and journal formats."""

import asyncio
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
from uuid import uuid4

from dev_agent.config import ConfigurationError
from dev_agent.context import safe, save
from dev_agent.dashboard_actions import ACTIONS, PREVIEWS, draft_path, preview, ticket_id
from dev_agent.local import protect_local_files

FIELDS = "ticket_id,state,current_step,updated_at,worktree_path,branch,error"


def runs(repository: Path) -> list[dict[str, Any]]:
    path = repository / ".dev-agent/state.sqlite3"
    if not path.exists():
        return []
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Symlink journals are not supported")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(row) for row in db.execute(f"SELECT {FIELDS} FROM runs ORDER BY updated_at DESC")
        ]


class Dashboard:
    def __init__(self, projects: list[Path]):
        self.projects = list(dict.fromkeys(p.resolve(strict=True) for p in projects))
        self.token = secrets.token_urlsafe(32)
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.previews: dict[str, dict[str, Any]] = {}
        for index, root in enumerate(self.projects):
            folder = root / ".dev-agent/dashboard/jobs"
            if safe(folder) and folder.is_dir():
                attempts = sorted(p for p in folder.iterdir() if safe(p) and p.is_dir())
                if attempts:
                    self.jobs[str(index)] = self.read_job(attempts[-1], recovering=True)

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
            drafts = []
            folder = path / ".dev-agent/tickets"
            if safe(folder) and folder.is_dir():
                drafts = [p.stem for p in sorted(folder.glob("*.md")) if safe(p)]
            projects.append(
                {
                    "drafts": drafts,
                    "id": str(index),
                    "name": path.name,
                    "path": str(path),
                    "tickets": tickets,
                    "error": error,
                }
            )
        with self.lock:
            for key, job in list(self.jobs.items()):
                if job.get("directory"):
                    self.jobs[key] = self.read_job(
                        Path(job["directory"]), recovering=job.get("recovered", False)
                    )
            jobs = {
                key: dict(
                    value,
                    elapsed=round(time.monotonic() - value["started"], 1)
                    if value["running"]
                    else value["elapsed"],
                )
                for key, value in self.jobs.items()
            }
        return {"projects": projects, "jobs": jobs}

    def read_job(self, directory: Path, *, recovering: bool = False) -> dict[str, Any]:
        meta = directory / "job.json"
        if not safe(meta):
            raise ValueError("Unsafe job record")
        data = json.loads(meta.read_text())
        receipt = directory / "result.json"
        if safe(receipt) and receipt.exists():
            result = json.loads(receipt.read_text())
            data.update(
                running=False,
                status=result["status"],
                result=result.get("result"),
                exit_code=0 if result["status"] == "COMPLETE" else 1,
                error=result.get("error"),
            )
        elif recovering:
            data.update(
                running=False,
                status="UNKNOWN",
                recovered=True,
                error="Server restarted. Inspect status before resuming; no automatic replay.",
            )
        log = directory / "output.log"
        if safe(log) and log.exists():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 64000))
                data["output"] = stream.read().decode("utf-8", errors="replace")
        data["directory"] = str(directory)
        data.setdefault("elapsed", 0)
        return dict(data)

    def make_preview(self, key: str, ticket: str, action: str) -> dict[str, Any]:
        root = self.project(key)
        if action not in PREVIEWS:
            raise ValueError("Unsupported preview")
        if action != "configure":
            ticket_id(ticket)
        value = asyncio.run(preview(root, ticket, action))
        if len(json.dumps(value).encode()) > 1_000_000:
            raise ValueError("Preview is too large for the dashboard; inspect with CLI.")
        nonce = secrets.token_urlsafe(24)
        with self.lock:
            self.previews = {
                k: v for k, v in self.previews.items() if time.monotonic() - v["created"] < 900
            }
            if len(self.previews) >= 128:
                raise ValueError("Too many pending previews; retry later.")
            self.previews[nonce] = dict(
                project=key, ticket=ticket, action=action, accepted=value, created=time.monotonic()
            )
        return {**value, "confirmation": nonce}

    def action(
        self, key: str, ticket: str, action: str, payload: dict[str, Any], confirmation: str = ""
    ) -> str:
        root = self.project(key)
        if action not in ACTIONS or not isinstance(payload, dict):
            raise ValueError("Unsupported action")
        if action not in {"init", "doctor", "configure", "rag-index"}:
            ticket_id(ticket)
        with self.lock:
            if self.jobs.get(key, {}).get("running"):
                raise ValueError("An action is already running for this project.")
            accepted = None
            if action in PREVIEWS:
                value = self.previews.get(confirmation)
                if (
                    not value
                    or value["project"] != key
                    or value["ticket"] != ticket
                    or value["action"] != action
                    or time.monotonic() - value["created"] > 900
                ):
                    raise ValueError("Preview expired or mismatched; reload before confirming.")
                accepted = self.previews.pop(confirmation)["accepted"]
            protect_local_files(root)
            from dev_agent.workflow.persistence import now

            identifier = now().replace(":", "-") + "-" + uuid4().hex[:8]
            folder = root / ".dev-agent/dashboard/jobs" / identifier
            if not safe(folder):
                raise ValueError("Unsafe dashboard state directory")
            folder.mkdir(parents=True)
            request = {
                "repository": str(root),
                "ticket": ticket,
                "action": action,
                "payload": payload,
                "accepted": accepted,
            }
            save(folder / "request.json", request)
            data = dict(
                id=identifier,
                action=action,
                ticket=ticket,
                running=True,
                status="RUNNING",
                started=time.monotonic(),
                elapsed=0,
                output="",
                exit_code=None,
                command=action,
                directory=str(folder),
            )
            save(folder / "job.json", data)
            self.jobs[key] = data
            threading.Thread(target=self._action, args=(key, folder), daemon=False).start()
        return identifier

    def _action(self, key: str, folder: Path) -> None:
        try:
            with (folder / "output.log").open("w") as output:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-u",
                        "-m",
                        "dev_agent.dashboard_actions",
                        str(folder / "request.json"),
                    ],
                    cwd=self.project(key),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
            if not (folder / "result.json").exists():
                save(
                    folder / "result.json",
                    {
                        "status": "FAILED",
                        "error": f"Worker exited {result.returncode}; inspect before retrying.",
                    },
                )
        except OSError as exc:
            save(folder / "result.json", {"status": "FAILED", "error": str(exc)})
        with self.lock:
            data = json.loads((folder / "job.json").read_text())
            data.update(running=False, elapsed=round(time.monotonic() - data["started"], 1))
            save(folder / "job.json", data)
            if self.jobs.get(key, {}).get("id") == data["id"]:
                self.jobs[key] = self.read_job(folder)

    def detail(self, key: str, ticket: str) -> dict[str, Any]:
        path = self.project(key)
        if ticket not in {r["ticket_id"] for r in runs(path)}:
            raise ValueError("Unknown ticket")
        result = subprocess.run(
            [sys.executable, "-m", "dev_agent.cli", "summary", ticket, "--json"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode:
            raise ValueError(result.stdout[-4000:] or result.stderr[-4000:])
        report = json.loads(result.stdout)
        database = path / ".dev-agent/state.sqlite3"
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            report["steps"] = [
                dict(row)
                for row in db.execute(
                    "SELECT name,status,started_at,finished_at,steps.error FROM steps "
                    "JOIN runs USING(run_id) WHERE ticket_id=? ORDER BY id",
                    (ticket,),
                )
            ]
        return dict(report)

    def test(self, key: str) -> None:
        path = self.project(key)
        from dev_agent.config import CONFIG_NAME, load_config

        config = load_config(path / CONFIG_NAME)
        with self.lock:
            if self.jobs.get(key, {}).get("running"):
                raise ValueError("A dashboard test is already running for this project")
            self.jobs[key] = {
                "running": True,
                "started": time.monotonic(),
                "elapsed": 0,
                "output": "Starting configured test…",
                "exit_code": None,
                "command": config.commands.test,
            }
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
            job.update(
                running=False,
                exit_code=code,
                output=output[-64000:],
                elapsed=round(time.monotonic() - job["started"], 1),
            )


def handler(dashboard: Dashboard) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def respond(self, status: int, data: Any, html: bool = False) -> None:
            payload = data.encode() if html else json.dumps(data).encode()
            self.send_response(status)
            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8" if html else "application/json; charset=utf-8",
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; "
                "script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "frame-ancestors 'none'; base-uri 'none'",
            )
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
            if not secrets.compare_digest(
                self.headers.get("X-Dashboard-Token", ""), dashboard.token
            ):
                self.respond(403, {"error": "Open the URL printed by dev-agent dashboard"})
                return
            try:
                if url.path == "/api/projects":
                    self.respond(200, dashboard.overview())
                elif url.path == "/api/ticket":
                    query = parse_qs(url.query)
                    self.respond(200, dashboard.detail(query["project"][0], query["ticket"][0]))
                elif url.path == "/api/diff":
                    from dev_agent.dashboard_actions import current_diff

                    query = parse_qs(url.query)
                    self.respond(
                        200,
                        asyncio.run(
                            current_diff(dashboard.project(query["project"][0]), query["ticket"][0])
                        ),
                    )
                elif url.path == "/api/jobs":
                    query = parse_qs(url.query)
                    folder = dashboard.project(query["project"][0]) / ".dev-agent/dashboard/jobs"
                    jobs = []
                    if safe(folder) and folder.is_dir():
                        for path in sorted(folder.iterdir(), reverse=True)[:20]:
                            if safe(path) and path.is_dir():
                                current = dashboard.jobs.get(query["project"][0], {})
                                active = current.get("running") and current.get("directory") == str(
                                    path
                                )
                                jobs.append(dashboard.read_job(path, recovering=not active))
                    self.respond(200, jobs)
                elif url.path == "/api/draft":
                    query = parse_qs(url.query)
                    path = draft_path(dashboard.project(query["project"][0]), query["ticket"][0])
                    self.respond(200, {"text": path.read_text()})
                elif url.path == "/api/rag":
                    from dev_agent.config import CONFIG_NAME, load_config
                    from dev_agent.rag import search

                    query = parse_qs(url.query)
                    root = dashboard.project(query["project"][0])
                    self.respond(
                        200,
                        asyncio.run(
                            search(root, query["q"][0][:4000], load_config(root / CONFIG_NAME).rag)
                        ),
                    )
                else:
                    self.respond(404, {"error": "Not found"})
            except (
                ValueError,
                KeyError,
                OSError,
                sqlite3.Error,
                subprocess.TimeoutExpired,
                ConfigurationError,
            ) as exc:
                self.respond(400, {"error": str(exc)})

        def do_POST(self) -> None:
            origin = f"http://127.0.0.1:{cast(ThreadingHTTPServer, self.server).server_port}"
            if (
                not self.allowed()
                or self.headers.get("Origin") != origin
                or not secrets.compare_digest(
                    self.headers.get("X-Dashboard-Token", ""), dashboard.token
                )
            ):
                self.respond(403, {"error": "Invalid origin or token"})
                return
            try:
                if self.path not in {"/api/test", "/api/preview", "/api/action"}:
                    self.respond(404, {"error": "Not found"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= (1024 if self.path == "/api/test" else 96000):
                    raise ValueError("Invalid request size")
                data = json.loads(self.rfile.read(length))
                if self.path == "/api/preview":
                    self.respond(
                        200,
                        dashboard.make_preview(
                            str(data["project"]), data.get("ticket", ""), data["action"]
                        ),
                    )
                elif self.path == "/api/action":
                    job = dashboard.action(
                        str(data["project"]),
                        data.get("ticket", ""),
                        data["action"],
                        data.get("payload", {}),
                        data.get("confirmation", ""),
                    )
                    self.respond(202, {"job": job})
                else:
                    dashboard.test(str(data["project"]))
                    self.respond(202, {"started": True})
            except (ValueError, KeyError, TypeError, OSError, ConfigurationError) as exc:
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
            print("Waiting for active jobs to finish or time out…", flush=True)
