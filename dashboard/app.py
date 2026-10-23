"""Minimal, server-rendered Orchestra dashboard; no browser credentials or state."""
from __future__ import annotations

import html
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs

from dashboard.omcp import LocalApprovalClient, OMCPClient, OMCPError


ACTIVE = {"starting", "running", "accepted", "approval_required", "cancel_requested"}
TERMINAL = {"succeeded", "failed", "cancelled", "rejected", "interrupted"}
TASK_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class RequestBodyError(ValueError):
    def __init__(self, status: HTTPStatus) -> None:
        self.status = status


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def elapsed(when: Any) -> str:
    if not isinstance(when, str):
        return "—"
    try:
        seconds = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(when)).total_seconds()))
    except ValueError:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def timestamp(when: Any) -> str:
    if not isinstance(when, str):
        return "—"
    return esc(when.replace("T", " ").replace("+00:00", " UTC"))


def state_badge(status: Any) -> str:
    label = esc(status or "unknown").replace("_", " ")
    return f'<span class="state state-{esc(status or "unknown")}">{label}</span>'


class Dashboard:
    """A WSGI app whose routes map to explicit OMCP operations only."""

    def __init__(self, client: Any, approval_client: Any | None = None,
                 static_root: Path | None = None) -> None:
        self.client = client
        self.approval_client = approval_client
        self.static_root = static_root or Path(__file__).with_name("static")
        self.csrf_token = secrets.token_urlsafe(32)

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        path = environ.get("PATH_INFO", "/")
        method = environ.get("REQUEST_METHOD", "GET")
        cancel_match = re.fullmatch(r"/tasks/(" + TASK_ID_RE.pattern + r")/cancel", path)
        approval_match = re.fullmatch(r"/tasks/(" + TASK_ID_RE.pattern + r")/approval", path)
        task_match = re.fullmatch(r"/tasks/(" + TASK_ID_RE.pattern + r")", path)
        try:
            if path == "/static/dashboard.css" and method == "GET":
                return self._static(start_response)
            if path == "/" and method == "GET":
                return self._page(start_response, self.home())
            if path == "/new-task" and method == "GET":
                return self._page(start_response, self.new_task())
            if path == "/new-task" and method == "POST":
                return self.submit(environ, start_response)
            if cancel_match and method == "POST":
                return self.cancel(cancel_match.group(1), environ, start_response)
            if approval_match and method == "POST":
                return self.resolve_approval(approval_match.group(1), environ, start_response)
            if task_match and method == "GET":
                return self._page(start_response, self.task_detail(task_match.group(1)))
            return self._page(start_response, self.layout("Not found", "<p>That page does not exist.</p>"), HTTPStatus.NOT_FOUND)
        except RequestBodyError as exc:
            message = "Request body is too large." if exc.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE else "Invalid request body."
            return self._page(start_response, self.layout("Request rejected", f"<p>{message}</p>"), exc.status)
        except OMCPError:
            return self._page(start_response, self.unavailable(), HTTPStatus.SERVICE_UNAVAILABLE)

    def _static(self, start_response: Callable[..., Any]) -> list[bytes]:
        body = (self.static_root / "dashboard.css").read_bytes()
        start_response("200 OK", [("Content-Type", "text/css; charset=utf-8"), ("Cache-Control", "public, max-age=300")])
        return [body]

    def _page(self, start_response: Callable[..., Any], body: str, status: HTTPStatus = HTTPStatus.OK) -> list[bytes]:
        encoded = body.encode("utf-8")
        start_response(f"{status.value} {status.phrase}", [
            ("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(encoded))),
            ("Content-Security-Policy", "default-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
            ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"), ("Cache-Control", "no-store"),
        ])
        return [encoded]

    def layout(self, title: str, content: str, status: str = "") -> str:
        return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)} · Orchestra</title><link rel="stylesheet" href="/static/dashboard.css"></head><body><header><a class="brand" href="/">Orchestra</a>{status}<a class="button" href="/new-task">+ New Task</a></header><main>{content}</main></body></html>'''

    def unavailable(self) -> str:
        return self.layout("Offline", "<section class=\"panel empty\"><h1>Orchestra is offline</h1><p>The dashboard could not reach OMCP. No task action was attempted.</p></section>", state_badge("offline"))

    def home(self) -> str:
        status = self.client.call("bridge_status")
        workers = self.client.call("worker_availability").get("workers", [])
        tasks = self.client.call("list_tasks", {"limit": 20}).get("tasks", [])
        approvals = self.client.call("list_approvals").get("approvals", [])
        current = [task for task in tasks if task.get("status") in ACTIVE]
        recent = [task for task in tasks if task.get("status") in TERMINAL][:10]
        current_by_worker = {task.get("worker"): task for task in current}
        worker_rows = "".join(
            f'<article class="worker"><h3>{esc(worker.get("worker"))}</h3>{state_badge("busy" if worker.get("current_task_id") else "available" if worker.get("available") else "unavailable")}<p>{esc(current_by_worker.get(worker.get("worker"), {}).get("title", ""))}</p></article>'
            for worker in workers
        ) or "<p>No configured workers.</p>"
        approvals_html = (f'<a class="approval" href="/tasks/{esc(approvals[0].get("task_id"))}">{len(approvals)} task{"s" if len(approvals) != 1 else ""} waiting for approval</a>' if approvals else "")
        return self.layout("Dashboard", f'''<section class="intro"><div><h1>Dashboard</h1><p>Task and worker state from Orchestra.</p></div>{approvals_html}</section><section><h2>Workers</h2><div class="workers">{worker_rows}</div></section><section><h2>Active tasks</h2>{self.task_list(current, "No active tasks.")}</section><section><h2>Recent tasks</h2>{self.task_list(recent, "No recent tasks.")}</section>''', state_badge(status.get("status", "offline")))

    def task_list(self, tasks: list[dict[str, Any]], empty: str) -> str:
        if not tasks:
            return f'<div class="panel empty">{esc(empty)}</div>'
        rows = "".join(f'<a class="task" href="/tasks/{esc(task.get("task_id"))}"><div><strong>{esc(task.get("title"))}</strong><small>{esc(task.get("repository_profile", task.get("repository", "—")))} · {esc(task.get("worker", "—"))}</small></div><div>{state_badge(task.get("status"))}<small>{elapsed(task.get("started_at") or task.get("created_at"))}</small></div></a>' for task in tasks)
        return f'<div class="task-list">{rows}</div>'

    def task_detail(self, task_id: str) -> str:
        task = self.client.call("task_status", {"task_id": task_id})
        result = None
        if task.get("status") in TERMINAL:
            result = self.client.call("task_result", {"task_id": task_id})
        auth = task.get("authorization") if isinstance(task.get("authorization"), dict) else {}
        capabilities = task.get("execution_capabilities") if isinstance(task.get("execution_capabilities"), dict) else None
        evidence = "<p>Security evidence unavailable</p>" if not capabilities else "<ul class=\"security\">" + "".join(f"<li>{'✓' if value is False or value == 'read_only' else '•'} {esc(label)}: {esc('denied' if value is False else value)}</li>" for label, value in (("Network", capabilities.get("network")), ("External credentials", capabilities.get("external_credentials")), ("Git metadata", capabilities.get("git_metadata")))) + "</ul>"
        fields = [("Project", task.get("repository_profile", task.get("repository"))), ("Worker", task.get("worker")), ("State", task.get("status")), ("Consequence class", task.get("consequence_class")), ("Authorisation", auth.get("mode")), ("Standing-authorisation ID", auth.get("authorization_id")), ("Created", timestamp(task.get("created_at"))), ("Started", timestamp(task.get("started_at"))), ("Completed", timestamp(task.get("finished_at")))]
        details = "".join(f"<dt>{esc(name)}</dt><dd>{value if name in {'Created', 'Started', 'Completed'} else esc(value or '—')}</dd>" for name, value in fields)
        output = "" if result is None else f'<section><h2>Final result</h2><pre>{esc(result.get("result") or task.get("failure") or "No result supplied.")}</pre></section>'
        cancel = "" if task.get("status") not in ACTIVE else f'<form method="post" action="/tasks/{esc(task_id)}/cancel"><input type="hidden" name="csrf" value="{esc(self.csrf_token)}"><input type="hidden" name="reason" value="Cancelled from dashboard"><button class="danger">Cancel task</button></form>'
        approval_controls = ""
        if task.get("status") == "approval_required":
            if self.approval_client is None:
                approval_controls = "<section><h2>Approval</h2><p>Approval is available only from the configured local approval interface.</p></section>"
            else:
                approval_controls = f'<section class="panel"><h2>Approval required</h2><form method="post" action="/tasks/{esc(task_id)}/approval"><input type="hidden" name="csrf" value="{esc(self.csrf_token)}"><label>Reason<input name="reason" maxlength="1000" required></label><button name="decision" value="approve" type="submit">Approve</button><button class="danger" name="decision" value="reject" type="submit">Reject</button></form></section>'
        return self.layout(str(task.get("title", "Task")), f'<a class="back" href="/">← Dashboard</a><section class="panel"><h1>{esc(task.get("title"))}</h1>{state_badge(task.get("status"))}<dl>{details}</dl>{cancel}</section>{approval_controls}<section><h2>Security</h2>{evidence}</section>{output}')

    def new_task(self, error: str = "") -> str:
        status = self.client.call("bridge_status")
        workers = self.client.call("worker_availability").get("workers", [])
        profiles = status.get("repository_profiles", [])
        options = "".join(f'<option value="{esc(value)}">{esc(value)}</option>' for value in profiles)
        worker_options = "".join(f'<option value="{esc(item.get("worker"))}">{esc(item.get("worker"))}</option>' for item in workers if item.get("available"))
        message = f'<p class="error">{esc(error)}</p>' if error else ""
        request_id = "dashboard-" + uuid.uuid4().hex
        return self.layout("New Task", f'''<a class="back" href="/">← Dashboard</a><section class="panel"><h1>New Task</h1>{message}<form method="post" action="/new-task"><input type="hidden" name="csrf" value="{esc(self.csrf_token)}"><input type="hidden" name="request_id" value="{request_id}"><label>Project<select name="repository_profile" required>{options}</select></label><label>Worker<select name="worker" required>{worker_options}</select></label><label>Task type<input value="Routine development" disabled><input type="hidden" name="consequence_class" value="routine_development"></label><label>Title<input name="title" maxlength="200" required></label><label>Instructions<textarea name="instructions" required></textarea></label><section class="preview"><h2>Authority preview</h2><p>Orchestra will decide whether standing authorisation applies. Network, credentials, production, deployment, and Git merge/push are not granted by this dashboard.</p></section><button type="submit">Submit task</button></form></section>''')

    def _form(self, environ: dict[str, Any]) -> dict[str, str]:
        raw_length = environ.get("CONTENT_LENGTH")
        if not isinstance(raw_length, str):
            raise RequestBodyError(HTTPStatus.BAD_REQUEST)
        try:
            length = int(raw_length)
        except ValueError:
            raise RequestBodyError(HTTPStatus.BAD_REQUEST) from None
        if length < 0:
            raise RequestBodyError(HTTPStatus.BAD_REQUEST)
        if length > 70_000:
            raise RequestBodyError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        raw = environ["wsgi.input"].read(length).decode("utf-8", "replace")
        return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}

    def submit(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        form = self._form(environ)
        if not secrets.compare_digest(form.get("csrf", ""), self.csrf_token):
            return self._page(start_response, self.layout("Request rejected", "<p>Invalid form request.</p>"), HTTPStatus.FORBIDDEN)
        title, instructions = form.get("title", "").strip(), form.get("instructions", "").strip()
        request_id = form.get("request_id", "")
        if (not title or not instructions or len(title) > 200 or "\n" in title
                or not re.fullmatch(r"dashboard-[0-9a-f]{32}", request_id)):
            return self._page(start_response, self.new_task("Enter a single-line title and instructions."), HTTPStatus.BAD_REQUEST)
        acknowledgement = self.client.call("submit_task", {"request_id": request_id, "title": title, "instructions": instructions, "worker": form.get("worker", ""), "repository_profile": form.get("repository_profile", ""), "consequence_class": "routine_development"})
        return self._page(start_response, self.layout("Task submitted", f'<section class="panel"><h1>Task submitted</h1><p>{state_badge(acknowledgement.get("status"))}</p><p><a href="/tasks/{esc(acknowledgement.get("task_id"))}">View task {esc(acknowledgement.get("task_id"))}</a></p></section>'))

    def cancel(self, task_id: str, environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        form = self._form(environ)
        if not secrets.compare_digest(form.get("csrf", ""), self.csrf_token):
            return self._page(start_response, self.layout("Request rejected", "<p>Invalid form request.</p>"), HTTPStatus.FORBIDDEN)
        self.client.call("cancel_task", {"task_id": task_id, "reason": form.get("reason", "Cancelled from dashboard")[:500] or "Cancelled from dashboard"})
        start_response("303 See Other", [("Location", f"/tasks/{task_id}"), ("Cache-Control", "no-store")])
        return [b""]

    def resolve_approval(self, task_id: str, environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        if self.approval_client is None:
            return self._page(start_response, self.layout("Unavailable", "<p>Approval is unavailable.</p>"), HTTPStatus.NOT_FOUND)
        form = self._form(environ)
        if not secrets.compare_digest(form.get("csrf", ""), self.csrf_token):
            return self._page(start_response, self.layout("Request rejected", "<p>Invalid form request.</p>"), HTTPStatus.FORBIDDEN)
        decision, reason = form.get("decision", ""), form.get("reason", "").strip()
        if decision not in {"approve", "reject"} or not reason or len(reason) > 1_000:
            return self._page(start_response, self.layout("Request rejected", "<p>Enter an approval reason.</p>"), HTTPStatus.BAD_REQUEST)
        self.approval_client.resolve(task_id, decision, reason)
        start_response("303 See Other", [("Location", f"/tasks/{task_id}"), ("Cache-Control", "no-store")])
        return [b""]


def main() -> None:
    from wsgiref.simple_server import make_server
    socket_path = os.environ.get("ORCHESTRA_OMCP_SOCKET", "/run/user/1000/orchestra/omcp.sock")
    approval_state_dir = os.environ.get("ORCHESTRA_DASHBOARD_APPROVAL_STATE_DIR")
    approver_identity = os.environ.get("ORCHESTRA_DASHBOARD_APPROVER_IDENTITY")
    host = os.environ.get("ORCHESTRA_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("ORCHESTRA_DASHBOARD_PORT", "8081"))
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("dashboard must bind to loopback; use the approved private access layer")
    if bool(approval_state_dir) != bool(approver_identity):
        raise SystemExit("dashboard approval requires both state directory and approver identity")
    approval_client = (LocalApprovalClient(approval_state_dir, approver_identity)
                       if approval_state_dir else None)
    with make_server(host, port, Dashboard(OMCPClient(socket_path), approval_client)) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
