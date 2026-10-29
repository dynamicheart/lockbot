"""Feishu rich-text transport. Authentication and group routing belong to ingress.

Only structured fragments create links or mentions. User prose is never interpreted
as HTML/Markdown, so task notes cannot inject a mention or redirect a reply.
"""

import copy
import json
import re
import threading
import time
import uuid
from collections import OrderedDict

import requests

from lockbot.core.message_adapter import MessageAdapter

API_ROOT = "https://open.feishu.cn/open-apis"
_OPEN_ID = re.compile(r"ou_[A-Za-z0-9_-]+\Z")
_CHAT_ID = re.compile(r"oc_[A-Za-z0-9_-]+\Z")


class FeishuAPIError(RuntimeError):
    """Deliberately contains no response body, credential or private message text."""


def decorate_people(value, config):
    """Add cached display labels to detached snapshots without changing stable IDs."""
    client = config.get_val("_FEISHU_CLIENT") if config else None
    if not isinstance(client, FeishuClient):
        return

    def walk(item):
        if isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, dict):
            for key, children in item.items():
                if key in {"holders", "current_users", "booking_list"} and isinstance(children, list):
                    for person in children:
                        if not isinstance(person, dict):
                            continue
                        uid = person.get("external_user_id") or person.get("user_id")
                        if isinstance(uid, str):
                            label = client.display_text(uid)
                            if label != uid:
                                person["display_name"] = label
                walk(children)

    walk(value)


