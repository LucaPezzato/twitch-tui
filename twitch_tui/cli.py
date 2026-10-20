"""CLI entrypoint for twitch-tui (TUI, Waybar widget JSON, Streamlink/MPV launcher, and OAuth setup)."""

from __future__ import annotations

import argparse
import html
import json
import sys

from .client import QUALITY_OPTIONS, TwitchClient
from .tui import _fmt_num, run_tui


def output_waybar(client: TwitchClient, max_len: int = 34) -> None:
    channels = client.get_followed_channels()
    client.check_and_notify_live(channels)
    live_channels = [c for c in channels if c.is_live]
    pstate = client.player.get_state()

    tooltip_lines: list[str] = []
    if pstate:
        ch_lbl = pstate.get("title") or pstate.get("channel") or "Stream"
        qual = pstate.get("quality", "best")
        status_lbl = "Paused" if pstate.get("paused") else "Playing"
        tooltip_lines.append(f"▶ MPV ({status_lbl} • {qual}): {ch_lbl}")
        tooltip_lines.append("─" * 32)

    if live_channels:
        tooltip_lines.append(f"🟣 Followed Live ({len(live_channels)}):")
        for c in live_channels[:12]:
            up = f" ({c.uptime_str})" if c.uptime_str else ""
            tooltip_lines.append(
                f"• {c.display_name} — {c.game_name} [👁 {_fmt_num(c.viewers_count)}{up}]"
            )
    else:
        tooltip_lines.append("⚫ No followed channels currently live")

    tooltip_lines.append("\nLeft-Click: Open Twitch TUI\nRight-Click: Stop/Toggle MPV Stream")

    if pstate:
        icon = "" if pstate.get("paused") else ""
        label = f"{icon} {len(live_channels)}"
        css_class = "watching"
    elif live_channels:
        label = f" {len(live_channels)}"
        css_class = "live"
    else:
        label = " 0"
        css_class = "offline"

    print(
        json.dumps(
            {
                "text": html.escape(label),
                "tooltip": "\n".join(tooltip_lines),
                "class": css_class,
            }
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="twitch-tui",
        description="Truecolor Terminal UI, Live IRC Chat, MPV/Streamlink controller & Waybar widget for Twitch.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("tui", help="Open interactive Twitch Terminal UI (default)")

    wb = sub.add_parser("waybar", help="Output JSON status for Waybar custom module")
    wb.add_argument("--max-len", type=int, default=28, help="Max label length")

    w_p = sub.add_parser("watch", help="Watch a channel or VOD URL in MPV via Streamlink")
    w_p.add_argument("channel", help="Channel login or Twitch URL")
    w_p.add_argument("-q", "--quality", choices=QUALITY_OPTIONS, default=None, help="Stream quality")
    w_p.add_argument("--pip", action="store_true", help="Open in Picture-in-Picture mode")
    w_p.add_argument("--audio-only", action="store_true", help="Listen to audio only in background")

    f_p = sub.add_parser("follow", help="Add a channel to followed list")
    f_p.add_argument("channel", help="Channel login to follow")

    uf_p = sub.add_parser("unfollow", help="Remove a channel from followed list")
    uf_p.add_argument("channel", help="Channel login to unfollow")

    sub.add_parser("list", help="List followed channels and their live status")
    sub.add_parser("toggle", help="Toggle pause/play on active MPV stream")
    sub.add_parser("stop", help="Stop active MPV stream")
    sub.add_parser("login", help="1-Click browser login via Twitch Device Activation")

    auth_p = sub.add_parser("auth", help="Configure optional Twitch OAuth token for chat & follow sync")
    auth_p.add_argument("token", nargs="?", default="", help="Twitch OAuth token (empty to clear)")
    auth_p.add_argument("-u", "--user", default="", help="Twitch username (auto-detected if omitted)")
    auth_p.add_argument("--sync", action="store_true", help="Sync followed channels from Twitch account")

    args = parser.parse_args(argv)
    client = TwitchClient()
    cmd = args.command or "tui"

    if cmd == "tui":
        run_tui(client)
        return 0

    if cmd == "waybar":
        output_waybar(client, max_len=args.max_len)
        return 0

    if cmd == "watch":
        qual = "audio_only" if args.audio_only else (args.quality or client.quality)
        pip = True if args.pip else client.pip
        ok, msg = client.player.watch_stream(
            channel_or_url=args.channel,
            title=args.channel,
            quality=qual,
            pip=pip,
            low_latency=bool(client.config.get("low_latency", True)),
            oauth_token=client.oauth_token,
        )
        print(msg)
        return 0 if ok else 1

    if cmd == "follow":
        login = args.channel.strip().lower()
        if not client.is_followed(login):
            client.toggle_follow(login)
        print(f"★ Followed {login}")
        return 0

    if cmd == "unfollow":
        login = args.channel.strip().lower()
        if client.is_followed(login):
            client.toggle_follow(login)
        print(f"Unfollowed {login}")
        return 0

    if cmd == "list":
        channels = client.get_followed_channels()
        for c in channels:
            if c.is_live:
                print(
                    f"🔴 LIVE    {c.display_name:<20} | {c.game_name:<24} | 👁 {_fmt_num(c.viewers_count):>6} | {c.title}"
                )
            else:
                print(f"⚫ OFFLINE {c.display_name:<20} | Last: {c.game_name or '—'}")
        return 0

    if cmd == "toggle":
        if client.player.is_playing():
            client.player.toggle_pause()
        return 0

    if cmd == "stop":
        client.player.stop()
        return 0

    if cmd == "login":
        import shutil
        import subprocess
        import time
        import webbrowser

        dev = client.start_device_auth()
        if not dev or not dev.get("user_code"):
            print("Failed to start Twitch Device Activation.")
            return 1
        user_code = str(dev["user_code"])
        device_code = str(dev["device_code"])
        verify_uri = str(dev.get("verification_uri") or "https://www.twitch.tv/activate")
        interval = max(3, int(dev.get("interval") or 5))
        full_url = f"{verify_uri}?public=true&device-code={user_code}"
        if shutil.which("wl-copy"):
            subprocess.run(["wl-copy", user_code], timeout=1.5)
        webbrowser.open(full_url)
        print(f"🔑 Twitch Activation Code: {user_code} (copied to clipboard!)")
        print(f"🌐 Opened {full_url} — click 'Activate / Authorize' in your browser…")
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(interval)
            token = client.poll_device_auth(device_code)
            if token:
                _, auth_msg = client.set_oauth_credentials(token)
                _, sync_msg = client.sync_oauth_follows()
                print(f"✅ {auth_msg} • {sync_msg}")
                return 0
        print("⏱ Login timed out.")
        return 1

    if cmd == "auth":
        if args.token:
            ok, msg = client.set_oauth_credentials(args.token, args.user)
            print(msg)
        if args.sync or args.token:
            _, sync_msg = client.sync_oauth_follows()
            print(sync_msg)
        elif not args.token:
            ok, msg = client.set_oauth_credentials("", "")
            print(msg)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
