"""Twitch GraphQL API client, hybrid OAuth/Device-Flow config, and Streamlink + MPV IPC player controller."""

from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
# Public web Client-ID used by Twitch web player & streamlink
TWITCH_PUBLIC_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
# Official Twitch TV/Device Client-ID supporting OAuth Device Code flow
TWITCH_DEVICE_CLIENT_ID = "ue6666qo983tsx6so1t0vnawi233wa"

CONFIG_DIR = Path.home() / ".config" / "twitch-tui"
CONFIG_PATH = CONFIG_DIR / "config.json"
MPV_SOCKET_PATH = Path("/tmp/twitch-tui-mpv.sock")
PLAYER_STATE_PATH = Path("/tmp/twitch-tui-player.json")
LIVE_CACHE_PATH = Path("/tmp/twitch-tui-live-state.json")

QUALITY_OPTIONS = ["best", "1080p60", "720p60", "480p", "360p", "audio_only"]

DEFAULT_FOLLOWED = [
    "dario_moccia_twitch",
    "tumblurr",
    "pow3rtv",
    "sabaku_no_sutoriimaa",
    "ydot",
    "shroud",
    "zackrawrr",
    "riotgames",
    "caedrel",
    "piratesoftware",
]


@dataclass
class ChannelInfo:
    login: str
    display_name: str
    is_live: bool = False
    title: str = ""
    game_name: str = ""
    viewers_count: int = 0
    started_at: str = ""
    preview_url: str = ""
    avatar_url: str = ""
    followers_count: int = 0
    description: str = ""
    is_followed: bool = False

    @property
    def uptime_str(self) -> str:
        if not self.is_live or not self.started_at:
            return ""
        try:
            dt = datetime.datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
            now = datetime.datetime.now(datetime.timezone.utc)
            secs = max(0, int((now - dt).total_seconds()))
            hrs, rem = divmod(secs, 3600)
            mins, _ = divmod(rem, 60)
            if hrs > 0:
                return f"{hrs}h {mins:02d}m"
            return f"{mins}m"
        except Exception:
            return ""

    @property
    def best_image_url(self) -> str:
        if self.is_live and self.preview_url:
            sep = "&" if "?" in self.preview_url else "?"
            return f"{self.preview_url}{sep}t={int(time.time() // 180)}"
        return self.avatar_url


@dataclass
class GameCategory:
    id: str
    name: str
    viewers_count: int = 0
    box_art_url: str = ""


@dataclass
class VodInfo:
    id: str
    title: str
    channel_login: str
    channel_name: str
    length_seconds: int = 0
    view_count: int = 0
    published_at: str = ""
    game_name: str = ""
    preview_url: str = ""

    @property
    def duration_str(self) -> str:
        secs = max(0, int(self.length_seconds))
        hrs, rem = divmod(secs, 3600)
        mins, s = divmod(rem, 60)
        if hrs > 0:
            return f"{hrs:d}:{mins:02d}:{s:02d}"
        return f"{mins:02d}:{s:02d}"

    @property
    def date_str(self) -> str:
        if not self.published_at:
            return ""
        return self.published_at[:10]


