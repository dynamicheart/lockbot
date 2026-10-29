"""Scoped read-only Prometheus query gateway; independent of the Lockbot backend."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jwt

METRICS = frozenset(
    {
        "lockbot_accelerator_utilization_percent",
        "lockbot_accelerator_memory_used_bytes",
        "lockbot_accelerator_memory_total_bytes",
        "lockbot_accelerator_process_info",
        "lockbot_accelerator_process_info_truncated",
        "lockbot_host_cpu_utilization_percent",
        "lockbot_host_memory_used_bytes",
        "lockbot_host_memory_total_bytes",
        "lockbot_host_filesystem_used_bytes",
        "lockbot_host_filesystem_total_bytes",
        "lockbot_sample_timestamp_seconds",
        "lockbot_collector_success",
    }
)
MAX_BODY, MAX_RESPONSE, MAX_SERIES = 32768, 2 * 1024 * 1024, 4096
JSON_STRING = r'"(?:[^"\\]|\\.)*"'
LABELS = {
    "__name__",
    "node",
    "device",
    "vendor",
    "pid",
    "container_id",
    "container_name",
    "user",
    "command",
    "filesystem",
    "collector",
}


class Rejected(Exception):
    def __init__(self, status, code, retry=0):
        self.status, self.code, self.retry = status, code, retry


def regex_escape(value):
    return re.sub(r"([.*+?^${}()|\[\]\\])", r"\\\1", value)


def regex_members(expression, allowed):
    if not expression.startswith("^(?:") or not expression.endswith(")$"):
        raise Rejected(403, "query_not_allowed")
    lookup = {regex_escape(value): value for value in allowed}
    parts = expression[4:-2].split("|")
    if not parts or len(parts) > 64 or any(part not in lookup for part in parts):
        raise Rejected(403, "query_not_allowed")
    return {lookup[part] for part in parts}


def validate_query(query, allowed_nodes):
    if not isinstance(query, str) or len(query) > 16384:
        raise Rejected(400, "invalid_query")
    batch = re.fullmatch(r"\{__name__=~(" + JSON_STRING + r"),node=~(" + JSON_STRING + r")\}", query)
    single = re.fullmatch(
        r"([a-zA-Z_:][a-zA-Z0-9_:]*)\{node=~(" + JSON_STRING + r")(?:,collector=(" + JSON_STRING + r"))?\}", query
    )
    try:
        if batch:
            names = regex_members(json.loads(batch[1]), METRICS)
            nodes = regex_members(json.loads(batch[2]), allowed_nodes)
        elif single and single[1] in METRICS:
            names = {single[1]}
            nodes = regex_members(json.loads(single[2]), allowed_nodes)
            if single[3] and json.loads(single[3]) not in {"host", "accelerator"}:
                raise Rejected(403, "query_not_allowed")
        else:
            raise Rejected(403, "query_not_allowed")
    except (ValueError, TypeError):
        raise Rejected(400, "invalid_query") from None
    return names, nodes


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class Gateway:
    def __init__(self, config, *, clock=time.time):
        self.config, self.clock = config, clock
        self.lock = threading.Lock()
        self.budgets, self.cache = OrderedDict(), OrderedDict()
        endpoint = urllib.parse.urlsplit(config.get("upstream", "http://127.0.0.1:19090"))
        if (
            endpoint.scheme != "http"
            or endpoint.hostname not in {"127.0.0.1", "::1", "localhost"}
            or (
                endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
                or endpoint.path not in {"", "/"}
            )
        ):
            raise ValueError("upstream must be a fixed loopback Prometheus address")
        self.endpoint = config.get("upstream", "http://127.0.0.1:19090").rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def reader(self, authorization):
        if not authorization.startswith("Bearer ") or not 32 <= len(authorization[7:]) <= 16384:
            raise Rejected(401, "invalid_datasource_credential")
        token = authorization[7:]
        if token.count(".") == 2:
            return self.grant_reader(token)
        if token.startswith(("lbr_", "lbk_", "eyJ")):
            raise Rejected(401, "invalid_datasource_credential")
        digest = hashlib.sha256(token.encode()).hexdigest()
        for item in self.config.get("readers", [])[:64]:
            if hmac.compare_digest(digest, item.get("sha256", "")) and item.get("nodes"):
                return digest, frozenset(item["nodes"])
        raise Rejected(401, "invalid_datasource_credential")

    def grant_reader(self, token):
        try:
            unverified = jwt.decode(token, options={"verify_signature": False}, algorithms=["HS256"])
            issuer = unverified.get("iss")
            if not isinstance(issuer, str) or issuer not in self.config.get("issuers", {}):
                raise ValueError
            source = self.config["issuers"][issuer]
            path = Path(source["key_file"])
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or not 32 <= info.st_size <= 4096:
                raise ValueError
            if info.st_uid not in {0, os.geteuid()}:
                raise ValueError
            audience = self.config.get("audience")
            if not isinstance(audience, str) or not audience or audience.endswith("/"):
                raise ValueError
            claims = jwt.decode(
                token,
                path.read_bytes(),
                algorithms=["HS256"],
                issuer=issuer,
                audience=audience,
                leeway=15,
                options={"require": ["iss", "sub", "aud", "iat", "exp", "nodes"]},
            )
            if claims["aud"] != audience or claims["iss"] != issuer or not isinstance(claims["sub"], str):
                raise ValueError
            if type(claims["iat"]) is not int or type(claims["exp"]) is not int:
                raise ValueError
            if not 0 < claims["exp"] - claims["iat"] <= 300:
                raise ValueError
            if claims["iat"] > self.clock() + 15 or claims["exp"] < self.clock() - 15:
                raise ValueError
            if "nbf" in claims and (type(claims["nbf"]) is not int or claims["nbf"] > claims["exp"]):
                raise ValueError
            if "nbf" in claims and claims["nbf"] > self.clock() + 15:
                raise ValueError
            nodes = claims["nodes"]
            if (
                not isinstance(nodes, list)
                or not 1 <= len(nodes) <= 512
                or any(not isinstance(node, str) or not 1 <= len(node) <= 256 for node in nodes)
                or len(nodes) != len(set(nodes))
            ):
                raise ValueError
            allowed = source.get("bots", {}).get(claims["sub"], [])
            if not allowed or not set(nodes).issubset(allowed):
                raise Rejected(403, "datasource_scope_denied")
            return "grant:" + issuer + ":" + claims["sub"], frozenset(nodes)
        except (OSError, KeyError, ValueError, TypeError, OverflowError, RecursionError, jwt.PyJWTError):
            raise Rejected(401, "invalid_datasource_grant") from None

    def budget(self, reader):
        now = self.clock()
        with self.lock:
            for key, maximum in (("global", 120), (reader, 30)):
                entries = self.budgets.setdefault(key, deque())
                while entries and entries[0] <= now - 60:
                    entries.popleft()
                if len(entries) >= maximum:
                    raise Rejected(429, "rate_limited", 60)
            self.budgets["global"].append(now)
            self.budgets[reader].append(now)
            while len(self.budgets) > 65:
                self.budgets.popitem(last=False)

    def query(self, authorization, body):
        reader, allowed_nodes = self.reader(authorization)
        if not isinstance(body, bytes) or len(body) > MAX_BODY:
            raise Rejected(413, "body_too_large")
        try:
            pairs = urllib.parse.parse_qsl(
                body.decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=3
            )
        except (ValueError, UnicodeError):
            raise Rejected(400, "invalid_parameters") from None
        params = dict(pairs)
        if len(params) != len(pairs) or set(params) - {"query", "time", "timeout"} or not params.get("query"):
            raise Rejected(400, "invalid_parameters")
        if params.get("timeout", "4s") != "4s":
            raise Rejected(400, "invalid_timeout")
        try:
            evaluation = float(params.get("time", self.clock()))
            if not math.isfinite(evaluation) or abs(evaluation - self.clock()) > 300:
                raise ValueError
        except ValueError:
            raise Rejected(400, "invalid_time") from None
        names, nodes = validate_query(params["query"], allowed_nodes)
        self.budget(reader)
        key = (reader, tuple(sorted(allowed_nodes)), params["query"], int(evaluation // 15))
        with self.lock:
            cached = self.cache.get(key)
            if cached and self.clock() - cached[0] < 15:
                return cached[1]
        payload = urllib.parse.urlencode({"query": params["query"], "time": str(evaluation), "timeout": "4s"}).encode()
        request = urllib.request.Request(
            self.endpoint + "/api/v1/query",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=4.5) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise Rejected(502, "upstream_too_large")
            result = self.filter_response(json.loads(raw), names, nodes)
        except (OSError, ValueError, urllib.error.URLError):
            raise Rejected(502, "datasource_unavailable") from None
        with self.lock:
            self.cache[key] = (self.clock(), result)
            self.cache.move_to_end(key)
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
        return result

    @staticmethod
    def filter_response(body, names, nodes):
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            raise Rejected(502, "invalid_upstream_response")
        result = body["data"].get("result")
        if (
            body.get("status") != "success"
            or body.get("data", {}).get("resultType") != "vector"
            or (not isinstance(result, list) or len(result) > MAX_SERIES)
        ):
            raise Rejected(502, "invalid_upstream_response")
        cleaned = []
        for item in result:
            if not isinstance(item, dict) or not isinstance(item.get("metric"), dict):
                raise Rejected(502, "invalid_upstream_response")
            metric = item["metric"]
            if metric.get("node") not in nodes or metric.get("__name__") not in names:
                raise Rejected(502, "invalid_upstream_scope")
            if not isinstance(item.get("value"), list) or len(item["value"]) != 2:
                raise Rejected(502, "invalid_upstream_response")
            try:
                timestamp, value = item["value"]
                if not isinstance(timestamp, int | float) or not math.isfinite(timestamp):
                    raise ValueError
                if not isinstance(value, str) or len(value) > 64 or not math.isfinite(float(value)):
                    continue
                if any(not isinstance(value, str) or len(value) > 256 for value in metric.values()):
                    raise ValueError
            except (ValueError, TypeError):
                raise Rejected(502, "invalid_upstream_response") from None
            cleaned.append(
                {"metric": {key: value for key, value in metric.items() if key in LABELS}, "value": item["value"]}
            )
        return json.dumps(
            {"status": "success", "data": {"resultType": "vector", "result": cleaned}}, separators=(",", ":")
        ).encode()


class BoundedHTTPServer(ThreadingHTTPServer):
    """Bound concurrent sockets, including clients that never finish request headers."""

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_server(config_path, listen, port):
    def load():
        raw = Path(config_path).read_bytes()
        if len(raw) > 65536:
            raise ValueError("configuration too large")
        return json.loads(raw)

    gateway = Gateway(load())

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def respond(self, status, body=b"", retry=0):
            self.send_response(status)
            origin = self.headers.get("Origin")
            if origin and origin in gateway.config.get("origins", []):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Expose-Headers", "Retry-After")
            self.send_header("Vary", "Origin")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if retry:
                self.send_header("Retry-After", str(retry))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            if self.path != "/api/v1/query" or self.headers.get("Origin") not in gateway.config.get("origins", []):
                self.respond(403)
                return
            self.respond(204)

        def do_POST(self):
            try:
                if self.path != "/api/v1/query":
                    raise Rejected(404, "not_found")
                try:
                    gateway.config = load()  # Apply reader revocation and reduced scope before any cache lookup.
                except (OSError, ValueError):
                    raise Rejected(503, "datasource_unavailable") from None
                origin = self.headers.get("Origin")
                if origin and origin not in gateway.config.get("origins", []):
                    raise Rejected(403, "origin_not_allowed")
                if len(self.headers.get_all("Authorization", [])) != 1:
                    raise Rejected(401, "invalid_datasource_credential")
                gateway.reader(self.headers.get("Authorization", ""))
                if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                    raise Rejected(400, "invalid_parameters")
                if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                    raise Rejected(415, "unsupported_media_type")
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    raise Rejected(400, "invalid_parameters") from None
                if not 0 <= length <= MAX_BODY:
                    raise Rejected(413, "body_too_large")
                self.connection.settimeout(5)
                self.respond(200, gateway.query(self.headers.get("Authorization", ""), self.rfile.read(length)))
            except Rejected as error:
                self.respond(error.status, json.dumps({"error": error.code}).encode(), error.retry)
            except (OSError, ValueError):
                self.respond(400, b'{"error":"invalid_request"}')

        def do_GET(self):
            self.respond(404)

        def log_message(self, *_args):
            return

    return BoundedHTTPServer((listen, port), Handler)


def serve(config_path, listen, port):
    server = make_server(config_path, listen, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19091)
    args = parser.parse_args()
    serve(args.config, args.listen, args.port)


if __name__ == "__main__":
    main()
