"""Browser access to a personal read-only dashboard on a trusted Lockbot origin."""

import asyncio
import ipaddress
import json
import math
import time
from collections import deque
from threading import Event, Lock
from time import monotonic
from urllib.parse import urlsplit

from anyio import CapacityLimiter, to_thread
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .credential_views import CredentialViews
from .handoff import connection_id, connection_instructions, installation, recovery_instructions
from .native import NativeProvider, ProviderError
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
    origin,
    path,
    provider,
    reporters=(),
    *,
    identity_resolver=None,
    identity_directory=None,
    allow_private_http=False,
    metadata_adapter=None,
    metadata_pins=None,
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
    # Reporters and third-party identity providers do not acquire native key
    # authority by returning similarly shaped metadata or display names.
    native_connections = type(provider) is NativeProvider
    credential_views = CredentialViews(sessions) if native_connections else None
    if metadata_adapter is not None:
        from .metadata import pinned_services

        if native_connections or not callable(getattr(metadata_adapter, "describe_connection", None)):
            raise ValueError("Metadata adapter requires a non-native provider and a callable descriptor")
        metadata_pins = pinned_services(list(metadata_pins or {}), metadata_pins, allow_private_http)
    metadata_available = metadata_adapter is not None
    rotation_enabled = native_connections and provider.config.get("allow_credential_rotation") is True
    rotation_links = CredentialViews(sessions, purpose="rotate_link") if rotation_enabled else None
    rotation_intents = CredentialViews(sessions, purpose="rotate_intent") if rotation_enabled else None
    skill = installation(origin) if native_connections else None
    recovery_guide = recovery_instructions(origin, skill) if skill else None

    @app.middleware("http")
    async def protect(request, next_handler):
        if request.headers.get("host") != upstream.netloc or request.url.query:
            return JSONResponse({"detail": "Unexpected host or query"}, status_code=403)
        origin_header = request.headers.get("origin")
        if origin_header is not None and origin_header != origin:
            return JSONResponse({"detail": "Origin mismatch"}, status_code=403)
        browser_post = request.method == "POST" and request.url.path.rsplit("/", 1)[-1] not in (
            "links",
            "credential-view-links",
            "credential-rotation-links",
        )
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
            **(
                {
                    "connection_management": {
                        "version": 2,
                        "metadata_available": True,
                        "credential_disclosure_supported": False,
                        "refresh_semantics": "revalidate_only",
                    }
                }
                if metadata_available
                else {}
            ),
            "protocol": "lockbot.personal-panel.v1",
            "home": origin + "/#/me",
            "issue_path": request.url.path.rsplit("/", 1)[0] + "/links",
            "transport": "https" if secure else "private-http",
            **(
                {
                    "connection_management": {
                        "version": 1,
                        "list_path": request.url.path.rsplit("/", 1)[0] + "/connections",
                        "skill": skill,
                        "recovery_instructions": recovery_guide,
                        "refresh_semantics": "revalidate_only",
                        "credential_disclosure_supported": True,
                        "credential_view": {
                            "version": 1,
                            "purpose": "view_current_key",
                            "issue_path": request.url.path.rsplit("/", 1)[0] + "/credential-view-links",
                            "expires_in": 120,
                            "requires_original_credential": True,
                            "requires_matching_browser_grant": True,
                            "single_use": True,
                        },
                        **(
                            {
                                "credential_rotation": {
                                    "version": 1,
                                    "purpose": "rotate_current_key",
                                    "issue_path": request.url.path.rsplit("/", 1)[0] + "/credential-rotation-links",
                                    "requires_original_credential": True,
                                    "requires_matching_browser_grant": True,
                                    "single_use": True,
                                    "requires_typed_group": True,
                                    "requires_second_confirmation": True,
                                }
                            }
                            if rotation_enabled
                            else {}
                        ),
                    }
                }
                if native_connections
                else {}
            ),
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

    @app.post("/credential-view-links")
    def issue_credential_view(request: Request):
        if not native_connections:
            raise HTTPException(404)
        auth = request.headers.get("authorization", "")
        if request.headers.get("origin") or not auth.startswith("Bearer lbr_") or len(auth) > 8192:
            raise HTTPException(401, "A fresh original binding credential is required")
        with rate_lock:
            now = monotonic()
            while recent and recent[0] < now - 60:
                recent.popleft()
            if len(recent) >= 120:
                raise HTTPException(429, headers={"Retry-After": "60"})
            recent.append(now)
        try:
            grant, pin, expiry = provider.issue_credential_view(auth[7:])
            token, deadline = credential_views.issue(grant, pin, expiry)
        except Exception:
            raise HTTPException(401, "Binding unavailable") from None
        return {
            "view_url": origin + "/#/me?credential_view=" + token,
            "expires_at": deadline,
            "purpose": "view_current_key",
        }

    def inspect_credential_view(request, token, *, consume=False):
        if not native_connections:
            raise HTTPException(404)
        try:
            grant, pin, deadline = credential_views.inspect(token, request.cookies.get(cookie_name), consume=consume)
        except PermissionError:
            raise HTTPException(403, "A matching current personal session is required") from None
        except ValueError:
            raise HTTPException(410, "Key view link expired or used") from None
        if not valid(grant):
            raise HTTPException(403, {"code": "panel_binding_unavailable", "recovery_instructions": recovery_guide})
        return grant, pin, deadline

    @app.post("/credential-view-preview")
    async def preview_credential_view(request: Request):
        data = await fields(request, ["ticket"])
        grant, pin, deadline = inspect_credential_view(request, data["ticket"])
        try:
            provider.credential_view(grant, pin)
            item = provider.connection(grant)
            provider.credential_view(grant, pin)
        except Exception:
            raise HTTPException(
                403, {"code": "panel_binding_unavailable", "recovery_instructions": recovery_guide}
            ) from None
        inspect_credential_view(request, data["ticket"])
        if time.time() >= deadline:
            raise HTTPException(410, "Key view link expired")
        return {
            "connection": item,
            "expires_at": deadline,
            "expires_in": max(0, deadline - time.time()),
            "purpose": "view_current_key",
        }

    @app.post("/credential-view")
    async def reveal_credential(request: Request):
        data = await fields(request, ["ticket", "confirmation"])
        if data["confirmation"] != "view_current_key":
            raise HTTPException(400)
        grant, pin, deadline = inspect_credential_view(request, data["ticket"], consume=True)
        try:
            key = provider.credential_view(grant, pin, reveal=True)
        except ProviderError as exc:
            code = exc.code if exc.code == "credential_recovery_unavailable" else "panel_binding_unavailable"
            raise HTTPException(403, {"code": code, "recovery_instructions": recovery_guide}) from None
        except Exception:
            raise HTTPException(503, "Key view unavailable") from None
        if (
            time.time() >= deadline
            or grant not in sessions.grants(request.cookies.get(cookie_name))
            or not valid(grant)
        ):
            raise HTTPException(403, {"code": "panel_binding_unavailable", "recovery_instructions": recovery_guide})
        # A final current-key pin check also catches replacement during other
        # response checks. The ticket has already been atomically consumed.
        try:
            provider.credential_view(grant, pin)
        except Exception:
            raise HTTPException(403, "Binding changed") from None
        if time.time() >= deadline or grant not in sessions.grants(request.cookies.get(cookie_name)):
            raise HTTPException(403, "Key view authorization expired")
        return {
            "credential": key,
            "expires_at": deadline,
            "expires_in": max(0, deadline - time.time()),
            "purpose": "view_current_key",
        }

    @app.post("/credential-rotation-links")
    def issue_credential_rotation(request: Request):
        if not rotation_enabled:
            raise HTTPException(404)
        auth = request.headers.get("authorization", "")
        if request.headers.get("origin") or not auth.startswith("Bearer lbr_") or len(auth) > 8192:
            raise HTTPException(401, "A fresh original credential is required")
        with rate_lock:
            now = monotonic()
            while recent and recent[0] < now - 60:
                recent.popleft()
            if len(recent) >= 120:
                raise HTTPException(429, headers={"Retry-After": "60"})
            recent.append(now)
        try:
            # This authenticates the original lbr, not a view proof. The new
            # proof is stored in a separate rotate-purpose table.
            grant, pin, expiry = provider.issue_credential_view(auth[7:])
            token, deadline = rotation_links.issue(grant, pin, expiry)
        except Exception:
            raise HTTPException(401, "Binding unavailable") from None
        return {
            "rotation_url": origin + "/#/me?credential_rotation=" + token,
            "expires_at": deadline,
            "purpose": "rotate_current_key",
        }

    def inspect_rotation(request, store, token, *, consume=False):
        if not rotation_enabled:
            raise HTTPException(404)
        try:
            grant, pin, deadline = store.inspect(token, request.cookies.get(cookie_name), consume=consume)
        except PermissionError:
            raise HTTPException(403, "A matching current personal session is required") from None
        except ValueError:
            raise HTTPException(410, "Rotation authorization expired or used") from None
        if not valid(grant) or time.time() >= deadline:
            raise HTTPException(403, "Binding changed or expired")
        return grant, pin, deadline

    @app.post("/credential-rotation-preview")
    async def preview_credential_rotation(request: Request):
        data = await fields(request, ["ticket"])
        grant, pin, deadline = inspect_rotation(request, rotation_links, data["ticket"])
        try:
            provider.credential_view(grant, pin)
            item = provider.connection(grant)
            provider.credential_view(grant, pin)
        except Exception:
            raise HTTPException(403, "Binding changed") from None
        # Turn one fresh link into exactly one server-side intent. The canonical
        # group label and expiry are pinned here, never supplied by the browser.
        inspect_rotation(request, rotation_links, data["ticket"], consume=True)
        payload = json.dumps({"pin": pin, "canonical_name": item["bot"]["name"]})
        try:
            intent, deadline = rotation_intents.issue(grant, payload, deadline)
        except Exception:
            raise HTTPException(410, "Rotation authorization unavailable") from None
        return {
            "intent": intent,
            "connection": item,
            "service_origin": origin,
            "purpose": "rotate_current_key",
            "expires_at": deadline,
            "expires_in": max(0, deadline - time.time()),
            "impact": (
                "All Agents using this identity's current Key for this bot lose access; "
                "other users and bots are unchanged."
            ),
        }

    @app.post("/credential-rotation-confirm")
    async def confirm_credential_rotation(request: Request):
        data = await fields(request, ["intent", "typed_group", "confirmation"])
        grant, payload, deadline = inspect_rotation(request, rotation_intents, data["intent"])
        intent = json.loads(payload)
        if (
            data["typed_group"] != intent["canonical_name"]
            or data["confirmation"] != "invalidate_all_agents_using_this_key"
        ):
            raise HTTPException(400, "Exact group name and second confirmation are required")
        inspect_rotation(request, rotation_intents, data["intent"], consume=True)
        try:
            replacement = provider.rotate_credential(grant, intent["pin"], intent["canonical_name"], deadline)
        except (ProviderError, ValueError):
            raise HTTPException(409, "Rotation authority changed; request fresh proof, do not replay") from None
        except Exception:
            raise HTTPException(503, "Rotation outcome unavailable; do not retry automatically") from None
        # Rotation is durable now. A lost response must not trigger another
        # rotation; the encrypted copy remains recoverable through fresh proof.
        try:
            authorized = bool(provider.authenticate(replacement)) and time.time() < deadline
        except Exception:
            authorized = False
        try:
            authorized = sessions.remove_grant(grant, required_cookie=request.cookies.get(cookie_name)) and authorized
        except Exception:
            raise HTTPException(503, "Rotation outcome unavailable; do not retry automatically") from None
        if not authorized or time.time() >= deadline:
            raise HTTPException(403, "Rotation completed; authorize through IM to recover access, do not replay")
        return {
            "credential": replacement,
            "purpose": "rotated_current_key",
            "requires_fresh_login": True,
            "expires_at": deadline,
            "expires_in": max(0, deadline - time.time()),
        }

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

    @app.get("/connections")
    def connections(request: Request):
        if not native_connections and not metadata_available:
            raise HTTPException(404)

        def unavailable(status):
            return HTTPException(status, {"code": "panel_binding_unavailable", "recovery_instructions": recovery_guide})

        original = sessions.grants(request.cookies.get(cookie_name))
        grants = [grant for grant in original if valid(grant)]
        if not grants:
            raise unavailable(401)
        items = []
        for grant in grants:
            try:
                if metadata_available:
                    from .metadata import project

                    raw = metadata_adapter.describe_connection(dict(grant))
                    if raw is None:
                        continue
                    item = project(raw, grant, metadata_pins)
                else:
                    item = provider.connection(grant)
            except ProviderError:
                raise unavailable(403) from None
            except Exception:
                raise HTTPException(503, {"code": "connection_metadata_unavailable"}) from None
            source = item["service_origin"] if metadata_available else origin
            source_skill = installation(source) if metadata_available else skill
            item.update(id=connection_id(grant), identity=public_identity(grant), service_origin=source)
            item["instructions"] = connection_instructions(source, source_skill, item)
            if metadata_available:
                item["skill"] = source_skill
                item["recovery_instructions"] = recovery_instructions(source, source_skill)
            items.append(item)
        # Logout, expiry or revocation must also win while an earlier connection
        # was being inspected. Do not extend the browser or binding lifetime.
        current = sessions.grants(request.cookies.get(cookie_name))
        if not current:
            raise unavailable(401)
        if any(grant not in current or not valid(grant) for grant in grants):
            raise unavailable(403)
        if metadata_available:
            if not items:
                raise unavailable(403)
            used = {item["service_origin"] for item in items}
            return {
                "schema_version": 2,
                "connections": items,
                "some_authorizations_unavailable": len(items) != len(original),
                "service_pins": [
                    {
                        "origin": source,
                        "instance_id": metadata_pins[source],
                        "service_id": "lockbot.resource-api",
                        "api_version": "1",
                    }
                    for source in sorted(used)
                ],
                "refresh_semantics": "revalidate_only",
            }
        return {
            "schema_version": 1,
            "connections": items,
            "some_authorizations_unavailable": len(grants) != len(original),
            "skill": skill,
            "recovery_instructions": recovery_guide,
            "refresh_semantics": "revalidate_only",
        }

    def read_groups(grant, source):
        try:
            data = source.report(grant)
        except Exception:
            data = [{"label": getattr(source, "label", "资源服务"), "status": "unknown", "allocations": []}]
        if not valid(grant):
            raise HTTPException(403, "Binding changed")
        for group in data:
            if (not native_connections or source is not provider) and isinstance(group.get("allocations"), list):
                group = {
                    **group,
                    "allocations": [
                        {field: value for field, value in row.items() if field != "own_task"}
                        if isinstance(row, dict)
                        else row
                        for row in group["allocations"]
                    ],
                }
            key = (
                id(source),
                grant["issuer"],
                grant["platform"],
                grant["realm"],
                grant["subject"],
                group.get("group_id", group["label"]),
                grant["scope"] if source is provider else "",
            )
            yield (
                key,
                dict(
                    group,
                    identity=public_identity(grant),
                    source={
                        "kind": "identity_provider" if source is provider else "reporter",
                        "label": "登录服务" if source is provider else getattr(source, "label", "外部报表"),
                    },
                ),
            )

    @app.get("/resources")
    def resources(request: Request):
        grants = live(request)
        if not grants:
            raise HTTPException(401)
        groups = {}
        for grant in grants:
            for source in (provider, *reporters):
                for key, group in read_groups(grant, source):
                    groups.setdefault(key, group)
        # Loading a later group can take time. Recheck earlier grants before releasing the combined response.
        if any(not valid(grant) for grant in grants):
            raise HTTPException(403, "Binding changed")
        # Sign-out from another tab takes effect even if a provider call was in flight.
        if not sessions.grants(request.cookies.get(cookie_name)):
            raise HTTPException(401)
        return {"groups": list(groups.values())}

    report_capacity = CapacityLimiter(4)
    report_tasks = set()

    def complete_report(job):
        report_tasks.discard(job)
        if not job.cancelled():
            job.exception()

    @app.get("/resources-stream")
    async def resources_stream(request: Request):
        cookie = request.cookies.get(cookie_name)

        def checked_snapshot():
            original = sessions.grants(cookie)
            return original, [grant for grant in original if valid(grant)]

        original, grants = await to_thread.run_sync(checked_snapshot)
        if not grants:
            raise HTTPException(401)

        def still_authorized():
            return sessions.grants(cookie) == original and all(valid(grant) for grant in grants)

        def line(event, **payload):
            return json.dumps({"event": event, **payload}, ensure_ascii=True) + "\n"

        async def stream():
            jobs = [(grant, source) for grant in grants for source in (provider, *reporters)]
            yield line("start", total=len(jobs))
            pending = {}
            next_index = 0
            deadline = monotonic() + 15
            seen = set()
            stopped = Event()

            def collect(grant, source):
                if stopped.is_set():
                    return []
                return list(read_groups(grant, source))

            try:
                while pending or next_index < len(jobs):
                    while len(pending) < 4 and next_index < len(jobs):
                        grant, source = jobs[next_index]
                        job = asyncio.create_task(to_thread.run_sync(collect, grant, source, limiter=report_capacity))
                        report_tasks.add(job)
                        job.add_done_callback(complete_report)
                        pending[job] = next_index
                        next_index += 1
                    finished, _ = await asyncio.wait(
                        pending, timeout=max(0, deadline - monotonic()), return_when=asyncio.FIRST_COMPLETED
                    )
                    if not finished:
                        yield line("timeout" if await to_thread.run_sync(still_authorized) else "unavailable")
                        return
                    for job in sorted(finished, key=pending.__getitem__):
                        index = pending.pop(job)
                        results = job.result()
                        if not await to_thread.run_sync(still_authorized):
                            yield line("unavailable")
                            return
                        groups = []
                        for key, group in results:
                            if key not in seen:
                                groups.append(group)
                                seen.add(key)
                        yield line("batch", index=index, groups=groups)
                yield line("complete" if await to_thread.run_sync(still_authorized) else "unavailable")
            except Exception:
                yield line("unavailable")
            finally:
                # A synchronous reporter can outlive the client. Keep its quota
                # until the thread exits, and skip jobs still waiting for a slot.
                stopped.set()

        return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no"})

    return app