class FeishuClient:
    """One client per enterprise app; serialize token refresh without retrying sends."""

    def __init__(self, app_id, app_secret):
        self.app_id = app_id
        self._secret = app_secret
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()
        self._names = OrderedDict()
        self._groups = OrderedDict()
        self._names_lock = threading.Lock()

    @property
    def _http_options(self):
        from lockbot.core.platforms.feishu_proxy import http_options

        return http_options()

    def reset_transport_cache(self):
        with self._names_lock:
            self._names.clear()
            self._groups.clear()

    def token(self):
        with self._lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            if not self.app_id or not self._secret:
                raise FeishuAPIError("Feishu application credentials are missing")
            try:
                response = requests.post(
                    API_ROOT + "/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.app_id, "app_secret": self._secret},
                    timeout=(3, 8),
                    allow_redirects=False,
                    **self._http_options,
                )
                data = response.json()
                if response.status_code != 200 or data.get("code") != 0:
                    raise FeishuAPIError("Feishu token request rejected")
                token = data["tenant_access_token"]
                expiry = int(data["expire"])
                if not isinstance(token, str) or not token or expiry <= 60:
                    raise ValueError("invalid token response")
            except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
                raise FeishuAPIError("Feishu token request failed") from None
            self._token, self._expires = token, time.monotonic() + expiry - 60
            return token

    def send(self, payload):
        try:
            response = requests.post(
                API_ROOT + "/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                json=payload,
                headers={"Authorization": "Bearer " + self.token()},
                timeout=(3, 8),
                allow_redirects=False,
                **self._http_options,
            )
            data = response.json()
            if response.status_code != 200 or data.get("code") != 0:
                raise FeishuAPIError("Feishu message request rejected")
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            # Never replay an ambiguous send automatically: the command may already have run.
            raise FeishuAPIError("Feishu message delivery uncertain") from None
        return [(200, "platform_accepted")]

    def bot_identity(self):
        """Discover the application's own bot without listing chats or contacts."""
        try:
            response = requests.get(
                API_ROOT + "/bot/v3/info/",
                headers={"Authorization": "Bearer " + self.token()},
                timeout=(3, 8),
                allow_redirects=False,
                **self._http_options,
            )
            data = response.json()
            open_id = data.get("bot", {}).get("open_id", "")
            if response.status_code != 200 or data.get("code") != 0 or not _OPEN_ID.fullmatch(open_id):
                raise FeishuAPIError("Feishu bot identity could not be verified")
            return open_id
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            raise FeishuAPIError("Feishu bot identity request failed") from None

    def group_name(self, chat_id, refresh=False):
        """Read only a selected group's display name; never enumerate groups or members."""
        if not _CHAT_ID.fullmatch(chat_id):
            raise ValueError("Invalid Feishu group ID")
        with self._names_lock:
            cached = self._groups.get(chat_id)
            if not refresh and cached and cached[2] > time.monotonic():
                return {"name": cached[0], "status": cached[1]}
        name, status, ttl = "", "unavailable", 300
        try:
            response = requests.get(
                API_ROOT + "/im/v1/chats/" + chat_id,
                headers={"Authorization": "Bearer " + self.token()},
                timeout=(3, 5),
                allow_redirects=False,
                **self._http_options,
            )
            data = response.json()
            candidate = data.get("data", {}).get("name")
            if response.status_code == 200 and data.get("code") == 0 and isinstance(candidate, str):
                name = "".join(char for char in candidate if char.isprintable())[:256].strip()
                status, ttl = ("ok", 21600) if name else ("unavailable", 300)
            elif data.get("code") in {99991672, 99991679}:
                status = "permission"
        except (requests.RequestException, ValueError, TypeError, AttributeError, FeishuAPIError):
            pass
        with self._names_lock:
            # Keep a previously known name on transient failures and name changes only affect display.
            old = self._groups.get(chat_id)
            name = name or (old[0] if old else "")
            self._groups[chat_id] = (name, status, time.monotonic() + ttl)
            self._groups.move_to_end(chat_id)
            while len(self._groups) > 256:
                self._groups.popitem(last=False)
        return {"name": name, "status": status}

    def warm_user_name(self, open_id):
        """Optional display lookup, outside resource locks; identity never uses the name."""
        if not _OPEN_ID.fullmatch(open_id):
            return
        with self._names_lock:
            cached = self._names.get(open_id)
            if cached and cached[1] > time.monotonic():
                return
        name, ttl = open_id, 300
        try:
            response = requests.get(
                API_ROOT + "/contact/v3/users/" + open_id,
                params={"user_id_type": "open_id"},
                headers={"Authorization": "Bearer " + self.token()},
                timeout=(3, 5),
                allow_redirects=False,
                **self._http_options,
            )
            data = response.json()
            candidate = data.get("data", {}).get("user", {}).get("name")
            if (
                response.status_code == 200
                and data.get("code") == 0
                and isinstance(candidate, str)
                and candidate.strip()
            ):
                name, ttl = candidate.strip()[:80], 21600
        except (requests.RequestException, FeishuAPIError, ValueError, AttributeError, TypeError):
            pass
        with self._names_lock:
            previous = self._names.get(open_id)
            if name == open_id and previous and previous[0] != open_id:
                name = previous[0]
            self._names[open_id] = (name, time.monotonic() + ttl)
            self._names.move_to_end(open_id)
            while len(self._names) > 1024:
                self._names.popitem(last=False)

    def display_text(self, text):
        with self._names_lock:
            names = {key: val[0] for key, val in self._names.items()}
        return re.sub(r"\bou_[A-Za-z0-9_-]+\b", lambda match: names.get(match[0], match[0]), text)


