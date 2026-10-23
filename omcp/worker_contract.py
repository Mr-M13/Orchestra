"""Structured worker completion and security-assertion contract.

The worker must explicitly write a JSON completion record inside its task
worktree.  The coordinator never infers semantic success from natural-language
worker output.  Standing routine tasks additionally run a trusted probe that
attempts each negative security assertion inside the worker sandbox and writes
a structured result.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any


CONTRACT_DIR = ".orchestra-tmp"
CONTRACT_FILE = "orchestra-completion.json"
SECURITY_PROBE_FILE = "orchestra-security-probe.py"
SECURITY_REPORT_FILE = "orchestra-security-probe.json"
REQUIRED_SECURITY_CHECKS = (
    "read_orchestra_config",
    "read_orchestra_secret",
    "write_outside_worktree",
    "network_egress",
    "git_metadata_write",
)
MAX_CONTRACT_BYTES = 16 * 1024
MAX_SECURITY_REPORT_BYTES = 64 * 1024


class CompletionContractError(ValueError):
    """The worker did not satisfy the structured completion contract."""


class SecurityAssertionError(ValueError):
    """A required negative security assertion was not actually denied."""


def contract_path(workspace: Path) -> Path:
    return Path(workspace) / CONTRACT_DIR / CONTRACT_FILE


def security_probe_path(workspace: Path) -> Path:
    return Path(workspace) / CONTRACT_DIR / SECURITY_PROBE_FILE


def security_report_path(workspace: Path) -> Path:
    return Path(workspace) / CONTRACT_DIR / SECURITY_REPORT_FILE


def contract_instructions(workspace: Path, security_probe: bool) -> str:
    path = contract_path(workspace)
    lines = [
        "ORCHESTRA STRUCTURED COMPLETION CONTRACT (required):",
        f"- Before your final message, write exactly one JSON object to {path}.",
        '- Use {"status":"success"} only if every required operation completed successfully.',
        '- Use {"status":"failure","reason":"..."} if any required operation failed or was denied.',
        "- Do not write success for a partial, denied or unverified task.",
    ]
    if security_probe:
        lines.extend([
            "- Run the trusted security probe exactly as follows:",
            f"  python3 {security_probe_path(workspace)} --worktree {Path(workspace)} "
            f"--report {security_report_path(workspace)}",
            "- The probe must exit 0 and record every required negative assertion as denied.",
            "- Do not edit or bypass the probe.",
        ])
    return "\n".join(lines)


def _read_bounded_regular_file(path: Path, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CompletionContractError("required worker file is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CompletionContractError("required worker file is not a regular file")
    if metadata.st_size <= 0 or metadata.st_size > maximum:
        raise CompletionContractError("required worker file has an invalid size")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CompletionContractError("required worker file cannot be read") from exc


def read_completion_contract(path: Path) -> dict[str, Any]:
    raw = _read_bounded_regular_file(Path(path), MAX_CONTRACT_BYTES)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompletionContractError("completion contract is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) - {"status", "reason"}:
        raise CompletionContractError("completion contract has unsupported fields")
    status = value.get("status")
    if status not in {"success", "failure"}:
        raise CompletionContractError("completion contract status is invalid")
    reason = value.get("reason")
    if reason is not None and (
            not isinstance(reason, str) or len(reason) > 1_000):
        raise CompletionContractError("completion contract reason is invalid")
    return {"status": status, "reason": reason}


def read_security_report(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = _read_bounded_regular_file(Path(path), MAX_SECURITY_REPORT_BYTES)
    except CompletionContractError as exc:
        raise SecurityAssertionError(str(exc)) from exc
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecurityAssertionError("security probe report is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise SecurityAssertionError("security probe report schema is invalid")
    checks = value.get("checks")
    if not isinstance(checks, list):
        raise SecurityAssertionError("security probe checks are missing")
    found: dict[str, dict[str, Any]] = {}
    for check in checks:
        if not isinstance(check, dict):
            raise SecurityAssertionError("security probe check is invalid")
        name = check.get("name")
        if name not in REQUIRED_SECURITY_CHECKS or name in found:
            raise SecurityAssertionError("security probe check name is invalid")
        if check.get("attempted") is not True:
            raise SecurityAssertionError(f"security probe did not attempt {name}")
        if check.get("allowed") is not False:
            raise SecurityAssertionError(f"security probe allowed {name}")
        error_type = check.get("error_type")
        if not isinstance(error_type, str) or not error_type:
            raise SecurityAssertionError(f"security probe has no evidence for {name}")
        found[name] = {
            "name": name,
            "attempted": True,
            "allowed": False,
            "error_type": error_type,
            "command": check.get("command"),
        }
    missing = set(REQUIRED_SECURITY_CHECKS) - set(found)
    if missing:
        raise SecurityAssertionError(
            "security probe is missing checks: " + ", ".join(sorted(missing))
        )
    return found
