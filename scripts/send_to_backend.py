#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a raw Telegram/OpenClaw message to the local Noreña backend."
    )
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--source", default="telegram")
    parser.add_argument("--url", default="http://localhost:8000/v1/process")
    parser.add_argument(
        "--api-key",
        default="a7f3e91b2d5c84e6f0a1b9c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    message = sys.stdin.read()

    payload = json.dumps(
        {
            "message": message,
            "chat_id": args.chat_id,
            "source": args.source,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        args.url,
        data=payload,
        headers={
            "Authorization": f"Bearer {args.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(detail or f"HTTP {exc.code}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print(body)
        return 0

    print(data.get("response") or data.get("detail") or body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
