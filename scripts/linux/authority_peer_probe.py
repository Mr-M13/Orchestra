#!/usr/bin/env python3
"""Run one authenticated authority request as the invoking OS identity.

This probe is used by the disposable Linux boundary test.  It prints a single
JSON object and never accepts or sends caller-supplied identity fields.
"""

from __future__ import annotations

import argparse
import json
import sys

from orchestra.authority_client import AuthorityClient, AuthorityClientError


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--payload", default="{}")
    parser.add_argument("--request-id", default=None)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.payload)
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        client = AuthorityClient(args.socket, timeout=10)
        result = client.call(
            args.operation, payload, request_id=args.request_id,
        )
    except AuthorityClientError as error:
        print(json.dumps({
            "ok": False,
            "error": {"code": error.code, "message": error.message},
            "request_id": error.request_id,
        }, sort_keys=True))
        return 0
    except Exception as error:  # noqa: BLE001 - probe must emit machine JSON
        print(json.dumps({
            "ok": False,
            "error": {"code": "probe_error", "message": f"{type(error).__name__}: {error}"},
        }, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, "result": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
