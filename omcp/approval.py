"""Local, interactive human approval for tasks submitted through OMCP."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from omcp.controller import Config
from omcp.local_control import approval_request


def format_review(details: dict[str, object], decision: str) -> str:
    return "\n".join([
        "OMCP LOCAL APPROVAL REVIEW",
        "==========================",
        f"Task ID:      {details['task_id']}",
        f"Request ID:   {details['request_id']}",
        f"Submitted by: {details['submitted_by']}",
        f"Worker:       {details['worker']}",
        f"Profile:      {details['repository_profile']}",
        f"Repository:   {details['repository']}",
        f"Base ref:     {details['base_ref']}",
        f"Consequence:  {details['consequence_class']}",
        f"Requested:    {details['requested_at']}",
        f"Expires:      {details['expires_at']}",
        f"Decision:     {decision.upper()}",
        f"Title:        {details['title']}",
        "Instructions:",
        str(details["instructions"]),
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Approve or reject an OMCP task locally")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--decision", choices=("approve", "reject"), required=True)
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise PermissionError("human approval requires an interactive local terminal")
    config = Config.load(args.config)
    details = approval_request(config.state_dir, {
        "action": "review", "task_id": args.task_id,
    })
    print(format_review(details, args.decision))
    expected = f"{args.decision.upper()} {args.task_id}"
    confirmation = input(f'Type "{expected}" to continue: ').strip()
    if confirmation != expected:
        raise PermissionError("approval confirmation did not match")
    reason = input("Human approval reason: ").strip()
    if not reason:
        raise ValueError("a non-empty approval reason is required")
    result = approval_request(config.state_dir, {
        "action": "resolve", "task_id": args.task_id,
        "decision": args.decision, "reason": reason,
        "approver_identity": "local:" + getpass.getuser(),
    })
    print(f"{result['task_id']}: {result['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
