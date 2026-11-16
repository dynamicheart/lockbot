#!/usr/bin/env python3
"""Standalone LockBot administrator CLI for previewing and applying bot creation."""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def save_review(path, service, config_digest, result):
    record = {"service": service, "config_digest": config_digest, "preview": result}
    content = json.dumps(record, ensure_ascii=False)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def load_review(path, service, config_digest):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), encoding="utf-8") as source:
        mode = os.fstat(source.fileno()).st_mode
        if not stat.S_ISREG(mode) or (os.name != "nt" and mode & 0o077):
            raise ValueError("receipt_file_not_private")
        record = json.load(source)
    if record["service"] != service or record["config_digest"] != config_digest:
        raise ValueError("receipt_file_configuration_mismatch")
    return record["preview"]


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
    with args.config.open(encoding="utf-8") as source:
        config = json.load(source)
    if not isinstance(config, dict):
        raise ValueError("invalid_bot_config")
    config_digest = hashlib.sha256(json.dumps(config, separators=(",", ":")).encode()).hexdigest()
    if args.command == "review":
        preview = load_review(args.receipt_file, service, config_digest)
        return {
            **{key: value for key, value in preview.items() if key != "receipt"},
            "expired": preview["expires_at"] <= time.time(),
        }
    credential = os.environ.get("LOCKBOT_PROVISIONING_KEY", "")
    if not re.fullmatch(r"lbc1\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}", credential):
        raise ValueError("provisioning_key_missing_or_invalid")
    if args.command == "apply":
        receipt = (
            load_review(args.receipt_file, service, config_digest)["receipt"] if args.receipt_file else args.receipt
        )
        payload = {"bot": config, "receipt": receipt, "confirmed": True}
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
    if args.command == "preview" and args.receipt_file:
        save_review(args.receipt_file, service, config_digest, result)
        result.pop("receipt")
    if args.command == "apply":
        path = result["console_path"]
        if not re.fullmatch(r"/#/bots/[1-9][0-9]*", path):
            raise ValueError("invalid_console_path")
        result["console_url"] = service + path
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview")
    preview.add_argument("--config", required=True, type=Path)
    preview.add_argument("--receipt-file", type=Path)
    review = commands.add_parser("review")
    review.add_argument("--config", required=True, type=Path)
    review.add_argument("--receipt-file", type=Path, required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--config", required=True, type=Path)
    receipt = apply.add_mutually_exclusive_group(required=True)
    receipt.add_argument("--receipt")
    receipt.add_argument("--receipt-file", type=Path)
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
            "receipt_file_not_private",
            "receipt_file_configuration_mismatch",
        }
        error = {"code": code if code in expected else "invalid_input_or_response"}
    else:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print(json.dumps(error), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
