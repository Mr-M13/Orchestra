"""Authenticated local web UI for OMCP approval review and decisions."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import html
import http.cookies
import ipaddress
import os
import secrets
import ssl
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from omcp.local_control import approval_request


SESSION_TTL_SECONDS = 15 * 60


def owner_state_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_dir() or resolved.stat().st_mode & 0o077:
        raise PermissionError("approval state directory must be owner-only")
    return resolved


def ensure_owner_secret(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    try:
        value = path.read_text().strip()
    except FileNotFoundError:
        value = secrets.token_hex(32)
        temporary = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (value + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        temporary.replace(path)
    os.chmod(path, 0o600)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("approval portal token must be a 32-byte lowercase hex value")
    return value


class PortalAuth:
    def __init__(self, secret: str, now: Callable[[], float] | None = None):
        self.secret = bytes.fromhex(secret)
        self.now = now or time.time

    def new_session(self) -> tuple[str, str]:
        session_id = secrets.token_hex(24)
        expires = int(self.now()) + SESSION_TTL_SECONDS
        payload = f"{expires}.{session_id}"
        signature = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}", session_id

    def validate(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            expires_text, session_id, signature = value.split(".", 2)
            expires = int(expires_text)
        except (TypeError, ValueError):
            return None
        payload = f"{expires}.{session_id}"
        expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        if expires < int(self.now()) or not hmac.compare_digest(signature, expected):
            return None
        return session_id

    def csrf(self, session_id: str, task_id: str) -> str:
        return hmac.new(
            self.secret, f"decision\0{session_id}\0{task_id}".encode(), hashlib.sha256,
        ).hexdigest()


def _risk_summary(details: dict[str, Any]) -> str:
    consequence = details.get("consequence_class", "unclassified")
    if consequence == "routine_development":
        return "Routine development is limited to isolated worktree preparation. Merge, push, deployment, credentials, destructive actions, and security configuration are outside this authority."
    if consequence == "production_deployment":
        return "Production deployment is consequential and requires this fresh explicit decision. Verify target, exact version, rollback plan, and evidence."
    if consequence == "destructive_operation":
        return "This operation is destructive. Confirm the exact target, scope, recovery path, and whether a reversible alternative exists."
    if consequence == "security_change":
        return "This changes a security boundary or policy. Confirm the authority impact, least privilege, tests, and rollback path."
    if consequence == "host_operation":
        return "This runs one built-in host-side acceptance operation. Verify the operation ID, pinned harness commit, exact target commit, repository, and disposable scope."
    return "This request is unclassified and therefore requires explicit approval. Treat its authority and side effects as unknown until reviewed."


def _layout(title: str, body: str) -> bytes:
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:880px;margin:36px auto;padding:0 18px;color:#18202b;background:#f5f7fa}}
.card{{background:white;border:1px solid #d8dee8;border-radius:14px;padding:22px;margin:14px 0;box-shadow:0 2px 10px #24324712}}
.meta{{display:grid;grid-template-columns:140px 1fr;gap:8px 14px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f8fa;padding:16px;border-radius:10px}}
.risk{{border-left:5px solid #d97706}}button,.button{{display:inline-block;border:0;border-radius:9px;padding:11px 16px;margin:5px;font-weight:650;text-decoration:none;cursor:pointer}}
.approve{{background:#16794b;color:white}}.reject{{background:#b42318;color:white}}.details{{background:#e5eaf1;color:#18202b}}
textarea{{width:100%;min-height:74px;box-sizing:border-box}}table{{width:100%;border-collapse:collapse}}th,td{{padding:9px;text-align:left;border-bottom:1px solid #e5eaf1}}code{{overflow-wrap:anywhere}}
</style></head><body><h1>{html.escape(title)}</h1>{body}</body></html>"""
    return page.encode()