class FeishuAdapter(MessageAdapter):
    platform_name = "feishu"

    def __init__(self, config=None):
        self.config = config

    def _get(self, key, default=None):
        return self.config.get_val(key, default) if self.config else default

    def verify_request(self, signature, **kwargs):
        # Feishu uses the dedicated raw-body verifier, never the Infoflow endpoint.
        return False

    def decrypt_payload(self, encrypted_data, **kwargs):
        return None

    def extract_command(self, msg_data):
        """Only consume an already verified, normalized ingress envelope."""
        return msg_data["user_id"], msg_data["group_id"], msg_data["command"]

    def build_reply(self, content, user_ids, group_id=None):
        if isinstance(group_id, (list, tuple)):
            if len(group_id) != 1:
                raise ValueError("A Feishu resource bot must target exactly one group")
            group_id = group_id[0]
        rows = [[]]
        for item in [content] if isinstance(content, str) else content:
            if isinstance(item, str):
                client = self._get("_FEISHU_CLIENT")
                if isinstance(client, FeishuClient):
                    item = client.display_text(item)
                for index, line in enumerate(item.split("\n")):
                    if index:
                        rows.append([])
                    if line:
                        rows[-1].append({"tag": "text", "text": line})
            elif isinstance(item, tuple) and len(item) == 2:
                label, url = item
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    raise ValueError("Feishu links must use HTTP(S)")
                rows[-1].extend([{"tag": "text", "text": str(label)}, {"tag": "a", "text": url, "href": url}])
                rows.append([])
            else:
                raise ValueError("Unsupported Feishu reply fragment")
        for person in dict.fromkeys(user_ids):
            if _OPEN_ID.fullmatch(str(person)):
                rows[-1].append({"tag": "at", "user_id": str(person)})
        return {
            "receive_id": group_id or "",
            "msg_type": "post",
            "content": json.dumps(
                {"zh_cn": {"title": "", "content": [row or [{"tag": "text", "text": " "}] for row in rows]}},
                ensure_ascii=False,
            ),
            "uuid": str(uuid.uuid4()),
        }

    def apply_reply_target(self, msg, conversation_id):
        if isinstance(conversation_id, (list, tuple)):
            if len(conversation_id) != 1:
                raise ValueError("A Feishu resource bot must target exactly one group")
            conversation_id = conversation_id[0]
        if conversation_id:
            if not _CHAT_ID.fullmatch(str(conversation_id)):
                raise ValueError("Invalid Feishu group")
            msg["receive_id"] = str(conversation_id)
        return msg

    def extract_reply_text(self, msg):
        if isinstance(msg, str):
            return msg
        if not isinstance(msg, dict):
            return ""
        rows = json.loads(msg.get("content", "{}")).get("zh_cn", {}).get("content", [])
        lines = ["".join(node.get("text", "") for node in row if node.get("tag") != "at") for row in rows]
        return "\n".join("" if line == " " else line for line in lines)

    def send(self, msg):
        client = self._get("_FEISHU_CLIENT")
        if not isinstance(client, FeishuClient):
            raise FeishuAPIError("Feishu application is not configured")
        payload = copy.deepcopy(msg)
        if payload.get("receive_id"):
            groups = [payload["receive_id"]]
        else:
            groups = self._get("_AUTHORIZED_GROUP_IDS", [])
        allowed = self._get("_AUTHORIZED_GROUP_IDS", [])
        if len(groups) != 1 or groups[0] not in allowed or not _CHAT_ID.fullmatch(groups[0]):
            raise FeishuAPIError("Feishu reply group is not authorized")
        payload["receive_id"] = groups[0]
        # Split by complete rich-text rows; never truncate links or mention identities.
        post = json.loads(payload["content"])
        rows = post["zh_cn"]["content"]
        chunks, current, size = [], [], 0
        for row in rows:
            row_size = len(json.dumps(row, ensure_ascii=False).encode())
            if row_size > 16000:
                raise FeishuAPIError("Feishu reply row exceeds message limit")
            if current and size + row_size > 16000:
                chunks.append(current)
                current, size = [], 0
            current.append(row)
            size += row_size
        if current:
            chunks.append(current)
        if len(chunks) > 10:
            raise FeishuAPIError("Feishu reply exceeds message limit")
        results = []
        for chunk in chunks:
            part = dict(payload, uuid=str(uuid.uuid4()))
            part["content"] = json.dumps({"zh_cn": {"title": "", "content": chunk}}, ensure_ascii=False)
            results.extend(client.send(part))
        return results

    def send_broadcast(self, content, group_id, mention_all=False):
        if not _CHAT_ID.fullmatch(group_id) or group_id not in self._get("_AUTHORIZED_GROUP_IDS", []):
            return "skipped", "invalid_group"
        reply = self.build_reply(content, [], group_id)
        if mention_all:
            post = json.loads(reply["content"])
            post["zh_cn"]["content"][-1].append({"tag": "at", "user_id": "all"})
            reply["content"] = json.dumps(post, ensure_ascii=False)
        try:
            self.send(reply)
        except FeishuAPIError:
            return "unknown", "delivery_uncertain"
        return "accepted", "platform_accepted"
