"""Minimal, server-rendered Orchestra dashboard; no browser credentials or state."""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs

from dashboard.omcp import OMCPClient, OMCPError
from dashboard.system import SystemCollector


TERMINAL = {"succeeded", "failed", "cancelled", "rejected", "interrupted"}
TASK_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
RECENT_LIMIT = 20
AGENT_ORDER = ("deepseek-code-1", "deepseek-code-2", "codex")
STATIC_FILES = {
    "/static/system.js": ("system.js", "text/javascript; charset=utf-8"),
    "/static/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/static/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
}
STATUS_LABELS = {
    "starting": "Starting",
    "running": "Running",
    "cancel_requested": "Cancelling",
    "accepted": "Queued",
    "approval_required": "Awaiting approval",
    "succeeded": "Succeeded",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "rejected": "Rejected",
    "interrupted": "Interrupted",
}


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


def short_id(task_id: Any) -> str:
    value = str(task_id or "")
    return (value[:8] + "…") if len(value) > 8 else (value or "—")


def state_badge(status: Any) -> str:
    value = str(status or "unknown")
    label = esc(STATUS_LABELS.get(value, value.replace("_", " ")))
    safe = re.sub(r"[^a-z0-9_-]", "-", value.lower()) or "unknown"
    return f'<span class="state state-{safe}">{label}</span>'