def make_handler(
    state_dir: Path,
    bootstrap_token: str,
    auth: PortalAuth,
    local_request: Callable[[Path, dict[str, Any]], dict[str, Any]] = approval_request,
    secure_cookie: bool = False,
) -> type[BaseHTTPRequestHandler]:
    class ApprovalPortalHandler(BaseHTTPRequestHandler):
        server_version = "OrchestraApprovalPortal/1"

        def log_message(self, format: str, *args: object) -> None:
            # Avoid logging paths containing the bootstrap credential.
            return

        def _headers(self, status: int, content_type: str = "text/html; charset=utf-8") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")

        def _session(self) -> str | None:
            cookies = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookies.get("omcp_approval_session")
            return auth.validate(morsel.value if morsel else None)

        def _send_page(self, status: int, title: str, body: str) -> None:
            data = _layout(title, body)
            self._headers(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _task_page(self, task_id: str, message: str = "") -> None:
            try:
                details = local_request(state_dir, {"action": "details", "task_id": task_id})
            except Exception as exc:
                self._send_page(404, "Approval unavailable", f"<div class=\"card\">{html.escape(type(exc).__name__)}</div>")
                return
            session_id = self._session()
            assert session_id is not None
            csrf = auth.csrf(session_id, task_id)
            approval = details.get("approval", {})
            meta = {
                "Task": details.get("task_id"), "Status": details.get("status"),
                "Repository": details.get("repository"), "Profile": details.get("repository_profile"),
                "Base ref": details.get("base_ref"), "Worker": details.get("worker"),
                "Consequence": details.get("consequence_class"),
                "Requested": approval.get("requested_at", details.get("created_at")),
                "Expires": approval.get("expires_at", "—"),
            }
            if details.get("kind") == "host_operation":
                meta.update({
                    "Operation": details.get("operation_id"),
                    "Harness SHA": details.get("harness_commit"),
                    "Target SHA": details.get("target_commit"),
                })
            meta_html = "".join(
                f"<strong>{html.escape(str(key))}</strong><span>{html.escape(str(value))}</span>"
                for key, value in meta.items()
            )
            pending = details.get("status") == "approval_required" and approval.get("status") == "pending"
            controls = ""
            if pending:
                controls = f"""<form class="card" method="post" action="/task/{html.escape(task_id)}/decision">
<input type="hidden" name="csrf" value="{csrf}"><label for="reason"><strong>Decision reason</strong></label>
<textarea id="reason" name="reason" required maxlength="1000"></textarea><div>
<button class="approve" name="decision" value="approve">Approve</button>
<button class="reject" name="decision" value="reject">Reject</button>
<a class="button details" href="/history">View details</a></div></form>"""
            else:
                controls = f"<div class=\"card\"><strong>No decision is available.</strong> Current status: {html.escape(str(details.get('status')))}. <a href=\"/history\">View approval history</a>.</div>"
            notice = f"<div class=\"card\">{html.escape(message)}</div>" if message else ""
            body = f"""{notice}<div class="card"><h2>{html.escape(str(details.get('title', 'Task approval')))}</h2><div class="meta">{meta_html}</div></div>
<div class="card risk"><h2>Risk and authority</h2><p>{html.escape(_risk_summary(details))}</p></div>
<div class="card"><h2>Complete instructions</h2><pre>{html.escape(str(details.get('instructions', '')))}</pre></div>{controls}"""
            self._send_page(200, "Orchestra approval", body)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
            if len(parts) == 4 and parts[0] == "bootstrap" and parts[2] == "task":
                if not hmac.compare_digest(parts[1], bootstrap_token):
                    self._send_page(403, "Forbidden", "<div class=\"card\">Invalid approval bootstrap credential.</div>")
                    return
                task_id = parts[3]
                session, _ = auth.new_session()
                self.send_response(303)
                cookie_security = "; Secure" if secure_cookie else ""
                self.send_header("Set-Cookie", f"omcp_approval_session={session}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL_SECONDS}{cookie_security}")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Location", "/task/" + urllib.parse.quote(task_id, safe=""))
                self.end_headers()
                return
            if self._session() is None:
                self._send_page(401, "Authentication required", "<div class=\"card\">Open this page from an Orchestra approval notification.</div>")
                return
            if len(parts) == 2 and parts[0] == "task":
                self._task_page(parts[1])
                return
            if parts == ["history"]:
                try:
                    result = local_request(state_dir, {"action": "history", "limit": 100})
                except Exception as exc:
                    self._send_page(500, "History unavailable", f"<div class=\"card\">{html.escape(type(exc).__name__)}</div>")
                    return
                rows = "".join(
                    "<tr><td><a href=\"/task/{id}\">{id}</a></td><td>{repo}</td><td>{worker}</td><td>{status}</td><td>{approval}</td></tr>".format(
                        id=html.escape(str(item.get("task_id", ""))),
                        repo=html.escape(str(item.get("repository", ""))),
                        worker=html.escape(str(item.get("worker", ""))),
                        status=html.escape(str(item.get("status", ""))),
                        approval=html.escape(str(item.get("approval", {}).get("status", ""))),
                    ) for item in result.get("approvals", [])
                )
                self._send_page(200, "Approval history", f"<div class=\"card\"><table><tr><th>Task</th><th>Repository</th><th>Worker</th><th>Status</th><th>Approval</th></tr>{rows}</table></div>")
                return
            self._send_page(404, "Not found", "<div class=\"card\">Unknown approval page.</div>")

        def do_POST(self) -> None:
            session_id = self._session()
            if session_id is None:
                self._send_page(401, "Authentication required", "<div class=\"card\">Session missing or expired.</div>")
                return
            parts = [urllib.parse.unquote(part) for part in urllib.parse.urlparse(self.path).path.split("/") if part]
            if len(parts) != 3 or parts[0] != "task" or parts[2] != "decision":
                self._send_page(404, "Not found", "<div class=\"card\">Unknown approval action.</div>")
                return
            task_id = parts[1]
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 1 <= length <= 4096:
                self._send_page(400, "Invalid decision", "<div class=\"card\">Invalid request size.</div>")
                return
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "strict"), keep_blank_values=True)
            csrf = form.get("csrf", [""])[0]
            if not hmac.compare_digest(csrf, auth.csrf(session_id, task_id)):
                self._send_page(403, "Forbidden", "<div class=\"card\">Invalid decision token.</div>")
                return
            decision = form.get("decision", [""])[0]
            reason = form.get("reason", [""])[0]
            if decision not in {"approve", "reject"}:
                self._send_page(400, "Invalid decision", "<div class=\"card\">Decision must be approve or reject.</div>")
                return
            try:
                result = local_request(state_dir, {
                    "action": "resolve", "task_id": task_id, "decision": decision,
                    "reason": reason, "approver_identity": "local:approval-portal:" + getpass.getuser(),
                })
            except Exception as exc:
                self._send_page(409, "Decision not applied", f"<div class=\"card\">{html.escape(type(exc).__name__)}</div>")
                return
            self._task_page(task_id, f"Decision applied: {result.get('approval', {}).get('status', result.get('status'))}")

    return ApprovalPortalHandler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the authenticated OMCP approval interface")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8767)
    parser.add_argument("--token-path", type=Path)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--client-ca", type=Path)
    args = parser.parse_args(argv)
    state_dir = owner_state_dir(args.state_dir)
    token_path = args.token_path or (state_dir / "approval-portal.token")
    token = ensure_owner_secret(token_path)
    address = ipaddress.ip_address(args.listen_host)
    is_loopback = address.is_loopback
    if not is_loopback:
        if not args.allow_remote:
            raise PermissionError("remote approval binding requires --allow-remote")
        if not all((args.tls_cert, args.tls_key, args.client_ca)):
            raise PermissionError("remote approval binding requires TLS and a client CA")
        if args.listen_host in {"0.0.0.0", "::"}:
            raise PermissionError("remote approval must bind one explicit interface address")
    handler = make_handler(state_dir, token, PortalAuth(token), secure_cookie=not is_loopback)
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), handler)
    if not is_loopback:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_cert_chain(str(args.tls_cert), str(args.tls_key))
        context.load_verify_locations(cafile=str(args.client_ca))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