class StreamPlayer:
    """Manages Streamlink + MPV playback with UNIX socket IPC control and live Ad-Break detection."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[Any] | None = None
        self._last_error: str = ""
        self._ad_until: float = 0.0
        self._in_preroll: bool = False

    def _send_mpv_ipc(self, command: list[Any]) -> dict[str, Any] | None:
        if not MPV_SOCKET_PATH.exists():
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.35)
                s.connect(str(MPV_SOCKET_PATH))
                payload = json.dumps({"command": command}) + "\n"
                s.sendall(payload.encode("utf-8"))
                resp = s.recv(4096).decode("utf-8", errors="ignore")
                for line in resp.splitlines():
                    if line.strip():
                        return json.loads(line)
        except Exception:
            return None
        return None

    def is_mpv_ready(self) -> bool:
        resp = self._send_mpv_ipc(["get_property", "pid"])
        return bool(resp and resp.get("error") == "success")

    def get_mpv_pid(self) -> int | None:
        resp = self._send_mpv_ipc(["get_property", "pid"])
        if resp and resp.get("error") == "success" and resp.get("data"):
            try:
                return int(resp["data"])
            except Exception:
                return None
        return None

    def is_playing(self) -> bool:
        if self.is_mpv_ready():
            return True
        if self._proc and self._proc.poll() is None:
            return True
        return False

    def pop_last_error(self) -> str:
        if self._proc and self._proc.poll() is not None and self._proc.returncode != 0:
            if not self.is_mpv_ready():
                self._proc = None
                err = self._last_error or "Streamlink terminato inaspettatamente."
                self._last_error = ""
                return err
        err = self._last_error
        self._last_error = ""
        return err

    def get_ad_remaining(self) -> int:
        rem = int(round(self._ad_until - time.time()))
        if rem > 0:
            return rem
        self._in_preroll = False
        return 0

    def get_state(self) -> dict[str, Any] | None:
        mpv_ready = self.is_mpv_ready()
        proc_alive = bool(self._proc and self._proc.poll() is None)
        if not mpv_ready and not proc_alive:
            return None

        ad_rem = self.get_ad_remaining()
        info: dict[str, Any] = {
            "channel": "",
            "login": "",
            "title": "",
            "quality": "best",
            "audio_only": False,
            "paused": False,
            "muted": False,
            "volume": 100,
            "starting": not mpv_ready,
            "ad_remaining": ad_rem,
        }
        if PLAYER_STATE_PATH.exists():
            try:
                info.update(json.loads(PLAYER_STATE_PATH.read_text(encoding="utf-8")))
                info["starting"] = not mpv_ready
                info["ad_remaining"] = ad_rem
            except Exception:
                pass

        if mpv_ready:
            pause_resp = self._send_mpv_ipc(["get_property", "pause"])
            if pause_resp and pause_resp.get("error") == "success":
                info["paused"] = bool(pause_resp.get("data", False))

            mute_resp = self._send_mpv_ipc(["get_property", "mute"])
            if mute_resp and mute_resp.get("error") == "success":
                info["muted"] = bool(mute_resp.get("data", False))

            vol_resp = self._send_mpv_ipc(["get_property", "volume"])
            if vol_resp and vol_resp.get("error") == "success":
                try:
                    info["volume"] = int(round(float(vol_resp.get("data", 100))))
                except Exception:
                    pass

        return info

    def stop(self) -> None:
        self._ad_until = 0.0
        self._in_preroll = False
        self._send_mpv_ipc(["quit"])
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None
        for p in (MPV_SOCKET_PATH, PLAYER_STATE_PATH):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass

    def toggle_pause(self) -> bool:
        resp = self._send_mpv_ipc(["cycle", "pause"])
        return bool(resp and resp.get("error") == "success")

    def toggle_mute(self) -> bool:
        resp = self._send_mpv_ipc(["cycle", "mute"])
        return bool(resp and resp.get("error") == "success")

    def adjust_volume(self, delta: int) -> int | None:
        state = self.get_state()
        if not state or state.get("starting"):
            return None
        new_vol = max(0, min(130, int(state.get("volume", 100)) + delta))
        self._send_mpv_ipc(["set_property", "volume", new_vol])
        return new_vol

    def watch_stream(
        self,
        channel_or_url: str,
        title: str = "",
        quality: str = "best",
        low_latency: bool = True,
        oauth_token: str = "",
    ) -> tuple[bool, str]:
        self.stop()

        if channel_or_url.startswith("http://") or channel_or_url.startswith("https://"):
            url = channel_or_url
            login = channel_or_url.rstrip("/").split("/")[-1].lower()
            label = title or login
        else:
            login = channel_or_url.strip().lstrip("#").lower()
            url = f"https://www.twitch.tv/{login}"
            label = title or login

        audio_only = quality == "audio_only"
        win_title = f"Twitch: {label}"

        mpv_args = [
            f"--input-ipc-server={MPV_SOCKET_PATH}",
            "--wayland-app-id=twitch-mpv",
            f"--title={win_title}",
            f"--force-media-title={label}",
            "--really-quiet",
        ]
        if audio_only:
            mpv_args.extend(["--no-video", "--force-window=no"])
        else:
            mpv_args.extend(
                [
                    "--force-window=immediate",
                    "--keep-open=yes",
                    "--cache=yes",
                    "--demuxer-max-bytes=64MiB",
                ]
            )

        streamlink_bin = shutil.which("streamlink")
        mpv_bin = shutil.which("mpv")
        if not mpv_bin:
            return False, "mpv non trovato nel sistema"

        if streamlink_bin:
            cmd = [
                streamlink_bin,
                url,
                f"{quality},best" if quality != "best" else "best",
                "--player",
                mpv_bin,
                "--player-args",
                shlex.join(mpv_args),
                "--twitch-disable-ads",
                "--hls-segment-queue-threshold=0",
            ]
            if low_latency and "/videos/" not in url:
                cmd.append("--twitch-low-latency")
        else:
            cmd = [mpv_bin, *mpv_args, url]

        try:
            self._ad_until = 0.0
            self._in_preroll = False
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
                bufsize=1,
            )
            threading.Thread(
                target=self._monitor_streamlink_output,
                args=(self._proc,),
                daemon=True,
            ).start()
            state_data = {
                "channel": channel_or_url,
                "login": login,
                "title": label,
                "quality": quality,
                "audio_only": audio_only,
                "started_at": time.time(),
            }
            PLAYER_STATE_PATH.write_text(json.dumps(state_data), encoding="utf-8")
            mode_tag = "Solo Audio" if audio_only else quality
            return True, f"⏳ Connessione a {label} [{mode_tag}] su MPV…"
        except Exception as exc:
            return False, f"Errore avvio MPV: {exc}"

    def _monitor_streamlink_output(self, proc: subprocess.Popen[Any]) -> None:
        """Reads Streamlink logs in background to detect Twitch Ad breaks and show OSD/TUI countdowns."""
        if not proc.stdout:
            return
        try:
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                if "Waiting for pre-roll ads to finish" in line:
                    self._in_preroll = True
                    if self._ad_until < time.time() + 5:
                        self._ad_until = time.time() + 20
                    self._send_mpv_ipc(
                        [
                            "show-text",
                            "📺 Pubblicità iniziale Twitch in corso — Streamlink sta saltando lo spot, lo stream partirà a breve...",
                            20000,
                        ]
                    )
                elif "Detected advertisement break of " in line:
                    m = re.search(r"Detected advertisement break of (\d+) second", line)
                    secs = int(m.group(1)) if m else 30
                    self._ad_until = time.time() + secs
                    self._send_mpv_ipc(
                        [
                            "show-text",
                            f"📺 Pausa pubblicitaria Twitch ({secs}s) — Streamlink sta filtrando l'AD, ripresa automatica...",
                            secs * 1000,
                        ]
                    )
                elif line.startswith("error:"):
                    self._last_error = line
        except Exception:
            pass


class TwitchClient:
    """Hybrid Zero-Config + OAuth Device Flow client for Twitch."""

    def __init__(self) -> None:
        self.config = self._load_config()
        self.player = StreamPlayer()

    def _load_config(self) -> dict[str, Any]:
        default_cfg: dict[str, Any] = {
            "followed_channels": list(DEFAULT_FOLLOWED),
            "oauth_token": "",
            "oauth_client_id": "",
            "user_id": "",
            "username": "",
            "quality": "best",
            "low_latency": True,
            "notifications": True,
            "dynamic_theme": True,
        }
        if not CONFIG_PATH.exists():
            self._save_config_dict(default_cfg)
            return default_cfg
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                default_cfg.update(loaded)
                follows = [
                    str(c).strip().lower()
                    for c in default_cfg.get("followed_channels", [])
                    if str(c).strip()
                ]
                default_cfg["followed_channels"] = list(dict.fromkeys(follows))
        except Exception:
            pass
        return default_cfg

    def _save_config_dict(self, cfg: dict[str, Any]) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception:
            pass

    def save_config(self) -> None:
        self._save_config_dict(self.config)

    @property
    def followed_logins(self) -> list[str]:
        return list(self.config.get("followed_channels", []))

    @property
    def quality(self) -> str:
        q = str(self.config.get("quality", "best"))
        return q if q in QUALITY_OPTIONS else "best"

    @quality.setter
    def quality(self, value: str) -> None:
        if value in QUALITY_OPTIONS:
            self.config["quality"] = value
            self.save_config()

    def cycle_quality(self) -> str:
        curr = self.quality
        idx = QUALITY_OPTIONS.index(curr) if curr in QUALITY_OPTIONS else 0
        nxt = QUALITY_OPTIONS[(idx + 1) % len(QUALITY_OPTIONS)]
        self.quality = nxt
        return nxt

    @property
    def oauth_token(self) -> str:
        tok = str(self.config.get("oauth_token", "")).strip()
        if tok.lower().startswith("oauth:"):
            tok = tok[6:].strip()
        return tok

    @property
    def username(self) -> str:
        return str(self.config.get("username", "")).strip().lower()

    def is_followed(self, login: str) -> bool:
        return login.strip().lower() in self.followed_logins

    def toggle_follow(self, login: str) -> bool:
        norm = login.strip().lower()
        if not norm:
            return False
        follows = self.followed_logins
        if norm in follows:
            follows.remove(norm)
            now_followed = False
        else:
            follows.insert(0, norm)
            now_followed = True
        self.config["followed_channels"] = follows
        self.save_config()
        return now_followed

    def _gql_request(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        use_auth: bool = False,
        timeout: float = 5.0,
    ) -> dict[str, Any] | None:
        client_id = TWITCH_PUBLIC_CLIENT_ID
        if use_auth and self.config.get("oauth_client_id"):
            client_id = str(self.config["oauth_client_id"])

        headers = {
            "Client-Id": client_id,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) twitch-tui/0.1",
        }
        if use_auth and self.oauth_token:
            headers["Authorization"] = f"OAuth {self.oauth_token}"

        payload: dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables

        try:
            req = urllib.request.Request(
                TWITCH_GQL_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict):
                    return data.get("data")
        except Exception:
            return None
        return None

    def start_device_auth(self) -> dict[str, Any] | None:
        """Starts Twitch OAuth2 Device Code flow (no client secret needed)."""
        try:
            body = urllib.parse.urlencode(
                {
                    "client_id": TWITCH_DEVICE_CLIENT_ID,
                    "scopes": "chat:read chat:edit user:read:follows",
                }
            ).encode("utf-8")
            req = urllib.request.Request("https://id.twitch.tv/oauth2/device", data=body)
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    def poll_device_auth(self, device_code: str) -> str | None:
        """Polls Twitch OAuth2 Device token endpoint once; returns access_token when authorized."""
        try:
            body = urllib.parse.urlencode(
                {
                    "client_id": TWITCH_DEVICE_CLIENT_ID,
                    "scopes": "chat:read chat:edit user:read:follows",
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                }
            ).encode("utf-8")
            req = urllib.request.Request("https://id.twitch.tv/oauth2/token", data=body)
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return str(data.get("access_token") or "") or None
        except urllib.error.HTTPError:
            return None
        except Exception:
            return None

    def set_oauth_credentials(self, token: str, username: str = "") -> tuple[bool, str]:
        clean = token.strip()
        if clean.lower().startswith("oauth:"):
            clean = clean[6:].strip()
        if not clean:
            self.config["oauth_token"] = ""
            self.config["oauth_client_id"] = ""
            self.config["user_id"] = ""
            self.config["username"] = ""
            self.save_config()
            return True, "Logout effettuato (modalità Lurker attiva)."

        self.config["oauth_token"] = clean
        resolved_user = username.strip().lower()
        resolved_uid = ""
        resolved_cid = ""

        try:
            req = urllib.request.Request(
                "https://id.twitch.tv/oauth2/validate",
                headers={"Authorization": f"OAuth {clean}"},
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                val = json.loads(resp.read().decode("utf-8"))
                resolved_user = str(val.get("login") or resolved_user).strip().lower()
                resolved_uid = str(val.get("user_id") or "")
                resolved_cid = str(val.get("client_id") or "")
        except Exception:
            pass

        self.config["username"] = resolved_user
        self.config["user_id"] = resolved_uid
        self.config["oauth_client_id"] = resolved_cid
        self.save_config()
        if resolved_user:
            return True, f"Autenticato come @{resolved_user}"
        return True, "Token OAuth salvato!"

    def sync_oauth_follows(self) -> tuple[int, str]:
        if not self.oauth_token:
            return 0, "Nessun token OAuth configurato"

        discovered: list[str] = []

        # Method 1: Official Helix API (/helix/channels/followed) using validated user_id & client_id
        uid = str(self.config.get("user_id") or "")
        cid = str(self.config.get("oauth_client_id") or TWITCH_DEVICE_CLIENT_ID)
        if uid and cid:
            try:
                url = f"https://api.twitch.tv/helix/channels/followed?user_id={uid}&first=100"
                req = urllib.request.Request(
                    url,
                    headers={
                        "Client-Id": cid,
                        "Authorization": f"Bearer {self.oauth_token}",
                    },
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    hdata = json.loads(resp.read().decode("utf-8"))
                    for item in hdata.get("data") or []:
                        broadcaster_login = str(item.get("broadcaster_login") or "").lower()
                        if broadcaster_login:
                            discovered.append(broadcaster_login)
            except Exception:
                pass

        # Method 2: GraphQL currentUser fallback
        if not discovered:
            query = """
            query {
              currentUser {
                login
                followedLiveUsers(first: 50) {
                  edges { node { login } }
                }
                follows(first: 100) {
                  edges { node { login } }
                }
              }
            }
            """
            data = self._gql_request(query, use_auth=True)
            cu = (data or {}).get("currentUser") or {}
            if cu.get("login") and not self.username:
                self.config["username"] = str(cu["login"]).lower()
            for edge in (cu.get("followedLiveUsers") or {}).get("edges") or []:
                node = edge.get("node") or {}
                if node.get("login"):
                    discovered.append(str(node["login"]).lower())
            for edge in (cu.get("follows") or {}).get("edges") or []:
                node = edge.get("node") or {}
                if node.get("login"):
                    discovered.append(str(node["login"]).lower())

        if not discovered:
            return 0, "Nessun canale seguito trovato (o permessi non sufficienti)"

        existing = self.followed_logins
        added = 0
        for login in discovered:
            if login not in existing:
                existing.append(login)
                added += 1
        self.config["followed_channels"] = existing
        self.save_config()
        return added, f"Sincronizzati {len(discovered)} canali ({added} nuovi)"

    def _parse_user_node(self, u: dict[str, Any], followed_set: set[str]) -> ChannelInfo | None:
        if not u or not u.get("login"):
            return None
        login = str(u["login"]).lower()
        display_name = str(u.get("displayName") or login)
        avatar_url = str(u.get("profileImageURL") or "")
        description = str(u.get("description") or "")
        followers_count = int((u.get("followers") or {}).get("totalCount") or 0)

        stream = u.get("stream")
        if stream and stream.get("id"):
            game_name = str((stream.get("game") or {}).get("name") or "Just Chatting")
            return ChannelInfo(
                login=login,
                display_name=display_name,
                is_live=True,
                title=str(stream.get("title") or ""),
                game_name=game_name,
                viewers_count=int(stream.get("viewersCount") or 0),
                started_at=str(stream.get("createdAt") or ""),
                preview_url=str(stream.get("previewImageURL") or ""),
                avatar_url=avatar_url,
                followers_count=followers_count,
                description=description,
                is_followed=login in followed_set,
            )

        last_b = u.get("lastBroadcast") or {}
        last_title = str(last_b.get("title") or description or "Offline")
        last_game = str((last_b.get("game") or {}).get("name") or "")
        return ChannelInfo(
            login=login,
            display_name=display_name,
            is_live=False,
            title=last_title,
            game_name=last_game,
            viewers_count=0,
            started_at="",
            preview_url="",
            avatar_url=avatar_url,
            followers_count=followers_count,
            description=description,
            is_followed=login in followed_set,
        )

    def get_followed_channels(self) -> list[ChannelInfo]:
        logins = self.followed_logins
        if not logins:
            return []

        followed_set = set(logins)
        query = """
        query($logins: [String!]) {
          users(logins: $logins) {
            login
            displayName
            description
            profileImageURL(width: 150)
            followers { totalCount }
            lastBroadcast { title startedAt game { name } }
            stream {
              id
              title
              viewersCount
              createdAt
              previewImageURL(width: 440, height: 248)
              game { name }
            }
          }
        }
        """
        results: list[ChannelInfo] = []
        found_logins: set[str] = set()

        for i in range(0, len(logins), 30):
            batch = logins[i : i + 30]
            data = self._gql_request(query, {"logins": batch})
            if not data:
                continue
            for u in data.get("users") or []:
                info = self._parse_user_node(u, followed_set)
                if info:
                    results.append(info)
                    found_logins.add(info.login)

        for login in logins:
            if login not in found_logins:
                results.append(
                    ChannelInfo(
                        login=login,
                        display_name=login,
                        is_live=False,
                        title="Offline",
                        is_followed=True,
                    )
                )

        results.sort(
            key=lambda c: (not c.is_live, -c.viewers_count, c.display_name.lower())
        )
        return results

    def get_top_streams(self, limit: int = 30, game_name: str | None = None) -> list[ChannelInfo]:
        followed_set = set(self.followed_logins)
        if game_name:
            query = """
            query($game: String!, $limit: Int!) {
              game(name: $game) {
                streams(first: $limit) {
                  edges {
                    node {
                      id
                      title
                      viewersCount
                      createdAt
                      previewImageURL(width: 440, height: 248)
                      broadcaster {
                        login
                        displayName
                        description
                        profileImageURL(width: 150)
                        followers { totalCount }
                      }
                      game { name }
                    }
                  }
                }
              }
            }
            """
            data = self._gql_request(query, {"game": game_name, "limit": limit})
            edges = ((data or {}).get("game") or {}).get("streams", {}).get("edges") or []
        else:
            query = """
            query($limit: Int!) {
              streams(first: $limit) {
                edges {
                  node {
                    id
                    title
                    viewersCount
                    createdAt
                    previewImageURL(width: 440, height: 248)
                    broadcaster {
                      login
                      displayName
                      description
                      profileImageURL(width: 150)
                      followers { totalCount }
                    }
                    game { name }
                  }
                }
              }
            }
            """
            data = self._gql_request(query, {"limit": limit})
            edges = ((data or {}).get("streams") or {}).get("edges") or []

        out: list[ChannelInfo] = []
        for edge in edges:
            node = edge.get("node") or {}
            b = node.get("broadcaster") or {}
            login = str(b.get("login") or "").lower()
            if not login:
                continue
            out.append(
                ChannelInfo(
                    login=login,
                    display_name=str(b.get("displayName") or login),
                    is_live=True,
                    title=str(node.get("title") or ""),
                    game_name=str((node.get("game") or {}).get("name") or game_name or ""),
                    viewers_count=int(node.get("viewersCount") or 0),
                    started_at=str(node.get("createdAt") or ""),
                    preview_url=str(node.get("previewImageURL") or ""),
                    avatar_url=str(b.get("profileImageURL") or ""),
                    followers_count=int((b.get("followers") or {}).get("totalCount") or 0),
                    description=str(b.get("description") or ""),
                    is_followed=login in followed_set,
                )
            )
        return out

    def get_top_games(self, limit: int = 25) -> list[GameCategory]:
        query = """
        query($limit: Int!) {
          games(first: $limit) {
            edges {
              node {
                id
                name
                viewersCount
                boxArtURL(width: 240, height: 320)
              }
            }
          }
        }
        """
        data = self._gql_request(query, {"limit": limit})
        edges = ((data or {}).get("games") or {}).get("edges") or []
        out: list[GameCategory] = []
        for edge in edges:
            node = edge.get("node") or {}
            if not node.get("name"):
                continue
            out.append(
                GameCategory(
                    id=str(node.get("id") or ""),
                    name=str(node["name"]),
                    viewers_count=int(node.get("viewersCount") or 0),
                    box_art_url=str(node.get("boxArtURL") or ""),
                )
            )
        return out

    def search_channels(self, search_query: str) -> list[ChannelInfo]:
        q = search_query.strip()
        if not q:
            return []
        followed_set = set(self.followed_logins)
        query = """
        query($search: String!) {
          searchFor(userQuery: $search, platform: "web") {
            channels {
              edges {
                item {
                  ... on User {
                    login
                    displayName
                    description
                    profileImageURL(width: 150)
                    followers { totalCount }
                    lastBroadcast { title startedAt game { name } }
                    stream {
                      id
                      title
                      viewersCount
                      createdAt
                      previewImageURL(width: 440, height: 248)
                      game { name }
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = self._gql_request(query, {"search": q})
        edges = (((data or {}).get("searchFor") or {}).get("channels") or {}).get("edges") or []
        out: list[ChannelInfo] = []
        for edge in edges:
            item = edge.get("item") or {}
            info = self._parse_user_node(item, followed_set)
            if info:
                out.append(info)
        out.sort(key=lambda c: (not c.is_live, -c.viewers_count))
        return out

    def get_channel_vods(self, login: str, limit: int = 20) -> list[VodInfo]:
        norm = login.strip().lower()
        if not norm:
            return []
        query = """
        query($login: String!, $limit: Int!) {
          user(login: $login) {
            login
            displayName
            videos(first: $limit, type: ARCHIVE) {
              edges {
                node {
                  id
                  title
                  lengthSeconds
                  viewCount
                  publishedAt
                  previewThumbnailURL(width: 440, height: 248)
                  game { name }
                }
              }
            }
          }
        }
        """
        data = self._gql_request(query, {"login": norm, "limit": limit})
        user = (data or {}).get("user") or {}
        disp = str(user.get("displayName") or norm)
        edges = (user.get("videos") or {}).get("edges") or []
        out: list[VodInfo] = []
        for edge in edges:
            node = edge.get("node") or {}
            if not node.get("id"):
                continue
            out.append(
                VodInfo(
                    id=str(node["id"]),
                    title=str(node.get("title") or "Untitled Broadcast"),
                    channel_login=norm,
                    channel_name=disp,
                    length_seconds=int(node.get("lengthSeconds") or 0),
                    view_count=int(node.get("viewCount") or 0),
                    published_at=str(node.get("publishedAt") or ""),
                    game_name=str((node.get("game") or {}).get("name") or ""),
                    preview_url=str(node.get("previewThumbnailURL") or ""),
                )
            )
        return out

    def check_and_notify_live(self, channels: list[ChannelInfo]) -> None:
        if not self.config.get("notifications", True):
            return
        current_live = {c.login: c for c in channels if c.is_live}
        prev_live: set[str] = set()
        first_run = not LIVE_CACHE_PATH.exists()

        if not first_run:
            try:
                cached = json.loads(LIVE_CACHE_PATH.read_text(encoding="utf-8"))
                prev_live = set(cached.get("live_logins", []))
            except Exception:
                pass

        try:
            LIVE_CACHE_PATH.write_text(
                json.dumps({"live_logins": list(current_live.keys()), "ts": time.time()}),
                encoding="utf-8",
            )
        except Exception:
            pass

        if first_run:
            return

        notify_bin = shutil.which("notify-send")
        if not notify_bin:
            return

        for login, ch in current_live.items():
            if login not in prev_live:
                try:
                    subprocess.Popen(
                        [
                            notify_bin,
                            "-a",
                            "Twitch TUI",
                            "-i",
                            "video-display",
                            f"🟣 {ch.display_name} is LIVE!",
                            f"{ch.game_name}\n{ch.title}",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    pass