class Dashboard:
    """A WSGI app with explicit OMCP operations and fixed read-only projections."""

    def __init__(self, client: Any, static_root: Path | None = None,
                 system_collector: Any | None = None) -> None:
        self.system_collector = system_collector or SystemCollector()
        self.client = client
        self.static_root = static_root or Path(__file__).with_name("static")
        self.csrf_token = secrets.token_urlsafe(32)

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        path = environ.get("PATH_INFO", "/")
        method = environ.get("REQUEST_METHOD", "GET")
        cancel_match = re.fullmatch(r"/tasks/(" + TASK_ID_RE.pattern + r")/cancel", path)
        follow_match = re.fullmatch(r"/tasks/(" + TASK_ID_RE.pattern + r")/follow-up", path)
        task_match = re.fullmatch(r"/tasks/(" + TASK_ID_RE.pattern + r")", path)
        try:
            if path in STATIC_FILES and method == "GET":
                return self._static(path, start_response)
            if path == "/api/system":
                if method != "GET":
                    return self._json(start_response, HTTPStatus.METHOD_NOT_ALLOWED,
                                      {"error": "method_not_allowed"}, [("Allow", "GET")])
                return self._json(start_response, HTTPStatus.OK, self.system_collector.snapshot())
            if path == "/api/tasks":
                return self._api_tasks(method, start_response)
            if path == "/" and method == "GET":
                return self._page(start_response, self.home())
            if path in {"/pixelbox", "/mbox", "/settings"} and method == "GET":
                return self._page(start_response, self.system_page(path[1:]))
            if path == "/new-task" and method == "GET":
                return self._page(start_response, self.new_task())
            if path == "/new-task" and method == "POST":
                return self.submit(environ, start_response)
            if path == "/approvals" and method == "GET":
                return self._page(start_response, self.approvals())
            if cancel_match and method == "POST":
                return self.cancel(cancel_match.group(1), environ, start_response)
            if follow_match and method == "POST":
                return self.follow_up(follow_match.group(1), environ, start_response)
            if task_match and method == "GET":
                return self._page(start_response, self.task_detail(task_match.group(1)))
            return self._page(start_response, self.layout("Not found", "<p>That page does not exist.</p>"), HTTPStatus.NOT_FOUND)
        except RequestBodyError as exc:
            message = "Request body is too large." if exc.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE else "Invalid request body."
            return self._page(start_response, self.layout("Request rejected", f"<p>{message}</p>"), exc.status)
        except OMCPError:
            return self._page(start_response, self.unavailable(), HTTPStatus.SERVICE_UNAVAILABLE)

    def _static(self, path: str, start_response: Callable[..., Any]) -> list[bytes]:
        filename, content_type = STATIC_FILES[path]
        body = (self.static_root / filename).read_bytes()
        start_response("200 OK", [("Content-Type", content_type), ("Cache-Control", "public, max-age=300")])
        return [body]

    def _json(self, start_response: Callable[..., Any], status: HTTPStatus,
              value: dict[str, Any],
              headers: list[tuple[str, str]] | None = None) -> list[bytes]:
        body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        response_headers = [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Content-Security-Policy", "default-src 'none'"),
            ("X-Content-Type-Options", "nosniff"),
            ("Cache-Control", "no-store"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
        ]
        response_headers.extend(headers or [])
        start_response(f"{status.value} {status.phrase}", response_headers)
        return [body]

    def _api_tasks(self, method: str, start_response: Callable[..., Any]) -> list[bytes]:
        if method != "GET":
            return self._json(start_response, HTTPStatus.METHOD_NOT_ALLOWED,
                              {"error": "method_not_allowed"}, [("Allow", "GET")])
        try:
            snapshot = self.client.call("task_snapshot", {"recent_limit": RECENT_LIMIT})
        except OMCPError:
            return self._json(start_response, HTTPStatus.SERVICE_UNAVAILABLE,
                              {"error": "orchestra_unavailable"})
        if not isinstance(snapshot, dict):
            return self._json(start_response, HTTPStatus.SERVICE_UNAVAILABLE,
                              {"error": "invalid_orchestra_response"})
        return self._json(start_response, HTTPStatus.OK, snapshot)

    def _page(self, start_response: Callable[..., Any], body: str, status: HTTPStatus = HTTPStatus.OK) -> list[bytes]:
        encoded = body.encode("utf-8")
        start_response(f"{status.value} {status.phrase}", [
            ("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(encoded))),
            ("Content-Security-Policy", "default-src 'self'; script-src 'self'; connect-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
            ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"), ("Cache-Control", "no-store"),
        ])
        return [encoded]

    def layout(self, title: str, content: str, status: str = "") -> str:
        links = "".join(f'<a href="{href}" class="nav-link{" active" if title.lower() == label.lower() else ""}">{label}</a>' for href, label in (("/", "Orchestra"), ("/pixelbox", "PixelBox"), ("/mbox", "Mbox"), ("/settings", "Settings")))
        return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)} · PixelBox</title><link rel="stylesheet" href="/static/dashboard.css"><script src="/static/dashboard.js" defer></script><script src="/static/system.js" defer></script></head><body><header><a class="brand" href="/">Orchestra</a><nav aria-label="Main menu">{links}</nav>{status}<button class="button secondary" id="refresh-tasks" type="button">Refresh</button><a class="button" href="/new-task">+ New Task</a></header><main id="task-dashboard" data-recent-limit="{RECENT_LIMIT}">{content}</main></body></html>'''

    def unavailable(self) -> str:
        return self.layout("Offline", self.system_panel() + "<section class=\"panel empty\"><h1>Orchestra is offline</h1><p>The dashboard could not reach OMCP. No task action was attempted.</p></section>", state_badge("offline"))

    @staticmethod
    def _items(value: Any) -> list[dict[str, Any]]:
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def home(self) -> str:
        status = self.client.call("bridge_status")
        readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
        readiness_status = str(readiness.get("status") or status.get("status") or "offline")
        readiness_workers = {
            item.get("worker"): item for item in readiness.get("workers", [])
            if isinstance(item, dict)
        }
        workers = self.client.call("worker_availability").get("workers", [])
        snapshot = self.client.call("task_snapshot", {"recent_limit": RECENT_LIMIT})
        running = self._items(snapshot.get("running"))
        queued = self._items(snapshot.get("queued"))
        recent = self._items(snapshot.get("recent"))
        counts = snapshot.get("counts") if isinstance(snapshot.get("counts"), dict) else {}
        pending = [task for task in queued if task.get("queue_state") == "approval_pending"]
        current_by_worker = {task.get("worker"): task for task in running}

        def worker_state(worker: dict[str, Any]) -> str:
            info = readiness_workers.get(worker.get("worker"), {})
            if info.get("role") == "response_only":
                return "response-only"
            if info.get("role") == "remote_unproven":
                return "unproven"
            if (worker.get("adapter") in {"codex", "opencode"}
                    and worker.get("proven_healthy") is not True
                    and worker.get("last_attempt") is None):
                return "unproven"
            if worker.get("current_task_id") or worker.get("idle") is False:
                return "busy"
            if worker.get("capacity_available") is False:
                return "busy"
            last_attempt = worker.get("last_attempt")
            last_category = (
                last_attempt.get("failure_category")
                if isinstance(last_attempt, dict) else None
            )
            last_status = last_attempt.get("status") if isinstance(last_attempt, dict) else None
            if last_category == "credit_exhausted":
                return "unavailable"
            if last_status in {"failed", "interrupted"} and worker.get("proven_healthy") is not True:
                return "unavailable"
            if worker.get("proven_healthy") is True:
                return "available"
            if worker.get("runnable") or worker.get("reason") == "no_queued_task":
                return "available"
            return "unavailable"

        def worker_reason(worker: dict[str, Any]) -> str:
            last_attempt = worker.get("last_attempt")
            last_category = (
                last_attempt.get("failure_category")
                if isinstance(last_attempt, dict) else None
            )
            last_status = last_attempt.get("status") if isinstance(last_attempt, dict) else None
            if last_category == "credit_exhausted":
                return " · last attempt blocked: workspace credits exhausted"
            if last_status in {"failed", "interrupted"} and worker.get("proven_healthy") is not True:
                return f" · last attempt failed: {last_category or 'unknown'}"
            reason = worker.get("reason")
            if reason in (None, "no_queued_task", "worker_busy"):
                return ""
            return f" · {reason}"

        enabled = [worker for worker in workers if worker.get("enabled", True)]
        enabled.sort(key=lambda worker: (AGENT_ORDER.index(worker.get("worker")) if worker.get("worker") in AGENT_ORDER else 100, str(worker.get("worker"))))
        worker_rows = "".join(
            f'<article class="worker"><h3>Agent {number}</h3>{state_badge(worker_state(worker))}'
            f'<p><strong>{esc(worker.get("worker"))}</strong> · {esc(worker.get("provider") or worker.get("adapter") or "unknown")} / {esc(worker.get("model") or "default model")}</p>'
            f'<p>{esc(readiness_workers.get(worker.get("worker"), {}).get("role", "coding").replace("_", "-"))}'
            f' · {esc(current_by_worker.get(worker.get("worker"), {}).get("title", ""))}{esc(worker_reason(worker))}</p></article>'
            for number, worker in enumerate(enabled[:3], 1)
        ) or "<p>No agents are enabled.</p>"
        approvals_html = (f'<a class="approval" href="/approvals">{len(pending)} task{"s" if len(pending) != 1 else ""} waiting for approval</a>' if pending else "")
        generated_at = snapshot.get("generated_at") if isinstance(snapshot.get("generated_at"), str) else ""
        queued_detail = (
            f'{int(counts.get("ready", 0))} ready · '
            f'{int(counts.get("approval_pending", 0))} awaiting approval'
        )
        recent_detail = f'Latest {int(counts.get("recent_limit", RECENT_LIMIT))}'
        monitor = f'''<section class="monitor-bar" aria-label="Task update status"><div><strong>Live task monitor</strong><span id="refresh-status" role="status" aria-live="polite" data-generated-at="{esc(generated_at)}">Updated {timestamp(generated_at)}</span></div></section><div class="warning" id="refresh-warning" role="status" aria-live="polite" hidden></div>'''
        reasons = readiness.get("reasons") if isinstance(readiness.get("reasons"), list) else []
        readiness_html = (
            f'<section class="panel readiness readiness-{esc(readiness_status)}">'
            f'<h2>Current readiness: {esc(readiness_status)}</h2>'
            f'<p>{"Accepting new tasks." if readiness.get("accepting") else "Not accepting new tasks."} '
            f'{"A verified coding worker is available now." if readiness.get("can_complete_now") else "No verified coding worker is available to complete a task immediately."}</p>'
            + (f'<p>Reasons: {esc(", ".join(str(item) for item in reasons))}</p>' if reasons else "")
            + '</section>'
        )
        return self.layout("Orchestra", f'''<section class="intro"><div><h1>Orchestra tasks</h1><p>Three active agent slots and current work.</p></div>{approvals_html}</section>{readiness_html}{monitor}<section><h2>Agents</h2><div class="workers">{worker_rows}</div></section><div class="task-sections">{self.task_section("running", "Running Tasks", running, "No tasks are running.", True)}{self.task_section("queued", "Queued Tasks", queued, "No tasks are queued or awaiting approval.", True, queued_detail)}{self.task_section("recent", "Recent Tasks", recent, "No recent terminal tasks.", False, recent_detail)}</div>''', state_badge(readiness_status))

    def system_page(self, area: str) -> str:
        labels_by_area = {
            "pixelbox": ("pixelbox", "photo", "photo-timers", "photo-recent", "pixelbox-services"),
            "mbox": ("mbox", "nextcloud"),
            "settings": ("orchestra-services", "public"),
        }
        intro = {"pixelbox": "PixelBox hardware, photos and services",
                 "mbox": "Mbox storage and Nextcloud",
                 "settings": "Health and diagnostics"}[area]
        diagnostic = ""
        if area == "settings":
            status = self.client.call("bridge_status")
            ready = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
            diagnostic = f'<section class="panel"><h2>Orchestra health</h2><p>Readiness: {esc(ready.get("status", "unavailable"))} · accepting tasks: {esc(ready.get("accepting", False))}</p><p>Release and service checks remain in the diagnostics below.</p></section>'
        return self.layout(area.capitalize(), f'<h1>{esc(area.capitalize())}</h1><p>{esc(intro)}</p>{diagnostic}{self.system_panel(labels_by_area[area])}')

    def system_panel(self, keys: tuple[str, ...] | None = None) -> str:
        labels = [
            ("pixelbox", "PixelBox system"), ("photo", "Photo pipeline"),
            ("photo-timers", "Photo timers"), ("photo-recent", "Recent photo events"),
            ("mbox", "Mbox storage"), ("nextcloud", "Nextcloud"),
            ("pixelbox-services", "PixelBox services"),
            ("orchestra-services", "Orchestra services"),
            ("public", "Cloudflare public health"),
        ]
        cards = "".join(f'<article class="panel"><h3>{esc(label)}</h3><p class="system-value" id="system-{esc(key)}">Unavailable — awaiting collection</p></article>' for key, label in labels if keys is None or key in keys)
        return f'''<section id="system-overview" aria-labelledby="system-heading"><h2 id="system-heading">System overview · read only</h2><p id="system-updated" role="status" aria-live="polite">Loading system overview…</p><div class="system-grid">{cards}</div><p>Data comes from the fixed loopback status projection. Storage and container reachability do not prove application health or a restorable backup.</p></section>'''

    def task_section(self, key: str, heading: str, tasks: list[dict[str, Any]],
                     empty: str, expanded: bool, detail: str = "") -> str:
        rows = "".join(self.task_row(task, key) for task in tasks)
        panel_hidden = "" if expanded else " hidden"
        empty_hidden = " hidden" if tasks else ""
        detail_html = f'<span class="section-detail" data-section-detail="{key}">{esc(detail)}</span>' if detail else ""
        return f'''<section class="task-section" data-section="{key}"><h2 class="section-heading"><button class="section-toggle" id="{key}-toggle" type="button" aria-expanded="{"true" if expanded else "false"}" aria-controls="{key}-panel" data-section-toggle="{key}"><span class="chevron" aria-hidden="true"></span><span class="section-name">{esc(heading)}</span><span class="section-count" data-section-count="{key}">{len(tasks)}</span>{detail_html}</button></h2><div class="task-panel" id="{key}-panel" role="region" aria-labelledby="{key}-toggle" data-section-panel="{key}"{panel_hidden}><div class="task-list" data-task-list="{key}">{rows}</div><div class="panel empty" data-empty="{key}"{empty_hidden}>{esc(empty)}</div></div></section>'''

    def task_row(self, task: dict[str, Any], section: str) -> str:
        task_id = str(task.get("task_id") or "")
        title = str(task.get("title") or "Untitled task")
        worker = str(task.get("worker") or "Worker not selected")
        status = str(task.get("status") or "unknown")
        progress = str(task.get("progress") or "")
        href = f'/tasks/{esc(task_id)}'
        queue_state = task.get("queue_state")
        meta: list[str] = [f'<span>Worker <strong>{esc(worker)}</strong></span>']
        side_extra = ""
        row_class = "task-row"
        if section == "running":
            started = task.get("started_at") or task.get("created_at")
            meta.append(f'<span>Started {timestamp(started)}</span>')
            side_extra = f'<span class="elapsed" data-started-at="{esc(started or "")}">{elapsed(started)}</span>'
        elif section == "queued":
            meta.append(f'<span>Submitted {timestamp(task.get("created_at"))}</span>')
            if queue_state == "approval_pending":
                meta.append('<span class="queue-state queue-approval">Awaiting approval</span>')
                row_class += " is-approval"
            else:
                meta.append('<span class="queue-state queue-ready">Ready to execute</span>')
        else:
            terminal_at = task.get("terminal_at") or task.get("finished_at") or task.get("created_at")
            meta.append(f'<span>Completed {timestamp(terminal_at)}</span>')
            if status in {"failed", "interrupted"}:
                row_class += " is-failed"
        if progress:
            meta.append(f'<span class="task-progress">{esc(progress)}</span>')
        return f'''<article class="{row_class}" data-task-id="{esc(task_id)}" data-status="{esc(status)}"><div class="task-main"><div class="task-heading"><a class="task-title" href="{href}" title="{esc(title)}">{esc(title)}</a><code class="task-id" title="{esc(task_id)}">{esc(short_id(task_id))}</code></div><div class="task-meta">{"".join(meta)}</div></div><div class="task-side">{state_badge(status)}{side_extra}<a class="task-details" href="{href}">Details<span class="sr-only"> for {esc(title)}</span></a></div></article>'''

    def task_detail(self, task_id: str) -> str:
        task = self.client.call("task_status", {"task_id": task_id})
        result = None
        if task.get("status") in TERMINAL:
            result = self.client.call("task_result", {"task_id": task_id})
        auth = task.get("authorization") if isinstance(task.get("authorization"), dict) else {}
        capabilities = task.get("execution_capabilities") if isinstance(task.get("execution_capabilities"), dict) else None
        if not capabilities:
            evidence = "<p>Security evidence unavailable</p>"
        else:
            writable_paths = capabilities.get("writable_paths") if isinstance(capabilities.get("writable_paths"), list) else []
            writable_files = capabilities.get("writable_files") if isinstance(capabilities.get("writable_files"), list) else []
            evidence = "<ul class=\"security\">" + "".join(
                f"<li>{'✓' if value is False or value == 'read_only' else '•'} {esc(label)}: {esc('denied' if value is False else value)}</li>"
                for label, value in (
                    ("Network", capabilities.get("network")),
                    ("External credentials", capabilities.get("external_credentials")),
                    ("Git metadata", capabilities.get("git_metadata")),
                )
            ) + "".join(
                f"<li>• {esc(label)}: {esc(', '.join(str(value) for value in values) or 'none')}</li>"
                for label, values in (
                    ("Writable directories", writable_paths),
                    ("Writable files", writable_files),
                )
            ) + "</ul>"
        fields = [("Project", task.get("repository_profile", task.get("repository"))), ("Worker", task.get("worker")), ("State", task.get("status")), ("Intent", task.get("task_intent")), ("Objective", task.get("objective_status", "unverified")), ("Objective note", task.get("objective_note")), ("Consequence class", task.get("consequence_class")), ("Authorisation", auth.get("mode")), ("Standing-authorisation ID", auth.get("authorization_id")), ("Created", timestamp(task.get("created_at"))), ("Started", timestamp(task.get("started_at"))), ("Completed", timestamp(task.get("finished_at")))]
        details = "".join(f"<dt>{esc(name)}</dt><dd>{value if name in {'Created', 'Started', 'Completed'} else esc(value or '—')}</dd>" for name, value in fields)
        failure = task.get("failure") if isinstance(task.get("failure"), dict) else None
        failure_html = ""
        if failure:
            category = failure.get("category") or failure.get("type")
            stage = failure.get("stage")
            next_action = failure.get("next_action")
            failure_html = (
                '<section class="panel"><h2>Failure</h2>'
                f'<p><strong>{esc(category or "unknown")}</strong></p>'
                f'<dl><dt>Stage</dt><dd>{esc(stage or "—")}</dd>'
                f'<dt>Exit code</dt><dd>{esc(task.get("exit_code") if task.get("exit_code") is not None else "—")}</dd></dl>'
                + (f'<p><strong>Next action:</strong> {esc(next_action)}</p>' if next_action else "")
                + '</section>'
            )
        result_text = result.get("result") if isinstance(result, dict) else None
        output = (
            f'<section><h2>Final result</h2><pre>{esc(result_text or "No result supplied.")}</pre></section>'
            if result is not None and result_text else ""
        )
        cancel = "" if task.get("status") in TERMINAL else f'<form method="post" action="/tasks/{esc(task_id)}/cancel"><input type="hidden" name="csrf" value="{esc(self.csrf_token)}"><input type="hidden" name="reason" value="Cancelled from dashboard"><button class="danger">Cancel task</button></form>'
        approval_controls = ""
        if task.get("status") == "approval_required":
            approval_controls = '''<section class="panel"><h2>Approval required</h2><p>This public dashboard does not accept approval decisions. Review the complete instructions, risk and requested authority in the owner-only local approval portal on the PixelBox host at <code>http://127.0.0.1:8767</code>, opened through an authenticated SSH tunnel. The portal requires its owner-only bootstrap token and applies session expiry, CSRF, audit, reason and self-approval checks.</p><p><a href="/approvals">Pending approvals and local access instructions</a></p></section>'''
        workers = self.client.call("worker_availability").get("workers", [])
        targets = "".join(f'<option value="{esc(item.get("worker"))}"{" selected" if item.get("worker") == task.get("worker") else ""}>{esc(item.get("worker"))} · {esc(item.get("provider") or item.get("adapter") or "unknown")} / {esc(item.get("model") or "default model")}</option>' for item in workers if item.get("enabled", True))
        follow_form = f'<section class="panel"><h2>Follow up or request a review</h2><form method="post" action="/tasks/{esc(task_id)}/follow-up"><input type="hidden" name="csrf" value="{esc(self.csrf_token)}"><input type="hidden" name="request_id" value="dashboard-{uuid.uuid4().hex}"><label>Agent<select name="worker" required>{targets}</select></label><label>Instructions<textarea name="instructions" maxlength="10000" required></textarea></label><button name="action" value="follow_up" type="submit">Add follow-up</button> <button class="button secondary" name="action" value="review" type="submit">Send to agent for review</button></form><p>Reviews are analysis tasks. The prior result is included as untrusted context; a review does not deploy code.</p></section>'
        parent = task.get("follow_up_of")
        parent_link = f'<p>Follow-up to <a href="/tasks/{esc(parent)}">{esc(short_id(parent))}</a></p>' if parent else ""
        return self.layout(str(task.get("title", "Task")), f'<a class="back" href="/">← Dashboard</a><section class="panel"><h1>{esc(task.get("title"))}</h1>{state_badge(task.get("status"))}{parent_link}<dl>{details}</dl>{cancel}</section>{failure_html}{approval_controls}<section><h2>Security</h2>{evidence}</section>{output}{follow_form}')

    def new_task(self, error: str = "") -> str:
        status = self.client.call("bridge_status")
        workers = self.client.call("worker_availability").get("workers", [])
        profiles = status.get("repository_profiles", [])
        options = "".join(f'<option value="{esc(value)}">{esc(value)}</option>' for value in profiles)
        worker_options = "".join(
            f'<option value="{esc(item.get("worker"))}" data-coding-ready="{str(bool(item.get("coding_ready"))).lower()}">{esc(item.get("worker"))} — {"verified coding artifacts" if item.get("coding_ready") else "analysis only in this form; coding unproven or unsupported"}</option>'
            for item in workers if item.get("enabled", True)
        )
        message = f'<p class="error">{esc(error)}</p>' if error else ""
        request_id = "dashboard-" + uuid.uuid4().hex
        return self.layout("New Task", f'''<a class="back" href="/">← Dashboard</a><section class="panel"><h1>New Task</h1>{message}<form method="post" action="/new-task"><input type="hidden" name="csrf" value="{esc(self.csrf_token)}"><input type="hidden" name="request_id" value="{request_id}"><label>Project<select name="repository_profile" required>{options}</select></label><label>Deliverable<select name="task_intent" required><option value="analysis">Analysis / review — response delivered</option><option value="code_change">Code change — contract and changed files required</option><option value="deployment" disabled>Deployment — no compatible verified worker</option></select></label><label>Worker<select name="worker" required>{worker_options}</select></label><label>Task type<input value="Routine development" disabled><input type="hidden" name="consequence_class" value="routine_development"></label><label>Title<input name="title" maxlength="200" required></label><label>Instructions<textarea name="instructions" required></textarea></label><section class="preview"><h2>Authority preview</h2><p>Orchestra will decide whether standing authorisation applies. Network, credentials, production, deployment, and Git merge/push are not granted by this dashboard.</p></section><button type="submit">Submit task</button></form></section>''')

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
        acknowledgement = self.client.call("submit_task", {"request_id": request_id, "title": title, "instructions": instructions, "worker": form.get("worker", ""), "repository_profile": form.get("repository_profile", ""), "consequence_class": "routine_development", "task_intent": form.get("task_intent", "analysis")})
        return self._page(start_response, self.layout("Task submitted", f'<section class="panel"><h1>Task submitted</h1><p>{state_badge(acknowledgement.get("status"))}</p><p><a href="/tasks/{esc(acknowledgement.get("task_id"))}">View task {esc(acknowledgement.get("task_id"))}</a></p></section>'))

    def follow_up(self, task_id: str, environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        form = self._form(environ)
        if not secrets.compare_digest(form.get("csrf", ""), self.csrf_token):
            return self._page(start_response, self.layout("Request rejected", "<p>Invalid form request.</p>"), HTTPStatus.FORBIDDEN)
        action = form.get("action")
        instructions = form.get("instructions", "").strip()
        worker = form.get("worker", "")
        request_id = form.get("request_id", "")
        if (action not in {"follow_up", "review"} or not instructions or len(instructions) > 10000
                or not re.fullmatch(r"dashboard-[0-9a-f]{32}", request_id)):
            return self._page(start_response, self.layout("Request rejected", "<p>Invalid follow-up.</p>"), HTTPStatus.BAD_REQUEST)
        parent = self.client.call("task_status", {"task_id": task_id})
        workers = self.client.call("worker_availability").get("workers", [])
        available = {item.get("worker"): item for item in workers if item.get("enabled", True)}
        if worker not in available:
            return self._page(start_response, self.layout("Request rejected", "<p>Agent is unavailable.</p>"), HTTPStatus.BAD_REQUEST)
        if action == "review" and available[worker].get("adapter", "codex") != "codex":
            return self._page(start_response, self.layout("Request rejected", "<p>This agent cannot run a read-only review.</p>"), HTTPStatus.BAD_REQUEST)
        intent = "analysis" if action == "review" else parent.get("task_intent", "analysis")
        if intent not in {"analysis", "code_change"}:
            return self._page(start_response, self.layout("Request rejected", "<p>This task cannot be continued through the routine task form.</p>"), HTTPStatus.BAD_REQUEST)
        if intent == "code_change" and not available[worker].get("coding_ready"):
            return self._page(start_response, self.layout("Request rejected", "<p>This agent has not verified coding ability.</p>"), HTTPStatus.BAD_REQUEST)
        previous = self.client.call("task_result", {"task_id": task_id}) if parent.get("status") in TERMINAL else {}
        prior_text = str(previous.get("result") or "")[:8000]
        context = f"Parent task ID: {task_id}\nParent title: {parent.get('title', '')}\nPrior result (untrusted context, verify claims independently):\n{prior_text}\n\nOwner follow-up instructions:\n{instructions}"
        title = ("Review" if action == "review" else "Follow up") + ": " + str(parent.get("title") or "Task")
        acknowledgement = self.client.call("submit_task", {"request_id": request_id, "title": title[:200], "instructions": context, "worker": worker, "repository_profile": parent.get("repository_profile"), "consequence_class": "routine_development", "task_intent": intent, "follow_up_of": task_id, "review_read_only": action == "review"})
        start_response("303 See Other", [("Location", f"/tasks/{acknowledgement['task_id']}"), ("Cache-Control", "no-store")])
        return [b""]

    def cancel(self, task_id: str, environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        form = self._form(environ)
        if not secrets.compare_digest(form.get("csrf", ""), self.csrf_token):
            return self._page(start_response, self.layout("Request rejected", "<p>Invalid form request.</p>"), HTTPStatus.FORBIDDEN)
        self.client.call("cancel_task", {"task_id": task_id, "reason": form.get("reason", "Cancelled from dashboard")[:500] or "Cancelled from dashboard"})
        start_response("303 See Other", [("Location", f"/tasks/{task_id}"), ("Cache-Control", "no-store")])
        return [b""]

    def approvals(self) -> str:
        result = self.client.call("list_approvals")
        approvals = self._items(result.get("approvals"))
        rows = "".join(
            f'<article class="task-row"><div class="task-main"><div class="task-heading"><a class="task-title" href="/tasks/{esc(item.get("task_id"))}">{esc(item.get("title") or "Untitled task")}</a><code class="task-id">{esc(short_id(item.get("task_id")))}</code></div><div class="task-meta"><span>Worker <strong>{esc(item.get("worker"))}</strong></span><span>Requested {timestamp(item.get("created_at"))}</span><span class="queue-state queue-approval">Awaiting local decision</span></div></div><div class="task-side">{state_badge(item.get("status"))}<a class="task-details" href="/tasks/{esc(item.get("task_id"))}">Details</a></div></article>'
            for item in approvals
        ) or '<div class="panel empty">No protected tasks are awaiting approval.</div>'
        return self.layout("Owner approvals", f'''<a class="back" href="/">← Dashboard</a><section class="panel"><h1>Owner approval interface</h1><p><strong>No approval controls are available in this public dashboard.</strong> Approval decisions use the separate, owner-authenticated local portal, which binds only to loopback on PixelBox and calls OMCP's existing owner-only Unix approval socket with a local capability token.</p><p>From an owner-controlled machine, open an authenticated SSH tunnel to PixelBox:</p><pre>ssh -N -L 8767:127.0.0.1:8767 hamed@&lt;pixelbox-tailscale-host&gt;</pre><p>Then open <code>http://127.0.0.1:8767/bootstrap/&lt;owner-only-token&gt;/task/&lt;task-id&gt;</code>. Read the token directly from the owner-only state file on PixelBox; do not send it through chat, logs or the public dashboard. The portal displays the complete instructions, risk summary, requested authority, expiry and worker, and requires a reason plus a CSRF-protected decision.</p></section><section><h2>Pending approvals (read only)</h2><div class="task-list">{rows}</div></section>''')


def main() -> None:
    from wsgiref.simple_server import make_server
    socket_path = os.environ.get("ORCHESTRA_OMCP_SOCKET", "/run/user/1000/orchestra/omcp.sock")
    host = os.environ.get("ORCHESTRA_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("ORCHESTRA_DASHBOARD_PORT", "8081"))
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("dashboard must bind to loopback; use the approved private access layer")
    # Approval decisions are intentionally not accepted by this public
    # dashboard.  The separate owner-only local approval portal is the only
    # interface that can call OMCP's local approval socket.
    with make_server(host, port, Dashboard(OMCPClient(socket_path))) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
