"""Private stdio connector for the official SDK's process-owned asyncio loop.

No listening socket, command-line secret or raw-event logging. The parent commits
a durable claim before acknowledging the callback, allowing SDK delivery retries
to use the same deduplication boundary as HTTP.
"""

import json
import sys


def main():
    channel = sys.stdout
    sys.stdout = sys.stderr
    config = json.loads(sys.stdin.buffer.readline(8192))
    # Import only after redirecting SDK output away from the framed event channel.
    import lark_oapi as lark
    import lark_oapi.ws.client as sdk_module

    from lockbot.backend.app.feishu.ws_proxy import configure_sdk_proxy

    def connected():
        channel.write(json.dumps({"status": "connected"}) + "\n")
        channel.flush()

    configure_sdk_proxy(sdk_module, connected=connected, proxy_override=config.get("proxy_url"))

    def received(data):
        encoded = lark.JSON.marshal(data)
        if len(encoded.encode()) > 256 * 1024:
            raise ValueError("Event too large")
        channel.write(json.dumps({"event": json.loads(encoded)}, ensure_ascii=False) + "\n")
        channel.flush()
        response = sys.stdin.buffer.readline(8192)
        if not response or not json.loads(response).get("ack"):
            raise RuntimeError("Event was not durably accepted")

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(received).build()
    client = lark.ws.Client(
        config["app_id"], config["app_secret"], event_handler=handler, log_level=lark.LogLevel.ERROR
    )
    client.start()


if __name__ == "__main__":
    main()
