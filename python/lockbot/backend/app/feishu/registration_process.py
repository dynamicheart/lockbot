"""Run the SDK's creation-only flow with process-local proxy and private stdio."""

import asyncio
import json
import os
import sys
from urllib.parse import urlsplit

from lockbot.core.platforms.feishu_proxy import validate_proxy_url

REGISTRATION_TIMEOUT = 600
_FEISHU_CONFIRMATION_HOSTS = {"accounts.feishu.cn", "applink.feishu.cn", "open.feishu.cn"}
_GROUP_SCOPES = ["im:message.group_at_msg:readonly", "im:message:send_as_bot", "im:chat:read"]
_PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


def configure_registration_proxy(override):
    if override is None:
        return
    proxy = validate_proxy_url(override)
    for name in _PROXY_VARIABLES:
        os.environ.pop(name, None)
        os.environ.pop(name.upper(), None)
    if proxy:
        os.environ["HTTPS_PROXY"] = proxy


def confirmation_url(value):
    if not isinstance(value, str) or not 0 < len(value) <= 8192 or any(ord(char) <= 32 for char in value):
        raise ValueError("Invalid Feishu confirmation URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _FEISHU_CONFIRMATION_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Invalid Feishu confirmation URL")
    return value


async def register(name, send):
    import lark_oapi
    from lark_oapi.scene.registration.errors import AppAccessDeniedError, AppExpiredError

    def verification_ready(result):
        expiry = result["expire_in"]
        if type(expiry) is not int or expiry <= 0:
            raise ValueError("Invalid Feishu device-flow expiry")
        send(
            {
                "status": "waiting",
                "url": confirmation_url(result["url"]),
                "expires_in": min(expiry, REGISTRATION_TIMEOUT),
            }
        )

    def status_changed(result):
        if result["status"] == "domain_switched":
            raise ValueError("Confirm the application in a Feishu tenant")

    try:
        result = await asyncio.wait_for(
            lark_oapi.aregister_app(
                on_qr_code=verification_ready,
                on_status_change=status_changed,
                source="lockbot",
                app_preset={"name": name},
                create_only=True,
                addons={
                    "preset": False,
                    "scopes": {"tenant": _GROUP_SCOPES},
                    "events": {"items": {"tenant": ["im.message.receive_v1"]}},
                },
            ),
            timeout=REGISTRATION_TIMEOUT,
        )
    except (asyncio.TimeoutError, AppExpiredError):
        send({"status": "expired"})
        return
    except AppAccessDeniedError:
        send({"status": "denied"})
        return
    if (result.get("user_info") or {}).get("tenant_brand") not in (None, "feishu"):
        raise ValueError("Confirm the application in a Feishu tenant")
    app_id, app_secret = result["client_id"], result["client_secret"]
    if not isinstance(app_id, str) or not isinstance(app_secret, str) or not app_id or not app_secret:
        raise ValueError("Invalid Feishu registration credentials")
    send({"status": "confirmed", "app_id": app_id, "app_secret": app_secret})


def main():
    channel, stderr = sys.stdout, sys.stderr

    def send(message):
        channel.write(json.dumps(message) + "\n")
        channel.flush()

    with open(os.devnull, "w") as quiet:
        sys.stdout = sys.stderr = quiet
        try:
            config = json.loads(sys.stdin.buffer.readline(8192))
            configure_registration_proxy(config.get("proxy_url"))
            asyncio.run(register(config["name"], send))
        except Exception:
            send({"status": "failed"})
        finally:
            sys.stdout = channel
            sys.stderr = stderr


if __name__ == "__main__":
    main()
