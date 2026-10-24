#!/usr/bin/env python3
"""Run the disposable adapter against a real authority Unix socket.

This helper is used only by the throwaway Linux boundary test.  The server
authenticates this process from its Unix peer credentials; no role or identity
is accepted on the command line.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from orchestra.authority import execution_grant_from_dict, task_spec_from_dict
from orchestra.authority_client import AuthorityClient
from orchestra.coding_worker_adapter import DisposableCodingWorkerAdapter


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--grant", required=True)
    args = parser.parse_args()

    task = task_spec_from_dict(json.loads(Path(args.task).read_text()))
    grant = execution_grant_from_dict(json.loads(Path(args.grant).read_text()))
    adapter = DisposableCodingWorkerAdapter(
        Path(args.state_root), AuthorityClient(args.socket),
    )
    receipt = adapter.dispatch(task, grant)
    deadline = time.monotonic() + task.resource_limits.max_runtime_seconds + 3
    while time.monotonic() < deadline:
        status = adapter.status(receipt)
        if status.state in {"succeeded", "failed", "cancelled", "unknown"}:
            report = adapter.collect(receipt)
            print(json.dumps({
                "receipt": receipt.to_dict(),
                "status": status.to_dict(),
                "report": report.to_dict(),
                "completion_verified": report.completion_verified,
            }, sort_keys=True))
            return 0
        time.sleep(0.02)
    adapter.cancel(receipt, "boundary probe timeout")
    raise RuntimeError("disposable adapter did not reach a terminal state")


if __name__ == "__main__":
    raise SystemExit(main())
