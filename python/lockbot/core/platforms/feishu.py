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


class FeishuPanelAPIError(FeishuAPIError):
    """A bounded panel attempt failed. Never include a platform response body."""

    def __init__(self, code=None, *, uncertain=False):
        super().__init__("Feishu query panel request failed")
        self.code = code if type(code) is int else None
        self.uncertain = uncertain


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
        self.credential_guard = None
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()
        self._names = OrderedDict()
        self._name_errors = {}
        self._groups = OrderedDict()
        self._names_lock = threading.Lock()

    @property
    def _http_options(self):
        from lockbot.core.platforms.feishu_proxy import http_options

        return http_options()

    def reset_transport_cache(self):
        with self._names_lock:
            self._names.clear()
            self._name_errors.clear()
            self._groups.clear()

    def replace_application_secret(self, secret, *, credential_guard=None):
        """Invalidate this app's cached token, including clients held by running bots."""
        with self._lock:
            self._secret = secret
            if credential_guard is not None:
                self.credential_guard = credential_guard
            self._token = ""
            self._expires = 0.0
        self.reset_transport_cache()

    def _check_credentials(self):
        if self.credential_guard is not None:
            try:
                self.credential_guard()
            except Exception:
                raise FeishuAPIError("Application credentials changed or are unavailable") from None

    def token(self):
        with self._lock:
            self._check_credentials()
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
            self._check_credentials()
            return token

    def _authorized_request(self, method, url, **options):
        token = self.token()
        # Token refresh and lock waits may cross a credential deadline. This check
        # guards the new business request, not the already completed auth request.
        self._check_credentials()
        if self.credential_guard is not None and self._token != token:
            raise FeishuAPIError("Application credentials changed during token acquisition")
        return method(url, headers={"Authorization": "Bearer " + token}, **options)

    def send(self, payload):
        try:
            response = self._authorized_request(
                requests.post,
                API_ROOT + "/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                json=payload,
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

    def _panel_request(self, path, payload, *, create=False):
        """Single attempt, separate from the unchanged ordinary-send path."""
        try:
            request = requests.post if create else requests.put
            response = self._authorized_request(
                request,
                API_ROOT + path,
                params={"receive_id_type": "chat_id"} if create else None,
                json=payload,
                timeout=(3, 8),
                allow_redirects=False,
                **self._http_options,
            )
            data = response.json()
            if not isinstance(data, dict) or type(data.get("code")) is not int:
                raise ValueError()
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            raise FeishuPanelAPIError(uncertain=True) from None
        if response.status_code >= 500 or (response.status_code != 200 and data["code"] == 0):
            raise FeishuPanelAPIError(uncertain=True)
        if response.status_code != 200 or data["code"] != 0:
            raise FeishuPanelAPIError(data["code"])
        return data.get("data")

    def create_query_panel(self, payload):
        """Return only a message reference proven to belong to this app and chat."""
        data = self._panel_request("/im/v1/messages", payload, create=True)
        if not isinstance(data, dict):
            return None  # Accepted, but not safe to adopt: never send a second copy.
        sender = data.get("sender")
        message_id = data.get("message_id")
        if (
            isinstance(message_id, str)
            and re.fullmatch(r"om_[A-Za-z0-9_-]{1,125}", message_id)
            and data.get("chat_id") == payload.get("receive_id")
            and isinstance(sender, dict)
            and sender.get("sender_type") == "app"
            and sender.get("id_type") == "app_id"
            and sender.get("id") == self.app_id
        ):
            return message_id
        return None

    def update_query_panel(self, message_id, payload):
        if not isinstance(message_id, str) or not re.fullmatch(r"om_[A-Za-z0-9_-]{1,125}", message_id):
            raise FeishuPanelAPIError()
        self._panel_request("/im/v1/messages/" + message_id, {"msg_type": "post", "content": payload["content"]})

    def react(self, message_id):
        """One bounded request; caller supplies a trusted inbound message ID."""
        if not isinstance(message_id, str) or not re.fullmatch(r"om_[A-Za-z0-9_-]{1,125}", message_id):
            raise FeishuAPIError("Invalid Feishu reaction target")
        try:
            response = self._authorized_request(
                requests.post,
                API_ROOT + "/im/v1/messages/" + message_id + "/reactions",
                json={"reaction_type": {"emoji_type": "DONE"}},
                timeout=(3, 5),
                allow_redirects=False,
                **self._http_options,
            )
            data = response.json()
            if response.status_code != 200 or data.get("code") != 0:
                raise FeishuAPIError("Feishu reaction request rejected")
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            raise FeishuAPIError("Feishu reaction delivery uncertain") from None

    def bot_identity(self):
        """Discover the application's own bot without listing chats or contacts."""
        try:
            response = self._authorized_request(
                requests.get,
                API_ROOT + "/bot/v3/info/",
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
            response = self._authorized_request(
                requests.get,
                API_ROOT + "/im/v1/chats/" + chat_id,
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

    def cached_user_name(self, open_id):
        """Read an exact cached label only; never issue a contact request."""
        if not isinstance(open_id, str) or not _OPEN_ID.fullmatch(open_id):
            return None
        with self._names_lock:
            cached = self._names.get(open_id)
            if cached and cached[1] > time.monotonic() and cached[0] != open_id:
                return cached[0]
        return None

    def cached_group_name(self, chat_id):
        """Read a selected group's cached label without fetching or listing chats."""
        if not isinstance(chat_id, str) or not _CHAT_ID.fullmatch(chat_id):
            return None
        with self._names_lock:
            cached = self._groups.get(chat_id)
            if cached and cached[2] > time.monotonic() and cached[0]:
                return cached[0]
        return None

    def user_name_states(self, identities):
        """Closed display-only state; raw contact responses never escape this client."""
        now = time.monotonic()
        result = {}
        with self._names_lock:
            for identity in identities:
                cached = self._names.get(identity)
                if cached and cached[1] > now:
                    result[identity] = (
                        {"status": "ready", "name": cached[0]}
                        if cached[0] != identity
                        else {"status": self._name_errors.get(identity, "unavailable")}
                    )
                else:
                    result[identity] = {"status": "unknown"}
        return result

    def warm_user_names(self, identities):
        """One exact-ID request (<=32); only open_id/name enter the bounded cache.

        Official contact/v3/users/batch supports repeated user_ids (up to 50).
        Admission and per-application single-flight belong to the backend batch worker.
        """
        if (
            not isinstance(identities, (list, tuple))
            or len(identities) > 32
            or any(not isinstance(uid, str) or not _OPEN_ID.fullmatch(uid) for uid in identities)
        ):
            raise ValueError("At most 32 exact Feishu open IDs are allowed")
        identities = list(dict.fromkeys(identities))
        states = self.user_name_states(identities)
        missing = [uid for uid in identities if states[uid]["status"] == "unknown"]
        if not missing:
            return
        names, failure = {}, "unavailable"
        try:
            response = self._authorized_request(
                requests.get,
                API_ROOT + "/contact/v3/users/batch",
                params=[("user_id_type", "open_id"), *[("user_ids", uid) for uid in missing]],
                timeout=(2, 4),
                allow_redirects=False,
                **self._http_options,
            )
            if response.status_code == 403:
                failure = "permission"
            body = response.json()
            if response.status_code == 200 and isinstance(body, dict) and body.get("code") == 0:
                items = body.get("data", {}).get("items", [])
                for item in items[:50] if isinstance(items, list) else []:
                    if not isinstance(item, dict):
                        continue
                    uid, name = item.get("open_id"), item.get("name")
                    if uid in missing and isinstance(name, str) and name.strip():
                        names[uid] = name.strip()[:80]
        except requests.Timeout:
            failure = "timeout"
        except (requests.RequestException, FeishuAPIError, ValueError, TypeError, AttributeError):
            pass
        now = time.monotonic()
        with self._names_lock:
            for uid in missing:
                # A concurrent successful command-side warm must not be replaced by failure.
                previous = self._names.get(uid)
                if uid not in names and previous and previous[1] > now and previous[0] != uid:
                    continue
                self._names[uid] = (names.get(uid, uid), now + (21600 if uid in names else 300))
                self._names.move_to_end(uid)
                if uid in names:
                    self._name_errors.pop(uid, None)
                else:
                    self._name_errors[uid] = failure
            while len(self._names) > 1024:
                evicted, _ = self._names.popitem(last=False)
                self._name_errors.pop(evicted, None)

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
            response = self._authorized_request(
                requests.get,
                API_ROOT + "/contact/v3/users/" + open_id,
                params={"user_id_type": "open_id"},
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
                evicted, _ = self._names.popitem(last=False)
                self._name_errors.pop(evicted, None)

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

    def build_api_notification(self, content, group_id=None):
        """Compact API-only notices without changing ordinary resource-message spacing."""
        reply = self.build_reply(content, [], group_id=group_id)
        post = json.loads(reply["content"])
        rows = post["zh_cn"]["content"]
        # Each rich-text row is already a paragraph. Legacy notification blank lines
        # otherwise become visible space-only paragraphs, including trailing padding.
        post["zh_cn"]["content"] = [
            row for row in rows if any(node.get("tag") != "text" or node.get("text", "").strip() for node in row)
        ] or [[{"tag": "text", "text": " "}]]
        reply["content"] = json.dumps(post, ensure_ascii=False)
        return reply

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

    def query_panel_payload(self, msg):
        """A panel must fit one ordinary rich-text message; otherwise keep normal splitting."""
        client = self._get("_FEISHU_CLIENT")
        if not isinstance(client, FeishuClient) or not isinstance(msg, dict):
            return None
        group = msg.get("receive_id")
        if group not in self._get("_AUTHORIZED_GROUP_IDS", []) or not _CHAT_ID.fullmatch(str(group)):
            raise FeishuAPIError("Feishu reply group is not authorized")
        try:
            rows = json.loads(msg["content"])["zh_cn"]["content"]
            if msg.get("msg_type") != "post" or not rows:
                return None
            if sum(len(json.dumps(row, ensure_ascii=False).encode()) for row in rows) > 16000:
                return None
        except (ValueError, KeyError, TypeError):
            return None
        payload = dict(msg, uuid=str(uuid.uuid4()))
        payload["content"] = json.dumps({"zh_cn": {"title": "", "content": rows}}, ensure_ascii=False)
        return client, payload

    def send_command_reply(self, reply, message, result):
        """Optional chat-only acknowledgement, never a change to API notifications."""
        eligible = (
            self._get("FEISHU_REACTION_REPLIES", False) is True
            and result.committed
            and result.reaction_safe
            and result.operation in {"lock", "slock", "unlock", "alias"}
            and message.get("group_id") in self._get("_AUTHORIZED_GROUP_IDS", [])
            and reply.get("receive_id") == message.get("group_id")
        )
        client = self._get("_FEISHU_CLIENT")
        if eligible and isinstance(client, FeishuClient):
            try:
                client.react(message.get("message_id"))
                return [(200, "reaction_accepted")]
            except FeishuAPIError:
                # The allocation is never replayed. Fall back to the unchanged reply,
                # even when the reaction response was ambiguous.
                import logging

                logging.getLogger(__name__).warning("Feishu reaction unavailable; using normal command reply")
        return self.send(reply)

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
