"""Apply one persisted site policy to Feishu HTTP and child-process WebSockets."""

from lockbot.backend.app.settings.models import SiteSetting
from lockbot.core.platforms.feishu_proxy import configure, http_options, sdk_proxy_override


def restore_proxy(db):
    mode = db.get(SiteSetting, "feishu_proxy_mode")
    url = db.get(SiteSetting, "feishu_proxy_url")
    configure(mode.value if mode and mode.value else "deployment", url.value if url and url.value else "")


def apply_proxy(db):
    from sqlalchemy.orm import sessionmaker

    from lockbot.backend.app.feishu.connectors import connectors
    from lockbot.backend.app.feishu.service import _clients, _clients_lock

    try:
        before = (http_options(), sdk_proxy_override())
    except ValueError:
        before = None  # A valid site policy can recover from an invalid deployment setting.
    restore_proxy(db)
    after = (http_options(), sdk_proxy_override())
    factory = sessionmaker(bind=db.get_bind())
    db.rollback()  # No database transaction spans receiver stop/start or remote I/O.
    if before == after:
        return "unchanged"
    with _clients_lock:
        for client in _clients.values():
            client.reset_transport_cache()
    connectors.reconfigure(factory=factory)
    return "connection_pending"
