"""Modern Truecolor Textual TUI for Twitch with 16:9 Live Previews, IRC Chat, MPV/PiP & 1-Click Login."""

from __future__ import annotations

import shutil
import subprocess
import time
import webbrowser
from typing import Any

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Input, Static

from .artwork import (
    DRACULA_PALETTE,
    ThemePalette,
    extract_dynamic_palette,
    fetch_image,
    render_image_halfblocks,
    render_placeholder_tv,
)
from .chat import TwitchChatClient
from .client import ChannelInfo, GameCategory, TwitchClient, VodInfo


SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 2:
        return text[:width]
    return text[: width - 1] + "…"


def _fmt_num(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,}"


class TwitchApp(App[None]):
    """Dracula & Dynamic Stream Artwork TUI for Twitch."""

    AUTO_FOCUS = None

    CSS = """
    Screen {
        background: #1e1f29;
        color: #f8f8f2;
        padding: 0 1;
    }

    #top-bar {
        height: 1;
        width: 100%;
        margin-top: 0;
        content-align: left middle;
    }

    #hero-card {
        height: 11;
        width: 100%;
        background: #282a36;
        border: round #bd93f9;
        padding: 0 1;
    }

    #artwork-box {
        width: 28;
        height: 9;
        margin-right: 2;
        content-align: center middle;
    }

    #stream-info-box {
        width: 1fr;
        height: 9;
        padding-top: 0;
    }

    #search-input, #auth-input {
        dock: top;
        height: 3;
        margin: 0 0 1 0;
        background: #282a36;
        color: #f8f8f2;
        border: round #ff79c6;
        display: none;
    }

    #search-input.visible, #auth-input.visible {
        display: block;
    }

    #workspace {
        width: 100%;
        height: 1fr;
    }

    #list-card {
        width: 3fr;
        height: 1fr;
        background: #282a36;
        border: round #6272a4;
        padding: 0 1;
    }

    #list-view {
        width: 100%;
        height: 1fr;
    }

    #chat-card {
        width: 2fr;
        height: 1fr;
        background: #282a36;
        border: round #6272a4;
        padding: 0 1;
        margin-left: 1;
    }

    #chat-card.hidden {
        display: none;
    }

    #chat-view {
        width: 100%;
        height: 1fr;
    }

    #chat-input {
        height: 3;
        width: 100%;
        background: #1e1f29;
        color: #f8f8f2;
        border: round #bd93f9;
        display: none;
    }

    #chat-input.visible {
        display: block;
    }

    #help-bar {
        height: 1;
        width: 100%;
        content-align: center middle;
        color: #6272a4;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=False),
        Binding("escape", "cancel_or_back", "Back", show=False),
        Binding("1", "tab_followed", "Followed", show=False),
        Binding("2", "tab_top", "Top Live", show=False),
        Binding("3", "tab_categories", "Categories", show=False),
        Binding("4", "tab_vods", "VODs", show=False),
        Binding("tab", "next_tab", "Next Tab", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("g", "cursor_top", "Top", show=False),
        Binding("G", "cursor_bottom", "Bottom", show=False),
        Binding("enter", "activate_item", "Watch/Open", show=False),
        Binding("w", "watch_selected", "Watch MPV", show=False),
        Binding("a", "listen_audio", "Audio-Only", show=False),
        Binding("x", "stop_stream", "Stop MPV", show=False),
        Binding("v", "cycle_quality", "Quality", show=False),
        Binding("V", "cycle_quality", "Quality", show=False),
        Binding("f", "toggle_follow", "Follow/Unfollow", show=False),
        Binding("F", "toggle_follow", "Follow/Unfollow", show=False),
        Binding("l", "lurk_chat", "Join Chat", show=False),
        Binding("c", "toggle_chat_panel", "Toggle Chat", show=False),
        Binding("i", "focus_chat_input", "Send Chat", show=False),
        Binding("slash", "open_search", "Search", show=False),
        Binding("u", "start_browser_login", "Twitch Login", show=False),
        Binding("U", "start_browser_login", "Twitch Login", show=False),
        Binding("A", "open_auth", "Manual Token", show=False),
        Binding("S", "sync_oauth_follows", "Sync Follows", show=False),
        Binding("o", "open_browser", "Browser", show=False),
        Binding("r", "refresh_data", "Refresh", show=False),
        Binding("t", "toggle_theme", "Theme", show=False),
        Binding("space", "mpv_pause", "MPV Pause", show=False),
        Binding("m", "mpv_mute", "MPV Mute", show=False),
        Binding("plus", "mpv_vol_up", "Vol +", show=False),
        Binding("equal", "mpv_vol_up", "Vol +", show=False),
        Binding("minus", "mpv_vol_down", "Vol -", show=False),
    ]

    active_tab: reactive[str] = reactive("followed")
    cursor_index: reactive[int] = reactive(0)
    chat_visible: reactive[bool] = reactive(True)
    dynamic_theme: reactive[bool] = reactive(True)

    def __init__(self, client: TwitchClient) -> None:
        super().__init__()
        self.client = client
        self.dynamic_theme = bool(client.config.get("dynamic_theme", True))
        self.palette: ThemePalette = DRACULA_PALETTE

        self.followed_channels: list[ChannelInfo] = []
        self.top_streams: list[ChannelInfo] = []
        self.categories: list[GameCategory] = []
        self.vods: list[VodInfo] = []
        self.search_results: list[ChannelInfo] = []

        self.selected_category: str | None = None
        self.vod_channel_login: str = ""
        self.vod_channel_name: str = ""
        self.search_query: str = ""
        self.status_banner: str = ""
        self.status_banner_ts: float = 0.0
        self.device_login_code: str = ""
        self.loading: bool = True

        self._spinner_idx: int = 0
        self._was_starting_mpv: bool = False
        self._img_cache: dict[str, tuple[Text, ThemePalette]] = {}
        self._current_art_url: str = ""
        self._fetching_url: str = ""
        self._art_renderable: Text = render_placeholder_tv(DRACULA_PALETTE)

        self.chat = TwitchChatClient(
            oauth_token=self.client.oauth_token,
            username=self.client.username,
            on_update=self._on_chat_message_threadsafe,
        )

    def compose(self) -> ComposeResult:
        yield Static(id="top-bar")
        with Horizontal(id="hero-card"):
            yield Static(id="artwork-box")
            yield Static(id="stream-info-box")
        yield Input(
            placeholder="🔍 Cerca canale Twitch (Invio per cercare, Esc per chiudere)…",
            id="search-input",
            disabled=True,
        )
        yield Input(
            placeholder="🔑 Incolla token OAuth Twitch (oppure lascia vuoto per Logout) e premi Invio…",
            id="auth-input",
            password=True,
            disabled=True,
        )
        with Horizontal(id="workspace"):
            with Vertical(id="list-card"):
                yield Static(id="list-view")
            with Vertical(id="chat-card"):
                yield Static(id="chat-view")
                yield Input(
                    placeholder="💬 Scrivi in chat (Invio per inviare, Esc per annullare)…",
                    id="chat-input",
                    disabled=True,
                )
        yield Static(id="help-bar")

    def on_mount(self) -> None:
        self.set_focus(None)
        self._apply_palette(self.palette)
        self._refresh_all_views()
        self.load_initial_data()
        self.set_interval(0.3, self._fast_ui_tick)
        self.set_interval(45.0, self._periodic_live_refresh)

    def on_unmount(self) -> None:
        self.device_login_code = ""
        self.chat.stop()

    def _set_banner(self, msg: str, duration: float = 6.0) -> None:
        self.status_banner = msg
        self.status_banner_ts = time.time() + (duration - 6.0)
        self._render_hero()

    def _on_chat_message_threadsafe(self) -> None:
        try:
            self.call_from_thread(self._render_chat)
        except Exception:
            pass

    def _fast_ui_tick(self) -> None:
        self._spinner_idx = (self._spinner_idx + 1) % len(SPINNER_FRAMES)

        err = self.client.player.pop_last_error()
        if err:
            self._was_starting_mpv = False
            self._set_banner(f"⚠ {err}")
            self.notify(f"Errore stream: {err}", title="MPV / Streamlink", severity="error", timeout=5)
            self._render_list()

        pstate = self.client.player.get_state()
        is_starting = bool(pstate and pstate.get("starting"))

        if self._was_starting_mpv and pstate and not is_starting:
            self._was_starting_mpv = False
            ch_title = pstate.get("title") or pstate.get("channel") or "Stream"
            self._set_banner(f"▶ Riproduzione avviata: {ch_title}")
            self.notify(f"▶ In riproduzione su MPV: {ch_title}", title="Twitch Player", timeout=3)
            self._render_list()

        if self.status_banner and not self.device_login_code and (time.time() - self.status_banner_ts > 6.0):
            self.status_banner = ""

        if is_starting or self._spinner_idx % 4 == 0:
            self._render_top_bar()
            self._render_hero()
            if is_starting:
                self._render_list()

    def _periodic_live_refresh(self) -> None:
        if self.active_tab == "followed":
            self._fetch_followed_bg(silent=True)

    @work(thread=True)
    def load_initial_data(self) -> None:
        self.loading = True
        self.call_from_thread(self._refresh_all_views)
        channels = self.client.get_followed_channels()
        self.client.check_and_notify_live(channels)
        self.followed_channels = channels
        self.loading = False

        first_live = next((c for c in channels if c.is_live), None)
        if first_live and not self.chat.current_channel:
            self.chat.join_channel(first_live.login)

        self.call_from_thread(self._on_data_loaded)

    def _on_data_loaded(self) -> None:
        self._clamp_cursor()
        self._update_selected_artwork()
        self._refresh_all_views()

    @work(thread=True)
    def _fetch_followed_bg(self, silent: bool = False) -> None:
        if not silent:
            self.loading = True
            self.call_from_thread(self._render_list)
        channels = self.client.get_followed_channels()
        self.client.check_and_notify_live(channels)
        self.followed_channels = channels
        self.loading = False
        self.call_from_thread(self._on_data_loaded)

    @work(thread=True)
    def _fetch_top_streams_bg(self, game_name: str | None = None) -> None:
        self.loading = True
        self.call_from_thread(self._render_list)
        self.top_streams = self.client.get_top_streams(limit=30, game_name=game_name)
        self.loading = False
        self.call_from_thread(self._on_data_loaded)

    @work(thread=True)
    def _fetch_categories_bg(self) -> None:
        self.loading = True
        self.call_from_thread(self._render_list)
        self.categories = self.client.get_top_games(limit=25)
        self.loading = False
        self.call_from_thread(self._on_data_loaded)

    @work(thread=True)
    def _fetch_vods_bg(self, login: str, display_name: str) -> None:
        self.loading = True
        self.vod_channel_login = login
        self.vod_channel_name = display_name
        self.call_from_thread(self._render_list)
        self.vods = self.client.get_channel_vods(login, limit=20)
        self.loading = False
        self.call_from_thread(self._on_data_loaded)

    @work(thread=True)
    def _fetch_search_bg(self, query: str) -> None:
        self.loading = True
        self.search_query = query
        self.call_from_thread(self._render_list)
        self.search_results = self.client.search_channels(query)
        self.loading = False
        self.call_from_thread(self._on_data_loaded)

    def _current_items(self) -> list[Any]:
        if self.active_tab == "followed":
            return self.followed_channels
        if self.active_tab == "top":
            return self.top_streams
        if self.active_tab == "categories":
            return self.categories
        if self.active_tab == "vods":
            return self.vods
        if self.active_tab == "search":
            return self.search_results
        return []

    def _selected_item(self) -> Any | None:
        items = self._current_items()
        if not items:
            return None
        idx = max(0, min(self.cursor_index, len(items) - 1))
        return items[idx]

    def _selected_channel(self) -> ChannelInfo | None:
        item = self._selected_item()
        if isinstance(item, ChannelInfo):
            return item
        return None

    def _clamp_cursor(self) -> None:
        items = self._current_items()
        if not items:
            self.cursor_index = 0
        else:
            self.cursor_index = max(0, min(self.cursor_index, len(items) - 1))

    def _update_selected_artwork(self) -> None:
        item = self._selected_item()
        url = ""
        is_live = False
        if isinstance(item, ChannelInfo):
            url = item.best_image_url
            is_live = item.is_live
        elif isinstance(item, VodInfo):
            url = item.preview_url
            is_live = True
        elif isinstance(item, GameCategory):
            url = item.box_art_url
            is_live = True

        if not url:
            self._current_art_url = ""
            self._art_renderable = render_placeholder_tv(self.palette, is_live=is_live)
            self._render_hero()
            return

        if url in self._img_cache:
            art_text, dyn_pal = self._img_cache[url]
            self._current_art_url = url
            self._art_renderable = art_text
            target_pal = dyn_pal if self.dynamic_theme else DRACULA_PALETTE
            if target_pal != self.palette:
                self.palette = target_pal
                self._apply_palette(self.palette)
            self._render_hero()
            return

        if url != self._fetching_url:
            self._fetching_url = url
            self._load_artwork_worker(url, is_live)

    @work(thread=True)
    def _load_artwork_worker(self, url: str, is_live: bool) -> None:
        img = fetch_image(url)
        if img is None:
            return
        art_text = render_image_halfblocks(img, width_cells=26, height_cells=9)
        dyn_pal = extract_dynamic_palette(img)
        self._img_cache[url] = (art_text, dyn_pal)
        self.call_from_thread(self._on_artwork_ready, url, art_text, dyn_pal)

    def _on_artwork_ready(self, url: str, art_text: Text, dyn_pal: ThemePalette) -> None:
        item = self._selected_item()
        curr_url = ""
        if isinstance(item, ChannelInfo):
            curr_url = item.best_image_url
        elif isinstance(item, VodInfo):
            curr_url = item.preview_url
        elif isinstance(item, GameCategory):
            curr_url = item.box_art_url

        if curr_url == url:
            self._current_art_url = url
            self._art_renderable = art_text
            target_pal = dyn_pal if self.dynamic_theme else DRACULA_PALETTE
            if target_pal != self.palette:
                self.palette = target_pal
                self._apply_palette(self.palette)
            self._render_hero()

    def _apply_palette(self, pal: ThemePalette) -> None:
        try:
            self.screen.styles.background = pal.bg_main
            hero = self.query_one("#hero-card", Horizontal)
            hero.styles.background = pal.bg_panel
            hero.styles.border = ("round", pal.primary)

            list_card = self.query_one("#list-card", Vertical)
            list_card.styles.background = pal.bg_panel
            list_card.styles.border = ("round", pal.secondary)

            chat_card = self.query_one("#chat-card", Vertical)
            chat_card.styles.background = pal.bg_panel
            chat_card.styles.border = ("round", pal.fg_muted)
        except Exception:
            pass

    def _refresh_all_views(self) -> None:
        self._render_top_bar()
        self._render_hero()
        self._render_list()
        self._render_chat()
        self._render_help_bar()

    def _render_top_bar(self) -> None:
        pal = self.palette
        live_count = sum(1 for c in self.followed_channels if c.is_live)

        t = Text()
        t.append(" 🟣 TWITCH ", style=f"bold {pal.bg_main} on {pal.primary}")
        t.append("  ")

        tabs = [
            ("followed", f"1 ★ Followed ({live_count})"),
            ("top", "2 🔥 Top Live"),
            ("categories", "3 🎮 Categories"),
            ("vods", "4 📼 VODs"),
        ]
        if self.active_tab == "search":
            tabs.append(("search", f"🔍 '{_truncate(self.search_query, 12)}'"))

        for key, label in tabs:
            if self.active_tab == key:
                t.append(f" {label} ", style=f"bold {pal.bg_main} on {pal.secondary}")
            else:
                t.append(f" {label} ", style=f"{pal.fg_muted}")
            t.append(" ")

        t.append(" │ ", style=pal.fg_muted)
        t.append(f"[v] 🎞 {self.client.quality}", style=f"bold {pal.primary}")
        t.append("  ")
        if self.device_login_code:
            t.append(f" 🔑 CODICE: {self.device_login_code} ", style="bold #1e1f29 on #f1fa8c")
        elif self.client.username:
            t.append(f"[u] ● @{self.client.username}", style="bold #50fa7b")
        else:
            t.append("[u] 🔑 Login", style="bold #ffb86c")

        try:
            self.query_one("#top-bar", Static).update(t)
        except Exception:
            pass

    def _render_hero(self) -> None:
        pal = self.palette
        try:
            self.query_one("#artwork-box", Static).update(self._art_renderable)
        except Exception:
            pass

        info = Text()
        item = self._selected_item()

        if isinstance(item, ChannelInfo):
            info.append(f"{item.display_name}", style=f"bold {pal.fg}")
            info.append(f" (@{item.login})  ", style=pal.fg_muted)
            if item.is_live:
                info.append(" 🔴 LIVE ", style="bold #f8f8f2 on #ff5555")
                if item.uptime_str:
                    info.append(f"  ⏱ {item.uptime_str}", style="bold #ffb86c")
                info.append(f"  👁 {_fmt_num(item.viewers_count)}", style="bold #ff79c6")
            else:
                info.append(" ⚫ OFFLINE ", style=f"bold {pal.fg_muted} on {pal.bg_highlight}")
            if self.client.is_followed(item.login):
                info.append("  ★ Following", style="bold #f1fa8c")
            info.append("\n")

            game_label = item.game_name or ("Live Stream" if item.is_live else "Offline")
            info.append("🎮 ", style=pal.primary)
            info.append(f"{_truncate(game_label, 50)}", style=f"bold {pal.secondary}")
            if item.followers_count > 0:
                info.append(f"   •   👥 {_fmt_num(item.followers_count)} followers", style=pal.fg_muted)
            info.append("\n")

            title_str = " ".join((item.title or item.description or "No broadcast title").split())
            info.append(f"{_truncate(title_str, 74)}\n", style=pal.fg)
            if len(title_str) > 74:
                info.append(f"{_truncate(title_str[74:], 74)}\n", style=pal.fg_muted)
            else:
                info.append("\n")

        elif isinstance(item, GameCategory):
            info.append(f"🎮 {item.name}\n", style=f"bold {pal.primary}")
            info.append(f"👁 {_fmt_num(item.viewers_count)} spettatori attuali su Twitch\n", style="bold #ff79c6")
            info.append("Premi [Invio] per esplorare i canali live in questa categoria.\n\n", style=pal.fg_muted)

        elif isinstance(item, VodInfo):
            info.append(f"📼 {item.channel_name} ", style=f"bold {pal.fg}")
            info.append(f"• {item.date_str} ", style=pal.fg_muted)
            info.append(f" ⏱ {item.duration_str} ", style=f"bold {pal.bg_main} on {pal.primary}")
            info.append(f"  👁 {_fmt_num(item.view_count)} views\n", style="bold #ff79c6")
            info.append(f"🎮 {item.game_name or 'Past Broadcast'}\n", style=f"bold {pal.secondary}")
            info.append(f"{_truncate(item.title, 74)}\n\n", style=pal.fg)

        else:
            info.append("Nessun canale selezionato\n\n\n\n", style=pal.fg_muted)

        info.append("─" * 68 + "\n", style=pal.bg_highlight)

        pstate = self.client.player.get_state()
        spinner = SPINNER_FRAMES[self._spinner_idx % len(SPINNER_FRAMES)]
        if pstate:
            ch_label = pstate.get("title") or pstate.get("channel") or "Stream"
            qual = pstate.get("quality", "best")
            if pstate.get("starting"):
                info.append(
                    f" {spinner} AVVIO MPV IN CORSO: {_truncate(str(ch_label), 34)} [{qual}] ",
                    style="bold #1e1f29 on #ffb86c",
                )
                info.append("  ([x] Annulla)\n", style=pal.fg_muted)
            else:
                icon = "⏸ PAUSA" if pstate.get("paused") else "▶ LIVE SU MPV"
                vol = pstate.get("volume", 100)
                mute_tag = " 🔇 Muto" if pstate.get("muted") else f" 🔊 {vol}%"
                info.append(
                    f" {icon}: {_truncate(str(ch_label), 30)} [{qual}]{mute_tag} ",
                    style="bold #1e1f29 on #50fa7b",
                )
                info.append("  ([Space] Pausa [m] Muto [x] Stop)\n", style=pal.fg_muted)
        else:
            info.append(
                "⏹ Player inattivo — [Invio/w] Apri su MPV  [a] Solo Audio  [v] Qualità  [u] Login Twitch\n",
                style=pal.fg_muted,
            )

        if self.status_banner:
            info.append(f"✨ {self.status_banner}", style="bold #ff79c6")

        try:
            self.query_one("#stream-info-box", Static).update(info)
        except Exception:
            pass

    def _render_list(self) -> None:
        pal = self.palette
        view = Text()

        if self.active_tab == "followed":
            live_n = sum(1 for c in self.followed_channels if c.is_live)
            view.append(
                f" ★ CANALI SEGUITI ({live_n} Live / {len(self.followed_channels)} Totali)\n",
                style=f"bold {pal.primary}",
            )
        elif self.active_tab == "top":
            cat_suffix = f" — {self.selected_category}" if self.selected_category else ""
            view.append(f" 🔥 TOP LIVE STREAMS{cat_suffix}\n", style=f"bold {pal.primary}")
        elif self.active_tab == "categories":
            view.append(" 🎮 TOP CATEGORIE SU TWITCH\n", style=f"bold {pal.primary}")
        elif self.active_tab == "vods":
            ch_lbl = self.vod_channel_name or self.vod_channel_login or "Canale"
            view.append(f" 📼 VOD E DIRETTE PASSATE — {ch_lbl}\n", style=f"bold {pal.primary}")
        elif self.active_tab == "search":
            view.append(f" 🔍 RISULTATI RICERCA: '{self.search_query}'\n", style=f"bold {pal.primary}")

        view.append("─" * 58 + "\n", style=pal.bg_highlight)

        if self.loading:
            view.append("\n  ⏳ Caricamento dati da Twitch…\n", style=f"italic {pal.fg_muted}")
            try:
                self.query_one("#list-view", Static).update(view)
            except Exception:
                pass
            return

        items = self._current_items()
        if not items:
            view.append("\n  Nessun elemento. Premi [/] per cercare o [r] per aggiornare.\n", style=pal.fg_muted)
            try:
                self.query_one("#list-view", Static).update(view)
            except Exception:
                pass
            return

        try:
            list_h = max(8, self.query_one("#list-view", Static).size.height - 3)
        except Exception:
            list_h = 14

        pstate = self.client.player.get_state()
        active_login = str((pstate or {}).get("login") or "").lower()
        is_starting = bool(pstate and pstate.get("starting"))
        spinner = SPINNER_FRAMES[self._spinner_idx % len(SPINNER_FRAMES)]

        total = len(items)
        half = max(1, list_h // 2)
        start = max(0, min(self.cursor_index - half, max(0, total - list_h)))
        end = min(total, start + list_h)

        for idx in range(start, end):
            is_sel = idx == self.cursor_index
            row_bg = f" on {pal.bg_highlight}" if is_sel else ""
            prefix = "▸ " if is_sel else "  "
            item = items[idx]

            if isinstance(item, ChannelInfo):
                dot = "🔴" if item.is_live else "⚫"
                star = "★ " if self.client.is_followed(item.login) else "  "
                name_str = _truncate(item.display_name, 15).ljust(15)
                name_style = f"bold {pal.fg}{row_bg}" if item.is_live else f"{pal.fg_muted}{row_bg}"

                view.append(prefix, style=f"bold {pal.secondary}{row_bg}")
                view.append(f"{dot} ", style=row_bg)
                view.append(star, style=f"bold #f1fa8c{row_bg}")
                view.append(name_str, style=name_style)

                if item.login == active_login:
                    if is_starting:
                        view.append(f" {spinner} Avvio MPV… ", style="bold #1e1f29 on #ffb86c")
                    else:
                        view.append(" ▶ SU MPV ", style="bold #1e1f29 on #50fa7b")
                    viewers_str = _fmt_num(item.viewers_count).rjust(6)
                    view.append(f" 👁 {viewers_str}", style=f"bold #ff79c6{row_bg}")
                elif item.is_live:
                    game_str = _truncate(item.game_name or "Live", 18).ljust(18)
                    viewers_str = _fmt_num(item.viewers_count).rjust(6)
                    view.append(f" {game_str}", style=f"{pal.primary}{row_bg}")
                    view.append(f" 👁 {viewers_str}", style=f"bold #ff79c6{row_bg}")
                else:
                    last_g = _truncate(item.game_name or "Offline", 24)
                    view.append(f" {last_g}", style=f"{pal.fg_muted}{row_bg}")
                view.append("\n")

            elif isinstance(item, GameCategory):
                name_str = _truncate(item.name, 34).ljust(34)
                viewers_str = _fmt_num(item.viewers_count).rjust(8)
                view.append(prefix, style=f"bold {pal.secondary}{row_bg}")
                view.append(f"🎮 {name_str}", style=f"bold {pal.fg}{row_bg}")
                view.append(f" 👁 {viewers_str}\n", style=f"bold #ff79c6{row_bg}")

            elif isinstance(item, VodInfo):
                title_str = _truncate(item.title, 30).ljust(30)
                dur_str = item.duration_str.rjust(8)
                view.append(prefix, style=f"bold {pal.secondary}{row_bg}")
                view.append(f"📼 {item.date_str} ", style=f"{pal.fg_muted}{row_bg}")
                view.append(title_str, style=f"bold {pal.fg}{row_bg}")
                view.append(f" ⏱ {dur_str}\n", style=f"{pal.primary}{row_bg}")

        try:
            self.query_one("#list-view", Static).update(view)
        except Exception:
            pass

    def _render_chat(self) -> None:
        if not self.chat_visible:
            return
        pal = self.palette
        chat_box = Text()
        ch = self.chat.current_channel
        status_dot = "🟢" if self.chat.is_connected else "🟡"
        ch_title = f"#{ch}" if ch else "Nessun canale"
        mode_tag = "Scrivi [i]" if self.chat.can_send else "Lurk [u per Login]"

        chat_box.append(f" 💬 CHAT {status_dot} {ch_title} ", style=f"bold {pal.secondary}")
        chat_box.append(f"({mode_tag})\n", style=pal.fg_muted)
        chat_box.append("─" * 42 + "\n", style=pal.bg_highlight)

        try:
            chat_h = max(6, self.query_one("#chat-view", Static).size.height - 3)
        except Exception:
            chat_h = 14

        msgs = self.chat.get_messages()
        if not msgs:
            chat_box.append(
                "\n  Premi [l] o [Invio] su un canale\n  per entrare nella chat live.\n",
                style=pal.fg_muted,
            )
        else:
            visible_msgs = msgs[-chat_h:]
            for m in visible_msgs:
                if m.is_system:
                    chat_box.append(f" {m.timestamp} ", style=pal.fg_muted)
                    chat_box.append(f"{_truncate(m.text, 44)}\n", style=f"italic {pal.fg_muted}")
                else:
                    badge_str = "".join(m.badges)
                    if badge_str:
                        badge_str += " "
                    chat_box.append(f"{badge_str}", style="bold")
                    chat_box.append(f"{_truncate(m.display_name, 14)}", style=f"bold {m.color}")
                    if m.is_action:
                        chat_box.append(f" {_truncate(m.text, 38)}\n", style=f"italic {m.color}")
                    else:
                        chat_box.append(": ", style=pal.fg_muted)
                        chat_box.append(f"{_truncate(m.text, 38)}\n", style=pal.fg)

        try:
            self.query_one("#chat-view", Static).update(chat_box)
        except Exception:
            pass

    def _render_help_bar(self) -> None:
        pal = self.palette
        h = Text()
        keys = [
            ("Invio/w", "Guarda"),
            ("v", "Qualità"),
            ("a", "Audio"),
            ("u", "Login Twitch"),
            ("i", "Scrivi Chat"),
            ("f", "Segui"),
            ("/", "Cerca"),
            ("1-4", "Tab"),
            ("q", "Esci"),
        ]
        for k, desc in keys:
            h.append(f"[{k}]", style=f"bold {pal.primary}")
            h.append(f" {desc}  ", style=pal.fg_muted)
        try:
            self.query_one("#help-bar", Static).update(h)
        except Exception:
            pass

    # --- Navigation & Actions ---

    def action_cursor_down(self) -> None:
        items = self._current_items()
        if items and self.cursor_index < len(items) - 1:
            self.cursor_index += 1
            self._update_selected_artwork()
            self._render_list()
            self._render_hero()

    def action_cursor_up(self) -> None:
        if self.cursor_index > 0:
            self.cursor_index -= 1
            self._update_selected_artwork()
            self._render_list()
            self._render_hero()

    def action_cursor_top(self) -> None:
        self.cursor_index = 0
        self._update_selected_artwork()
        self._render_list()
        self._render_hero()

    def action_cursor_bottom(self) -> None:
        items = self._current_items()
        if items:
            self.cursor_index = len(items) - 1
            self._update_selected_artwork()
            self._render_list()
            self._render_hero()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self.action_cursor_down()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self.action_cursor_up()

    def action_tab_followed(self) -> None:
        self.active_tab = "followed"
        self.cursor_index = 0
        self._update_selected_artwork()
        self._refresh_all_views()

    def action_tab_top(self) -> None:
        self.active_tab = "top"
        self.cursor_index = 0
        if not self.top_streams:
            self._fetch_top_streams_bg(self.selected_category)
        else:
            self._update_selected_artwork()
            self._refresh_all_views()

    def action_tab_categories(self) -> None:
        self.active_tab = "categories"
        self.cursor_index = 0
        if not self.categories:
            self._fetch_categories_bg()
        else:
            self._update_selected_artwork()
            self._refresh_all_views()

    def action_tab_vods(self) -> None:
        ch = self._selected_channel()
        self.active_tab = "vods"
        self.cursor_index = 0
        if ch and ch.login != self.vod_channel_login:
            self._fetch_vods_bg(ch.login, ch.display_name)
        elif not self.vods:
            default_ch = self.followed_channels[0] if self.followed_channels else None
            if default_ch:
                self._fetch_vods_bg(default_ch.login, default_ch.display_name)
            else:
                self._refresh_all_views()
        else:
            self._update_selected_artwork()
            self._refresh_all_views()

    def action_next_tab(self) -> None:
        order = ["followed", "top", "categories", "vods"]
        curr = self.active_tab if self.active_tab in order else "followed"
        nxt = order[(order.index(curr) + 1) % len(order)]
        if nxt == "followed":
            self.action_tab_followed()
        elif nxt == "top":
            self.action_tab_top()
        elif nxt == "categories":
            self.action_tab_categories()
        elif nxt == "vods":
            self.action_tab_vods()

    def action_activate_item(self) -> None:
        item = self._selected_item()
        if isinstance(item, GameCategory):
            self.selected_category = item.name
            self.active_tab = "top"
            self.cursor_index = 0
            self._fetch_top_streams_bg(item.name)
            return
        self.action_watch_selected()

    def action_watch_selected(self) -> None:
        item = self._selected_item()
        if isinstance(item, ChannelInfo):
            self.chat.join_channel(item.login)
            if not item.is_live:
                msg = f"{item.display_name} è offline — apro i suoi VOD passati!"
                self._set_banner(msg)
                self.notify(msg, title="Canale Offline", timeout=4)
                self.active_tab = "vods"
                self.cursor_index = 0
                self._fetch_vods_bg(item.login, item.display_name)
                return

            self._was_starting_mpv = True
            ok, msg = self.client.player.watch_stream(
                channel_or_url=item.login,
                title=f"{item.display_name} — {item.game_name}",
                quality=self.client.quality,
                low_latency=bool(self.client.config.get("low_latency", True)),
                oauth_token=self.client.oauth_token,
            )
            self._set_banner(msg)
            self.notify(msg, title="Avvio Stream MPV", timeout=4)
            self._render_hero()
            self._render_list()

        elif isinstance(item, VodInfo):
            vod_url = f"https://www.twitch.tv/videos/{item.id}"
            self._was_starting_mpv = True
            ok, msg = self.client.player.watch_stream(
                channel_or_url=vod_url,
                title=f"[VOD] {item.channel_name}: {item.title}",
                quality=self.client.quality,
                low_latency=False,
                oauth_token=self.client.oauth_token,
            )
            self._set_banner(msg)
            self.notify(msg, title="Avvio VOD MPV", timeout=4)
            self._render_hero()
            self._render_list()

    def action_listen_audio(self) -> None:
        item = self._selected_item()
        if isinstance(item, ChannelInfo) and item.is_live:
            self.chat.join_channel(item.login)
            self._was_starting_mpv = True
            ok, msg = self.client.player.watch_stream(
                channel_or_url=item.login,
                title=f"{item.display_name} (Audio)",
                quality="audio_only",
                low_latency=True,
                oauth_token=self.client.oauth_token,
            )
            self._set_banner(msg)
            self.notify(msg, title="Modalità Solo Audio", timeout=4)
            self._render_hero()
            self._render_list()
        elif isinstance(item, VodInfo):
            vod_url = f"https://www.twitch.tv/videos/{item.id}"
            self._was_starting_mpv = True
            ok, msg = self.client.player.watch_stream(
                channel_or_url=vod_url,
                title=f"[VOD Audio] {item.channel_name}",
                quality="audio_only",
                low_latency=False,
                oauth_token=self.client.oauth_token,
            )
            self._set_banner(msg)
            self.notify(msg, title="Modalità Solo Audio", timeout=4)
            self._render_hero()
            self._render_list()

    def action_stop_stream(self) -> None:
        self._was_starting_mpv = False
        self.client.player.stop()
        self._set_banner("⏹ Stream MPV fermato.")
        self.notify("Stream MPV chiuso.", title="Twitch Player", timeout=2)
        self._render_hero()
        self._render_list()

    def action_cycle_quality(self) -> None:
        q = self.client.cycle_quality()
        self._set_banner(f"🎞 Qualità stream impostata su: {q}")
        self.notify(f"Qualità predefinita: {q}", title="Qualità Video [v]", timeout=2)
        self._render_top_bar()

    def action_toggle_follow(self) -> None:
        ch = self._selected_channel()
        if not ch:
            return
        now_followed = self.client.toggle_follow(ch.login)
        ch.is_followed = now_followed
        if now_followed:
            if not any(c.login == ch.login for c in self.followed_channels):
                self.followed_channels.append(ch)
                self.followed_channels.sort(
                    key=lambda c: (not c.is_live, -c.viewers_count, c.display_name.lower())
                )
            self._set_banner(f"★ Ora segui {ch.display_name}")
            self.notify(f"★ Aggiunto {ch.display_name} ai seguiti", timeout=2)
        else:
            self.followed_channels = [c for c in self.followed_channels if c.login != ch.login]
            self._clamp_cursor()
            self._set_banner(f"Rimosso {ch.display_name} dai seguiti")
            self.notify(f"Rimosso {ch.display_name} dai seguiti", timeout=2)
        self._refresh_all_views()

    def action_lurk_chat(self) -> None:
        ch = self._selected_channel()
        if ch:
            self.chat.join_channel(ch.login)
            self._set_banner(f"💬 Connesso alla chat di #{ch.login}")
            self._render_chat()

    def action_toggle_chat_panel(self) -> None:
        self.chat_visible = not self.chat_visible
        try:
            chat_card = self.query_one("#chat-card", Vertical)
            if self.chat_visible:
                chat_card.remove_class("hidden")
            else:
                chat_card.add_class("hidden")
        except Exception:
            pass
        self._render_chat()

    def action_focus_chat_input(self) -> None:
        if not self.chat_visible:
            self.action_toggle_chat_panel()
        if not self.chat.can_send:
            self.notify(
                "Avvio il Login Twitch nel browser per permetterti di scrivere in chat!",
                title="Login richiesto per scrivere",
                timeout=4,
            )
            self.action_start_browser_login()
            return
        inp = self.query_one("#chat-input", Input)
        inp.disabled = False
        inp.add_class("visible")
        inp.focus()

    def action_open_search(self) -> None:
        inp = self.query_one("#search-input", Input)
        inp.disabled = False
        inp.add_class("visible")
        inp.value = ""
        inp.focus()

    def action_open_auth(self) -> None:
        inp = self.query_one("#auth-input", Input)
        inp.disabled = False
        inp.add_class("visible")
        inp.value = ""
        inp.focus()

    @work(thread=True)
    def action_start_browser_login(self) -> None:
        """Starts 1-Click Twitch OAuth Device Activation flow in browser and polls until authorized."""
        self.call_from_thread(
            self._set_banner, "🔑 Richiesta codice di attivazione Twitch in corso…"
        )
        dev = self.client.start_device_auth()
        if not dev or not dev.get("user_code"):
            self.call_from_thread(
                self.notify,
                "Impossibile avviare il login automatico. Premi Shift+A per incollare un token manuale.",
                title="Errore Login",
                severity="error",
            )
            return

        user_code = str(dev["user_code"])
        device_code = str(dev["device_code"])
        verify_uri = str(dev.get("verification_uri") or "https://www.twitch.tv/activate")
        interval = max(3, int(dev.get("interval") or 5))
        full_url = f"{verify_uri}?public=true&device-code={user_code}"

        wl_copy = shutil.which("wl-copy")
        if wl_copy:
            try:
                subprocess.Popen(
                    [wl_copy, user_code],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception:
                pass

        xdg_open = shutil.which("xdg-open")
        try:
            if xdg_open:
                subprocess.Popen(
                    [xdg_open, full_url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            else:
                webbrowser.open(full_url)
        except Exception:
            pass

        self.device_login_code = user_code
        msg = f"🔑 CODICE TWITCH: {user_code} (copiato negli appunti! Conferma nel browser aperto…)"
        self.call_from_thread(self._set_banner, msg, 120.0)
        self.call_from_thread(self._render_top_bar)
        self.call_from_thread(
            self.notify,
            f"Codice: {user_code} (già copiato negli appunti).\nPremi 'Attiva / Autorizza' nella pagina aperta nel browser!",
            title="🔑 Login Twitch in corso",
            timeout=12,
        )

        deadline = time.time() + 180
        while time.time() < deadline and self.device_login_code == user_code:
            time.sleep(interval)
            token = self.client.poll_device_auth(device_code)
            if token:
                self.device_login_code = ""
                ok, auth_msg = self.client.set_oauth_credentials(token)
                self.chat.update_credentials(self.client.oauth_token, self.client.username)
                _, sync_msg = self.client.sync_oauth_follows()
                self.followed_channels = self.client.get_followed_channels()
                done_msg = f"✅ {auth_msg} • {sync_msg}"
                self.call_from_thread(self._set_banner, done_msg)
                self.call_from_thread(
                    self.notify,
                    done_msg,
                    title="Login Twitch Completato!",
                    timeout=6,
                )
                self.call_from_thread(self._on_data_loaded)
                return

        if self.device_login_code == user_code:
            self.device_login_code = ""
            self.call_from_thread(self._set_banner, "⏱ Tempo scaduto per il codice di login.")

    @work(thread=True)
    def action_sync_oauth_follows(self) -> None:
        if not self.client.oauth_token:
            self.call_from_thread(
                self.notify,
                "Premi 'u' per fare il Login con Twitch prima di sincronizzare i seguiti.",
                title="Login Richiesto",
            )
            return
        self.call_from_thread(self._set_banner, "🔄 Sincronizzazione canali seguiti da Twitch…")
        added, msg = self.client.sync_oauth_follows()
        self.chat.update_credentials(self.client.oauth_token, self.client.username)
        channels = self.client.get_followed_channels()
        self.followed_channels = channels
        self.call_from_thread(self._set_banner, msg)
        self.call_from_thread(self.notify, msg, title="Sincronizzazione Twitch", timeout=4)
        self.call_from_thread(self._on_data_loaded)

    def action_cancel_or_back(self) -> None:
        if self.device_login_code:
            self.device_login_code = ""
            self._set_banner("Login annullato.")
        for sel in ("#search-input", "#auth-input", "#chat-input"):
            try:
                inp = self.query_one(sel, Input)
                if inp.has_class("visible"):
                    inp.remove_class("visible")
                    inp.disabled = True
                    self.set_focus(None)
                    return
            except Exception:
                pass
        if self.active_tab == "search":
            self.action_tab_followed()
        elif self.active_tab == "top" and self.selected_category:
            self.selected_category = None
            self._fetch_top_streams_bg(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        inp_id = event.input.id
        val = event.value.strip()
        event.input.remove_class("visible")
        event.input.disabled = True
        self.set_focus(None)

        if inp_id == "search-input" and val:
            self.active_tab = "search"
            self.cursor_index = 0
            self._fetch_search_bg(val)
        elif inp_id == "auth-input":
            self._apply_oauth_bg(val)
        elif inp_id == "chat-input" and val:
            ok, msg = self.chat.send_message(val)
            if not ok:
                self._set_banner(msg)
                self.notify(msg, title="Errore Chat", severity="warning")
            event.input.value = ""
            self._render_chat()

    @work(thread=True)
    def _apply_oauth_bg(self, token: str) -> None:
        ok, msg = self.client.set_oauth_credentials(token)
        self.chat.update_credentials(self.client.oauth_token, self.client.username)
        if self.client.oauth_token:
            _, sync_msg = self.client.sync_oauth_follows()
            self.followed_channels = self.client.get_followed_channels()
            msg = f"{msg} • {sync_msg}"
        self.call_from_thread(self._set_banner, msg)
        self.call_from_thread(self.notify, msg, title="Autenticazione Twitch", timeout=4)
        self.call_from_thread(self._on_data_loaded)

    def action_open_browser(self) -> None:
        item = self._selected_item()
        xdg_open = shutil.which("xdg-open")
        if isinstance(item, ChannelInfo):
            url = f"https://www.twitch.tv/{item.login}"
            if xdg_open:
                subprocess.Popen([xdg_open, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            else:
                webbrowser.open(url)
            self._set_banner(f"🌐 Aperto twitch.tv/{item.login} nel browser")
        elif isinstance(item, VodInfo):
            url = f"https://www.twitch.tv/videos/{item.id}"
            if xdg_open:
                subprocess.Popen([xdg_open, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            else:
                webbrowser.open(url)
            self._set_banner("🌐 Aperto VOD nel browser")

    def action_refresh_data(self) -> None:
        self._set_banner("🔄 Aggiornamento dati Twitch…")
        if self.active_tab == "followed":
            self._fetch_followed_bg()
        elif self.active_tab == "top":
            self._fetch_top_streams_bg(self.selected_category)
        elif self.active_tab == "categories":
            self._fetch_categories_bg()
        elif self.active_tab == "vods" and self.vod_channel_login:
            self._fetch_vods_bg(self.vod_channel_login, self.vod_channel_name)

    def action_toggle_theme(self) -> None:
        self.dynamic_theme = not self.dynamic_theme
        self.client.config["dynamic_theme"] = self.dynamic_theme
        self.client.save_config()
        if not self.dynamic_theme:
            self.palette = DRACULA_PALETTE
            self._apply_palette(self.palette)
        else:
            self._update_selected_artwork()
        mode_name = "Dynamic Stream Artwork" if self.dynamic_theme else "Dracula Twitch"
        self._set_banner(f"🎨 Tema: {mode_name}")
        self._refresh_all_views()

    def action_mpv_pause(self) -> None:
        if self.client.player.toggle_pause():
            self._render_hero()

    def action_mpv_mute(self) -> None:
        if self.client.player.toggle_mute():
            self._render_hero()

    def action_mpv_vol_up(self) -> None:
        vol = self.client.player.adjust_volume(5)
        if vol is not None:
            self._render_hero()

    def action_mpv_vol_down(self) -> None:
        vol = self.client.player.adjust_volume(-5)
        if vol is not None:
            self._render_hero()


def run_tui(client: TwitchClient | None = None) -> None:
    app = TwitchApp(client or TwitchClient())
    app.run()
