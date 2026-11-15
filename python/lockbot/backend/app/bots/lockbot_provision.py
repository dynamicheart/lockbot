#!/usr/bin/env python3
"""Standalone LockBot administrator CLI for previewing and applying bot creation."""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def run(args):
    service = args.service.rstrip("/")
    parts = urlsplit(service)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise ValueError("invalid_service_origin")
    credential = os.environ.get("LOCKBOT_PROVISIONING_KEY", "")
    if not re.fullmatch(r"lbc1\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}", credential):
        raise ValueError("provisioning_key_missing_or_invalid")
    with args.config.open(encoding="utf-8") as source:
        config = json.load(source)
    if not isinstance(config, dict):
        raise ValueError("invalid_bot_config")
    if args.command == "apply":
        payload = {"bot": config, "receipt": args.receipt, "confirmed": True}
    else:
        payload = config
    request = Request(
        f"{service}/api/bot-provisioning/{args.command}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json"},
        method="POST",
    )
    with build_opener(NoRedirects()).open(request, timeout=30) as response:
        result = json.load(response)
    if args.command == "apply":
        path = result["console_path"]
        if not re.fullmatch(r"/bots/[1-9][0-9]*", path):
            raise ValueError("invalid_console_path")
        result["console_url"] = service + path
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preview").add_argument("--config", required=True, type=Path)
    apply = commands.add_parser("apply")
    apply.add_argument("--config", required=True, type=Path)
    apply.add_argument("--receipt", required=True)
    apply.add_argument("--confirm", action="store_true", required=True)
    args = parser.parse_args()
    try:
        result = run(args)
    except HTTPError as exc:
        try:
            code = json.load(exc).get("detail", {}).get("code", "")
        except (ValueError, AttributeError):
            code = ""
        error = {"http_status": exc.code}
        if re.fullmatch(r"(?:bot_provisioning|bot_creation|configuration)_[a-z0-9_]{1,80}", str(code)):
            error["code"] = code
    except (OSError, URLError):
        error = {"code": "input_or_connection_error"}
    except (ValueError, KeyError, TypeError) as exc:
        code = str(exc)
        expected = {
            "invalid_service_origin",
            "provisioning_key_missing_or_invalid",
            "invalid_bot_config",
            "invalid_console_path",
        }
        error = {"code": code if code in expected else "invalid_input_or_response"}
    else:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print(json.dumps(error), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
