"""Browser access to a personal read-only dashboard on a trusted Lockbot origin."""

import ipaddress
import json
import math
import time
from collections import deque
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .sessions import PanelSessions

COOKIE = "__Host-lockbot_me"
PUBLIC_FIELDS = ("issuer", "platform", "realm", "subject", "scope")
PRINCIPAL_FIELDS = ("issuer", "platform", "realm", "subject")


def public_identity(grant):
    return {field: grant[field] for field in PUBLIC_FIELDS}


def same_person_groups(identities, resolver, directory):
    """Advisory directory equality for already-authorized, distinct IM principals.

    Keep directory subjects ephemeral and private. Ambiguous collections, stale
    assertions and resolver failures leave that principal's person unknown.
    """
    if resolver is None or len(identities) < 2:
        return []
    results = []
    for identity in identities:
        try:
            # A plugin cannot accidentally mutate browser identities or grants.
            result = resolver.resolve({field: identity[field] for field in PRINCIPAL_FIELDS})
            results.append(dict(result) if isinstance(result, dict) else None)
        except Exception:
            results.append(None)
    matched = {}
    now = time.time()
    for index, result in enumerate(results):
        if not isinstance(result, dict) or set(result) != {"directory", "subject", "expires_at"}:
            continue
        expiry = result["expires_at"]
        if (
            result["directory"] != directory
            or not isinstance(result["subject"], str)
            or not result["subject"].strip()
            or isinstance(expiry, bool)
            or not isinstance(expiry, (int, float))
        ):
            continue
        try:
            if not math.isfinite(expiry) or expiry <= now:
                continue
        except OverflowError:
            continue
        matched.setdefault(result["subject"], []).append(index)
    return [indices for indices in matched.values() if len(indices) >= 2]


async def fields(request, allowed):
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415)
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 4096:
            raise HTTPException(413)
        data.extend(chunk)
    try:
        obj = json.loads(data)
    except (UnicodeError, ValueError):
        raise HTTPException(400, "Invalid JSON") from None
    if not isinstance(obj, dict) or set(obj) != set(allowed) or not all(isinstance(v, str) for v in obj.values()):
        raise HTTPException(400, "Invalid fields")
    return obj


def panel_origin(origin, allow_private_http=False):
    upstream = urlsplit(origin)
    private_http = False
    if allow_private_http is True and upstream.scheme == "http":
        try:
            address = ipaddress.ip_address(upstream.hostname)
            private_http = any(
                address in ipaddress.ip_network(network)
                for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "fc00::/7", "::1/128")
            )
        except ValueError:
            pass
    if (
        (upstream.scheme != "https" and not private_http)
        or not upstream.hostname
        or upstream.username
        or upstream.password
        or upstream.path
        or upstream.query
        or upstream.fragment
    ):
        raise ValueError("Configure an approved HTTPS origin or explicitly allow a private HTTP IP")
    return upstream


