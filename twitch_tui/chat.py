"""Live Twitch IRC Chat client over TLS with IRCv3 tags, badges, user colors, and OAuth chat sending."""

from __future__ import annotations

import hashlib
import random
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


FALLBACK_USER_COLORS = [
    "#ff79c6",
    "#bd93f9",
    "#8be9fd",
    "#50fa7b",
    "#ffb86c",
    "#f1fa8c",
    "#ff5555",
    "#00ff7f",
    "#00bfff",
    "#da70d6",
    "#ff69b4",
    "#ffd700",
    "#7fffd4",
    "#ff8c00",
]


def _default_color_for_user(username: str) -> str:
    digest = hashlib.md5(username.lower().encode("utf-8")).digest()
    idx = digest[0] % len(FALLBACK_USER_COLORS)
    return FALLBACK_USER_COLORS[idx]


@dataclass
class ChatMessage:
    username: str
    display_name: str
    text: str
    color: str
    badges: list[str] = field(default_factory=list)
    timestamp: str = ""
    is_system: bool = False
    is_self: bool = False
    is_action: bool = False


def _parse_irc_tags(tag_str: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    if not tag_str.startswith("@"):
        return tags
    raw = tag_str[1:]
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            tags[k] = (
                v.replace("\\s", " ")
                .replace("\\n", "")
                .replace("\\r", "")
                .replace("\\:", ";")
                .replace("\\\\", "\\")
            )
        else:
            tags[part] = ""
    return tags


def _extract_badges(badge_str: str) -> list[str]:
    if not badge_str:
        return []
    icons: list[str] = []
    for item in badge_str.split(","):
        name = item.split("/", 1)[0]
        if name == "broadcaster":
            icons.append("📺")
        elif name == "moderator":
            icons.append("⚔")
        elif name == "vip":
            icons.append("💎")
        elif name in ("subscriber", "founder"):
            icons.append("★")
        elif name in ("premium", "turbo", "partner"):
            icons.append("👑")
    return icons


class TwitchChatClient:
    """Asynchronous background IRCv3 client for Twitch chat."""

    def __init__(
        self,
        oauth_token: str = "",
        username: str = "",
        max_messages: int = 160,
        on_update: Callable[[], None] | None = None,
    ) -> None:
        self.oauth_token = self._clean_token(oauth_token)
        self.username = username.strip().lower()
        self.max_messages = max_messages
        self.on_update = on_update

        self._lock = threading.Lock()
        self._messages: list[ChatMessage] = []
        self._current_channel: str = ""
        self._desired_channel: str = ""
        self._connected: bool = False
        self._authenticated: bool = False
        self._running: bool = False
        self._sock: ssl.SSLSocket | None = None
        self._thread: threading.Thread | None = None

    @staticmethod
    def _clean_token(token: str) -> str:
        t = token.strip()
        if t.lower().startswith("oauth:"):
            t = t[6:].strip()
        return t

    @property
    def current_channel(self) -> str:
        with self._lock:
            return self._current_channel

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def can_send(self) -> bool:
        return bool(self.oauth_token and self.username)

    def update_credentials(self, oauth_token: str, username: str) -> None:
        clean_tok = self._clean_token(oauth_token)
        clean_user = username.strip().lower()
        if clean_tok == self.oauth_token and clean_user == self.username:
            return
        self.oauth_token = clean_tok
        self.username = clean_user
        self._reconnect()

    def get_messages(self) -> list[ChatMessage]:
        with self._lock:
            return list(self._messages)

    def add_system_message(self, text: str) -> None:
        now = time.strftime("%H:%M")
        msg = ChatMessage(
            username="system",
            display_name="Twitch",
            text=text,
            color="#6272a4",
            timestamp=now,
            is_system=True,
        )
        with self._lock:
            self._messages.append(msg)
            if len(self._messages) > self.max_messages:
                self._messages = self._messages[-self.max_messages :]
        if self.on_update:
            try:
                self.on_update()
            except Exception:
                pass

    def join_channel(self, channel: str) -> None:
        target = channel.strip().lower().lstrip("#")
        if not target:
            return
        with self._lock:
            if self._desired_channel == target and self._current_channel == target:
                return
            prev = self._current_channel
            self._desired_channel = target
            self._messages.clear()

        self.add_system_message(f"Connecting to #{target} chat…")

        if not self._running:
            self.start()
            return

        sock = self._sock
        if sock and self._connected:
            try:
                if prev and prev != target:
                    sock.sendall(f"PART #{prev}\r\n".encode("utf-8"))
                sock.sendall(f"JOIN #{target}\r\n".encode("utf-8"))
                with self._lock:
                    self._current_channel = target
            except Exception:
                self._reconnect()

    def send_message(self, text: str) -> tuple[bool, str]:
        msg_text = text.strip()
        if not msg_text:
            return False, "Empty message"
        if not self.can_send:
            return (
                False,
                "OAuth token required to chat. Press 'A' or run: twitch-tui auth <token>",
            )
        sock = self._sock
        channel = self.current_channel
        if not sock or not self._connected or not channel:
            return False, "Not connected to a channel chat"

        try:
            sock.sendall(f"PRIVMSG #{channel} :{msg_text}\r\n".encode("utf-8"))
            now = time.strftime("%H:%M")
            own_msg = ChatMessage(
                username=self.username,
                display_name=self.username,
                text=msg_text,
                color="#50fa7b",
                badges=["💬"],
                timestamp=now,
                is_self=True,
            )
            with self._lock:
                self._messages.append(own_msg)
                if len(self._messages) > self.max_messages:
                    self._messages = self._messages[-self.max_messages :]
            if self.on_update:
                self.on_update()
            return True, "Sent"
        except Exception as exc:
            return False, f"Send failed: {exc}"

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        sock = self._sock
        self._sock = None
        if sock:
            try:
                sock.close()
            except Exception:
                pass

    def _reconnect(self) -> None:
        sock = self._sock
        self._sock = None
        if sock:
            try:
                sock.close()
            except Exception:
                pass

    def _run_loop(self) -> None:
        while self._running:
            with self._lock:
                target = self._desired_channel
            if not target:
                time.sleep(0.25)
                continue

            try:
                ctx = ssl.create_default_context()
                raw_sock = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=8.0)
                sock = ctx.wrap_socket(raw_sock, server_hostname="irc.chat.twitch.tv")
                sock.settimeout(1.0)
                self._sock = sock

                sock.sendall(b"CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")
                if self.oauth_token and self.username:
                    sock.sendall(f"PASS oauth:{self.oauth_token}\r\n".encode("utf-8"))
                    sock.sendall(f"NICK {self.username}\r\n".encode("utf-8"))
                    self._authenticated = True
                else:
                    anon_nick = f"justinfan{random.randint(10000, 99999)}"
                    sock.sendall(b"PASS SCHMOOPIIE\r\n")
                    sock.sendall(f"NICK {anon_nick}\r\n".encode("utf-8"))
                    self._authenticated = False

                with self._lock:
                    target = self._desired_channel
                    self._current_channel = target
                    self._connected = True

                if target:
                    sock.sendall(f"JOIN #{target}\r\n".encode("utf-8"))
                    mode_label = f"as @{self.username}" if self._authenticated else "in lurk mode"
                    self.add_system_message(f"Joined #{target} ({mode_label})")

                buffer = ""
                while self._running and self._sock is sock:
                    try:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        buffer += chunk.decode("utf-8", errors="ignore")
                        while "\r\n" in buffer:
                            line, buffer = buffer.split("\r\n", 1)
                            self._handle_irc_line(sock, line)
                    except socket.timeout:
                        # Check if desired channel changed while idle
                        with self._lock:
                            desired = self._desired_channel
                            curr = self._current_channel
                        if desired and desired != curr:
                            if curr:
                                sock.sendall(f"PART #{curr}\r\n".encode("utf-8"))
                            sock.sendall(f"JOIN #{desired}\r\n".encode("utf-8"))
                            with self._lock:
                                self._current_channel = desired
                            self.add_system_message(f"Joined #{desired}")
                        continue
            except Exception:
                pass
            finally:
                with self._lock:
                    self._connected = False
                if self._running:
                    time.sleep(2.0)

    def _handle_irc_line(self, sock: ssl.SSLSocket, line: str) -> None:
        if not line:
            return
        if line.startswith("PING"):
            payload = line.split(" ", 1)[1] if " " in line else ":tmi.twitch.tv"
            try:
                sock.sendall(f"PONG {payload}\r\n".encode("utf-8"))
            except Exception:
                pass
            return

        tags: dict[str, str] = {}
        rest = line
        if rest.startswith("@"):
            parts = rest.split(" ", 1)
            tags = _parse_irc_tags(parts[0])
            rest = parts[1] if len(parts) > 1 else ""

        if " PRIVMSG " in rest:
            prefix, msg_part = rest.split(" PRIVMSG ", 1)
            login = prefix.lstrip(":").split("!", 1)[0]
            if " :" not in msg_part:
                return
            _, text = msg_part.split(" :", 1)

            is_action = False
            if text.startswith("\x01ACTION ") and text.endswith("\x01"):
                text = text[8:-1]
                is_action = True

            display_name = tags.get("display-name") or login
            color = tags.get("color") or _default_color_for_user(login)
            badges = _extract_badges(tags.get("badges", ""))
            now = time.strftime("%H:%M")

            msg = ChatMessage(
                username=login,
                display_name=display_name,
                text=text,
                color=color,
                badges=badges,
                timestamp=now,
                is_action=is_action,
            )
            with self._lock:
                self._messages.append(msg)
                if len(self._messages) > self.max_messages:
                    self._messages = self._messages[-self.max_messages :]
            if self.on_update:
                try:
                    self.on_update()
                except Exception:
                    pass

        elif " USERNOTICE " in rest:
            sys_msg = tags.get("system-msg", "").strip()
            if sys_msg:
                self.add_system_message(f"🎉 {sys_msg}")
        elif " NOTICE " in rest and "Login authentication failed" in rest:
            self.add_system_message("⚠ OAuth login failed — check your token.")