def create_panel(
    origin, path, provider, reporters=(), *, identity_resolver=None, identity_directory=None, allow_private_http=False
):
    upstream = panel_origin(origin, allow_private_http)
    secure = upstream.scheme == "https"
    cookie_name = COOKIE if secure else "lockbot_me_http"
    if identity_resolver is not None and (not isinstance(identity_directory, str) or not identity_directory.strip()):
        raise ValueError("Identity resolver requires a pinned directory")
    sessions = PanelSessions(path)
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    recent = deque()
    rate_lock = Lock()

    @app.middleware("http")
    async def protect(request, next_handler):
        if request.headers.get("host") != upstream.netloc or request.url.query:
            return JSONResponse({"detail": "Unexpected host or query"}, status_code=403)
        origin_header = request.headers.get("origin")
        if origin_header is not None and origin_header != origin:
            return JSONResponse({"detail": "Origin mismatch"}, status_code=403)
        browser_post = request.method == "POST" and not request.url.path.endswith("/links")
        if browser_post and request.headers.get("origin") != origin:
            return JSONResponse({"detail": "Origin mismatch"}, status_code=403)
        response = await next_handler(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": (
                    "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
                ),
            }
        )
        return response

    def valid(grant):
        # A source failure must not grant access or disclose the upstream exception.
        try:
            return bool(provider.validate(grant))
        except Exception:
            raise HTTPException(503, "Identity provider unavailable") from None

    def live(request):
        return [g for g in sessions.grants(request.cookies.get(cookie_name)) if valid(g)]

    @app.get("/capabilities")
    def capabilities(request: Request):
        return {
            "protocol": "lockbot.personal-panel.v1",
            "home": origin + "/#/me",
            "issue_path": request.url.path.rsplit("/", 1)[0] + "/links",
            "transport": "https" if secure else "private-http",
        }

    @app.post("/links")
    def issue(request: Request):
        auth = request.headers.get("authorization", "")
        if request.headers.get("origin") or not auth.startswith("Bearer ") or not 0 < len(auth[7:]) <= 8192:
            raise HTTPException(401, "A bound agent credential is required")
        with rate_lock:
            now = monotonic()
            while recent and recent[0] < now - 60:
                recent.popleft()
            if len(recent) >= 120:
                raise HTTPException(429, headers={"Retry-After": "60"})
            recent.append(now)
        try:
            grant = provider.authenticate(auth[7:])
        except Exception:
            raise HTTPException(401, "Binding unavailable") from None
        if not grant or not valid(grant):
            raise HTTPException(401, "Binding unavailable")
        token = sessions.issue(grant)
        return dict(home=origin + "/#/me", login_url=origin + "/#/me?ticket=" + token, expires_in=120)

    def inspect_ticket(token):
        try:
            grant = sessions.peek(token)
        except ValueError:
            raise HTTPException(410, "Login link expired or used") from None
        if not valid(grant):
            raise HTTPException(403, "Binding expired or revoked")
        return grant

    @app.post("/preview")
    async def preview(request: Request):
        data = await fields(request, ["ticket"])
        return {"identity": public_identity(inspect_ticket(data["ticket"]))}

    @app.post("/login")
    async def login(request: Request):
        data = await fields(request, ["ticket", "mode"])
        inspect_ticket(data["ticket"])
        if data["mode"] not in ("append", "replace"):
            raise HTTPException(400)
        if data["mode"] == "append" and not live(request):
            raise HTTPException(401)
        try:
            cookie = sessions.consume(data["ticket"], request.cookies.get(cookie_name), mode=data["mode"])
        except ValueError:
            raise HTTPException(410, "Login link or session invalid") from None
        response = JSONResponse({"authenticated": True})
        response.set_cookie(
            cookie_name,
            cookie,
            max_age=30 * 86400,
            httponly=True,
            secure=secure,
            samesite="strict",
            path="/" if secure else request.url.path.rsplit("/", 1)[0],
        )
        return response

    @app.get("/session")
    def session(request: Request):
        identities = {}
        grants = live(request)
        for grant in grants:
            principal = tuple(grant[field] for field in PRINCIPAL_FIELDS)
            if principal not in identities:
                identity = public_identity(grant)
                identity["scopes"] = []
                identities[principal] = identity
            scopes = identities[principal]["scopes"]
            if grant["scope"] not in scopes:
                scopes.append(grant["scope"])
        identities = list(identities.values())
        groups = same_person_groups(identities, identity_resolver, identity_directory)
        if identity_resolver is not None and len(identities) >= 2:
            # Directory calls may be slow; they must not retain revoked access.
            if any(not valid(grant) for grant in grants):
                raise HTTPException(403, "Binding changed")
            if not sessions.grants(request.cookies.get(cookie_name)):
                raise HTTPException(401)
        return {"authenticated": bool(identities), "identities": identities, "same_person_groups": groups}

    @app.post("/logout")
    async def logout(request: Request):
        await fields(request, [])
        sessions.logout(request.cookies.get(cookie_name))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(
            cookie_name,
            path="/" if secure else request.url.path.rsplit("/", 1)[0],
            secure=secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/resources")
    def resources(request: Request):
        grants = live(request)
        if not grants:
            raise HTTPException(401)
        groups = []
        seen = set()
        for grant in grants:
            for source in (provider, *reporters):
                try:
                    data = source.report(grant)
                except Exception:
                    data = [{"label": getattr(source, "label", "资源服务"), "status": "unknown", "allocations": []}]
                if not valid(grant):
                    raise HTTPException(403, "Binding changed")
                for group in data:
                    who = public_identity(grant)
                    key = (
                        id(source),
                        grant["issuer"],
                        grant["platform"],
                        grant["realm"],
                        grant["subject"],
                        group.get("group_id", group["label"]),
                        grant["scope"] if source is provider else "",
                    )
                    if key not in seen:
                        groups.append(dict(group, identity=who))
                        seen.add(key)
        # Loading a later group can take time. Recheck earlier grants before releasing the combined response.
        if any(not valid(grant) for grant in grants):
            raise HTTPException(403, "Binding changed")
        # Sign-out from another tab takes effect even if a provider call was in flight.
        if not sessions.grants(request.cookies.get(cookie_name)):
            raise HTTPException(401)
        return {"groups": groups}

    return app
