"""
FRITZ!Box Overengineered Dashboard (FOD)
----------------------------------------
A thoroughly unnecessary Textual TUI for watching every byte, packet, photon,
and existential anomaly passing through your AVM FRITZ!Box.

Six tabs of pure feature creep:
  1. Overview        — live gauges + sparklines
  2. DSL Deep Dive   — line quality, error vectors, bitswap counts
  3. WLAN            — all bands + guest net
  4. Hosts           — every device on the LAN/WLAN
  5. System          — firmware, uptime, build info
  6. TR-064 Explorer — pick any service/action and fire it raw

Install:
    pip install textual fritzconnection

Run:
    python fritz_dashboard.py
"""

from __future__ import annotations

import os
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Any

import logging

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, ScrollableContainer, Vertical
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Log,
    ProgressBar,
    Select,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
)

from fritzconnection import FritzConnection
from fritzconnection.core.exceptions import FritzConnectionException
from dotenv import load_dotenv

from dns_sync import sync_once, load_tsig_key, load_overrides

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    filename="fritzdash.log",
)
log = logging.getLogger("fritzdash")

# Pull values from a local .env if present. Real shell env vars win.
load_dotenv()

#fmt: off

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG = {
    "address":              os.environ.get("FRITZ_ADDRESS", "ip.of.your.fritz.box"),
    "user":                 os.environ.get("FRITZ_USERNAME", "fritz.box.username.or.empty.for.default"),
    "password":             os.environ.get("FRITZ_PASSWORD", "fritz.box.password"),
    "refresh_seconds":      3,    # how often to poll (overview has fast-refresh)
    "history_points":       120,  # sparkline buffer — 120 × 3s = 6 min window
    "dnszone":              os.environ.get("DNS_ZONE", "home.lan"),
    "dnsserver":            os.environ.get("DNS_SERVER", "ip.of.your.dns.server"),
    "tsigkey_path":         os.environ.get("TSIG_KEY_PATH", "path.to.tsig.key"),
    
    # MAC -> DNS-label overrides loaded from a YAML file (see
    # dns_overrides.yaml.example). Empty mapping if DNS_OVERRIDES_FILE is
    # unset or the file is missing.
    "dnsoverrides":         load_overrides(os.environ.get("DNS_OVERRIDES_FILE")),

    # Hostnames the script must NEVER delete from the zone, even if they
    # disappear from the FRITZ!Box host list. Hand-curated records like
    # NS targets, service aliases, static infra. Comma-separated in env.
    "protectednames":       frozenset(
        n.strip()
        for n in os.environ.get("DNS_PROTECTED_NAMES", "").split(",")
        if n.strip()
    ),
}

#fmt: on
# ---------------------------------------------------------------------------
# TR-064 helpers — everything isolated so a missing action never crashes
# ---------------------------------------------------------------------------


def safe(fc: FritzConnection, service: str, action: str, **kwargs) -> dict[str, Any]:
    """Call a TR-064 action, return {} on any failure."""
    try:
        return fc.call_action(service, action, **kwargs)
    except Exception:  # noqa: BLE001
        return {}


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} EB"


def fmt_uptime(s: int) -> str:
    if s <= 0:
        return "—"
    td = timedelta(seconds=s)
    d, rem = td.days, td.seconds
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Grafana-ish dark theme (CSS)
# ---------------------------------------------------------------------------

CSS = """
Screen {
    background: #0b0e14;
}

Header {
    background: #161b22;
    color: #58a6ff;
}

Footer {
    background: #161b22;
}

TabbedContent {
    background: #0b0e14;
}

Tabs {
    background: #161b22;
}

Tab {
    color: #8b949e;
}

Tab.-active {
    color: #58a6ff;
    text-style: bold;
}

.panel {
    border: round #30363d;
    background: #0d1117;
    padding: 0 2;
    height: auto;
    min-height: 5;
}

.panel-title {
    color: #58a6ff;
    text-style: bold;
    margin-bottom: 1;
}

.metric-big {
    color: #e6edf3;
    text-style: bold;
}

.metric-label {
    color: #8b949e;
}

.metric-ok {
    color: #3fb950;
}

.metric-warn {
    color: #d29922;
}

.metric-bad {
    color: #f85149;
}

.overview-grid {
    grid-size: 2 3;
    grid-gutter: 1 2;
    grid-rows: 5 5 5;
    padding: 1 2;
    height: 19;
}

Sparkline {
    height: 5;
    margin-top: 1;
}

.sparkline-down > .sparkline--max-color { color: #58a6ff; }
.sparkline-down > .sparkline--min-color { color: #1f6feb; }

.sparkline-up > .sparkline--max-color { color: #3fb950; }
.sparkline-up > .sparkline--min-color { color: #238636; }

.sparkline-snr > .sparkline--max-color { color: #d29922; }
.sparkline-snr > .sparkline--min-color { color: #9e6a03; }

DataTable {
    background: #0d1117;
    height: auto;
    max-height: 40;
}

DataTable > .datatable--header {
    background: #161b22;
    color: #58a6ff;
    text-style: bold;
}

Input {
    background: #161b22;
    color: #e6edf3;
}

Select {
    background: #161b22;
}

#explorer-output {
    background: #010409;
    color: #3fb950;
    border: round #30363d;
    height: 20;
    padding: 1;
}

Button {
    background: #238636;
    color: #ffffff;
}

Button:hover {
    background: #2ea043;
}

Button.-warning {
    background: #9e6a03;
}

Button.-warning:hover {
    background: #d29922;
}

#hosts-actions {
    height: auto;
    padding: 1 2;
}

#wake-button {
    margin-right: 2;
}

#sync-button {
    margin-right: 2;
}

#wake-status {
    padding: 1 0;
    color: #8b949e;
}

#sync-status {
    padding: 1 0;
    color: #8b949e;
}

#connection-banner {
    dock: top;
    height: 1;
    background: #f85149;
    color: #ffffff;
    text-align: center;
    text-style: bold;
    display: none;
}

#connection-banner.visible {
    display: block;
}
"""


# ---------------------------------------------------------------------------
# Custom widgets
# ---------------------------------------------------------------------------


class MetricCard(Static):
    """A single big-number metric card, Grafana-style.

    Call `.set_metric(value, status, subtitle)` to update it. We use direct
    .update() with Rich markup instead of reactive attributes, because
    Static doesn't auto-re-render on reactive changes unless you wire it up
    explicitly — and .update() is simpler.
    """

    STATUS_COLORS = {"ok": "#3fb950", "warn": "#d29922", "bad": "#f85149"}

    def __init__(self, label: str, **kwargs):
        super().__init__(**kwargs)
        self.label = label
        self.add_class("panel")

    def on_mount(self) -> None:
        # Initial state: show just the label + placeholder
        self.set_metric("—", "ok", "")

    def set_metric(self, value: str, status: str = "ok", subtitle: str = "") -> None:
        color = self.STATUS_COLORS.get(status, "#e6edf3")
        lines = [
            f"[#8b949e]{self.label}[/]",
            f"[bold {color}]{value}[/]",
        ]
        if subtitle:
            lines.append(f"[#8b949e]{subtitle}[/]")
        self.update("\n".join(lines))


class GraphPanel(Static):
    """A panel with a title and a sparkline underneath."""

    def __init__(self, title: str, sparkline_id: str, sparkline_class: str, **kw):
        super().__init__(**kw)
        self.title = title
        self.sparkline_id = sparkline_id
        self.sparkline_class = sparkline_class
        self.add_class("panel")

    def compose(self) -> ComposeResult:
        yield Label(self.title, classes="panel-title")
        yield Label("—", id=f"{self.sparkline_id}-label")
        yield Sparkline(
            [0],
            id=self.sparkline_id,
            classes=self.sparkline_class,
            summary_function=max,
        )


# ---------------------------------------------------------------------------
# The App
# ---------------------------------------------------------------------------


class FritzDashboard(App):
    CSS = CSS
    TITLE = "FRITZ!Box Overengineered Dashboard"
    SUB_TITLE = "FOD v1.0"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("w", "wake_selected", "Wake"),
        Binding("s", "sync_dns", "Sync"),
        Binding("1", "show_tab('overview')", "Overview"),
        Binding("2", "show_tab('dsl')", "DSL"),
        Binding("3", "show_tab('wlan')", "WLAN"),
        Binding("4", "show_tab('hosts')", "Hosts"),
        Binding("5", "show_tab('system')", "System"),
        Binding("6", "show_tab('explorer')", "Explorer"),
    ]

    fc: FritzConnection | None = None

    # TSIG key for DNS dynamic updates — loaded once at startup from
    # CONFIG["tsigkey_path"]. None means key file missing/unreadable;
    # the sync button surfaces the error rather than silently failing.
    _tsig_keyring: Any = None
    _tsig_algorithm: Any = None
    _tsig_error: str | None = None

    # Ring buffers for sparklines
    hist_down_mbps: deque[float]
    hist_up_mbps: deque[float]
    hist_snr_down: deque[float]
    hist_snr_up: deque[float]
    prev_bytes_recv: int = 0
    prev_bytes_sent: int = 0
    prev_poll_time: float = 0.0

    # Map DataTable row_key -> (name, mac) so Wake can look up the MAC of
    # whichever host is currently selected.
    row_to_host: dict[Any, tuple[str, str]]

    def __init__(self):
        super().__init__()
        n = CONFIG["history_points"]
        self.hist_down_mbps = deque([0.0] * n, maxlen=n)
        self.hist_up_mbps = deque([0.0] * n, maxlen=n)
        self.hist_snr_down = deque([0.0] * n, maxlen=n)
        self.hist_snr_up = deque([0.0] * n, maxlen=n)
        self.row_to_host = {}

    # -------------------- layout --------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="connection-banner")

        with TabbedContent(initial="overview"):
            # --- Overview ---
            with TabPane("[1] Overview", id="overview"):
                with ScrollableContainer():
                    with Grid(classes="overview-grid"):
                        yield MetricCard("Connection", id="card-conn")
                        yield MetricCard("External IP", id="card-ip")
                        yield MetricCard("DSL Sync ↓", id="card-sync-down")
                        yield MetricCard("DSL Sync ↑", id="card-sync-up")
                        yield MetricCard("WAN Uptime", id="card-uptime")
                        yield MetricCard("Total Traffic", id="card-traffic")
                    yield GraphPanel(
                        "Throughput ↓ (Mbps)", "spark-down", "sparkline-down"
                    )
                    yield GraphPanel("Throughput ↑ (Mbps)", "spark-up", "sparkline-up")
                    yield GraphPanel("SNR Margin ↓ (dB)", "spark-snr", "sparkline-snr")

            # --- DSL ---
            with TabPane("[2] DSL", id="dsl"):
                with ScrollableContainer():
                    yield Static(id="dsl-content", classes="panel")

            # --- WLAN ---
            with TabPane("[3] WLAN", id="wlan"):
                with ScrollableContainer():
                    yield Static(id="wlan-content", classes="panel")

            # --- Hosts ---
            with TabPane("[4] Hosts", id="hosts"):
                with Vertical():
                    yield Label(
                        "Connected devices — [r] refresh  ·  [w] wake selected  ·  [s] sync DNS",
                        classes="panel-title",
                    )
                    yield DataTable(
                        id="hosts-table", cursor_type="row", zebra_stripes=True
                    )
                    with Horizontal(id="hosts-actions"):
                        yield Button(
                            "⚡ Wake selected device",
                            id="wake-button",
                            variant="warning",
                        )
                        yield Button(
                            "🔄 Sync DNS now", id="sync-button", variant="success"
                        )
                        yield Label("", id="wake-status")
                        yield Label("", id="sync-status")

            # --- System ---
            with TabPane("[5] System", id="system"):
                with ScrollableContainer():
                    yield Static(id="system-content", classes="panel")

            # --- TR-064 Explorer ---
            with TabPane("[6] Explorer", id="explorer"):
                with Vertical():
                    yield Label(
                        "🔬 Raw TR-064 Explorer — fire any service.action",
                        classes="panel-title",
                    )
                    with Horizontal():
                        yield Select(
                            [],
                            prompt="Service",
                            id="explorer-service",
                            allow_blank=True,
                        )
                        yield Select(
                            [], prompt="Action", id="explorer-action", allow_blank=True
                        )
                        yield Button("▶ Call", id="explorer-go", variant="success")
                    yield Log(id="explorer-output", highlight=True)

        yield Footer()

    # -------------------- lifecycle --------------------

    def on_mount(self) -> None:
        # Hosts table columns
        tbl = self.query_one("#hosts-table", DataTable)
        tbl.add_columns("Name", "IP", "MAC", "Interface", "Active", "Lease")

        # Load TSIG key once — failure is non-fatal; the sync button
        # surfaces the error when pressed.
        try:
            self._tsig_keyring, self._tsig_algorithm = load_tsig_key(
                CONFIG["tsigkey_path"]
            )
            log.info("loaded TSIG key from %s", CONFIG["tsigkey_path"])
        except Exception as e:  # noqa: BLE001
            self._tsig_error = str(e)
            log.warning("TSIG key not available: %s", e)

        # Connect in a worker so the UI mounts immediately
        self.connect_to_box()

        # Periodic refresh
        self.set_interval(CONFIG["refresh_seconds"], self.refresh_fast)
        # Heavier stuff runs less often
        self.set_interval(30, self.refresh_slow)

    @work(thread=True)
    def connect_to_box(self) -> None:
        if not CONFIG["password"]:
            self.call_from_thread(
                self.show_banner,
                "⚠  FRITZ_USERNAME / FRITZ_PASSWORD env vars not set — running in demo mode",
            )
            return
        try:
            self.fc = FritzConnection(
                address=CONFIG["address"],
                user=CONFIG["user"] or None,
                password=CONFIG["password"],
                use_cache=True,
            )
            self.call_from_thread(self.on_connected)
        except FritzConnectionException as e:
            self.call_from_thread(self.show_banner, f"❌  Could not connect: {e}")

    def on_connected(self) -> None:
        self.hide_banner()
        # Populate the Explorer's service dropdown
        if self.fc is None:
            return
        services = sorted(self.fc.services.keys())
        select = self.query_one("#explorer-service", Select)
        select.set_options([(s, s) for s in services])
        # Kick an immediate refresh
        self.refresh_fast()
        self.refresh_slow()

    def show_banner(self, msg: str) -> None:
        banner = self.query_one("#connection-banner", Static)
        banner.update(msg)
        banner.add_class("visible")

    def hide_banner(self) -> None:
        self.query_one("#connection-banner", Static).remove_class("visible")

    # -------------------- refresh cycles --------------------

    @work(thread=True, exclusive=True, group="fast")
    def refresh_fast(self) -> None:
        """Overview + graphs. Called every few seconds."""
        if self.fc is None:
            return

        dsl = safe(self.fc, "WANDSLInterfaceConfig1", "GetInfo")

        wan = safe(self.fc, "WANPPPConnection1", "GetStatusInfo") or safe(
            self.fc, "WANIPConnection1", "GetStatusInfo"
        )

        ext = safe(self.fc, "WANPPPConnection1", "GetExternalIPAddress") or safe(
            self.fc, "WANIPConnection1", "GetExternalIPAddress"
        )

        b_sent = safe(self.fc, "WANCommonInterfaceConfig1", "GetTotalBytesSent")
        b_recv = safe(self.fc, "WANCommonInterfaceConfig1", "GetTotalBytesReceived")

        # Compute instantaneous throughput
        now = time.time()
        total_sent = b_sent.get("NewTotalBytesSent", 0)
        total_recv = b_recv.get("NewTotalBytesReceived", 0)
        down_mbps = up_mbps = 0.0
        if self.prev_poll_time > 0:
            elapsed = max(0.1, now - self.prev_poll_time)
            down_mbps = max(0, (total_recv - self.prev_bytes_recv) * 8) / (
                elapsed * 1_000_000
            )
            up_mbps = max(0, (total_sent - self.prev_bytes_sent) * 8) / (
                elapsed * 1_000_000
            )
        self.prev_poll_time = now
        self.prev_bytes_recv = total_recv
        self.prev_bytes_sent = total_sent

        # Update ring buffers
        self.hist_down_mbps.append(down_mbps)
        self.hist_up_mbps.append(up_mbps)
        self.hist_snr_down.append(dsl.get("NewDownstreamNoiseMargin", 0) / 10.0)
        self.hist_snr_up.append(dsl.get("NewUpstreamNoiseMargin", 0) / 10.0)

        # Post to UI thread
        self.call_from_thread(
            self._apply_overview,
            dsl,
            wan,
            ext,
            total_sent,
            total_recv,
            down_mbps,
            up_mbps,
        )

    def _apply_overview(self, dsl, wan, ext, tx, rx, down_mbps, up_mbps):
        # Connection card
        conn = self.query_one("#card-conn", MetricCard)
        dsl_up = dsl.get("NewStatus") == "Up"
        wan_up = wan.get("NewConnectionStatus") == "Connected"
        subt = (
            f"DSL {dsl.get('NewStatus','?')} · WAN {wan.get('NewConnectionStatus','?')}"
        )
        if dsl_up and wan_up:
            conn.set_metric("● ONLINE", "ok", subt)
        elif dsl_up or wan_up:
            conn.set_metric("● PARTIAL", "warn", subt)
        else:
            conn.set_metric("● OFFLINE", "bad", "Both layers down")

        # External IP
        ip_card = self.query_one("#card-ip", MetricCard)
        ip_card.set_metric(
            ext.get("NewExternalIPAddress") or "—",
            "ok",
            "IPv4 WAN address",
        )

        # Sync rates
        sync_down = dsl.get("NewDownstreamCurrRate", 0) / 1000
        sync_up = dsl.get("NewUpstreamCurrRate", 0) / 1000
        sync_down_max = dsl.get("NewDownstreamMaxRate", 0) / 1000
        sync_up_max = dsl.get("NewUpstreamMaxRate", 0) / 1000
        self.query_one("#card-sync-down", MetricCard).set_metric(
            f"{sync_down:.1f} Mbps",
            "ok" if sync_down > 0 else "bad",
            f"max {sync_down_max:.1f} Mbps",
        )
        self.query_one("#card-sync-up", MetricCard).set_metric(
            f"{sync_up:.1f} Mbps",
            "ok" if sync_up > 0 else "bad",
            f"max {sync_up_max:.1f} Mbps",
        )

        # Uptime
        uptime = wan.get("NewUptime", 0)
        uptime_subtitle = ""
        if uptime > 0:
            since = (datetime.now() - timedelta(seconds=uptime)).strftime(
                "%Y-%m-%d %H:%M"
            )
            uptime_subtitle = f"since {since}"
        self.query_one("#card-uptime", MetricCard).set_metric(
            fmt_uptime(uptime),
            "ok",
            uptime_subtitle,
        )

        # Traffic totals
        self.query_one("#card-traffic", MetricCard).set_metric(
            f"↓ {fmt_bytes(rx)}",
            "ok",
            f"↑ {fmt_bytes(tx)}",
        )

        # Sparklines + labels
        def update_spark(spark_id: str, data: deque[float], unit: str) -> None:
            spark = self.query_one(f"#{spark_id}", Sparkline)
            spark.data = list(data)
            last = data[-1] if data else 0.0
            avg = sum(data) / max(1, len(data))
            peak = max(data) if data else 0.0
            self.query_one(f"#{spark_id}-label", Label).update(
                f"now [bold]{last:.2f}[/] {unit}   avg {avg:.2f}   peak {peak:.2f}"
            )

        update_spark("spark-down", self.hist_down_mbps, "Mbps")
        update_spark("spark-up", self.hist_up_mbps, "Mbps")
        update_spark("spark-snr", self.hist_snr_down, "dB")

    @work(thread=True, exclusive=True, group="slow")
    def refresh_slow(self) -> None:
        """DSL deep dive, WLAN, Hosts, System — called less frequently."""
        if self.fc is None:
            return

        dsl_info = safe(self.fc, "WANDSLInterfaceConfig1", "GetInfo")
        dsl_stats = safe(self.fc, "WANDSLInterfaceConfig1", "GetStatisticsTotal")
        device_info = safe(self.fc, "DeviceInfo1", "GetInfo")
        host_count = safe(self.fc, "Hosts1", "GetHostNumberOfEntries").get(
            "NewHostNumberOfEntries", 0
        )

        wlan_info = []
        for i in (1, 2, 3):
            info = safe(self.fc, f"WLANConfiguration{i}", "GetInfo")
            if info:
                wlan_info.append((i, info))

        # Gather hosts
        hosts = []
        for idx in range(host_count):
            h = safe(self.fc, "Hosts1", "GetGenericHostEntry", NewIndex=idx)
            if h:
                hosts.append(h)

        self.call_from_thread(
            self._apply_slow,
            dsl_info,
            dsl_stats,
            device_info,
            wlan_info,
            hosts,
        )

    def _apply_slow(self, dsl, stats, dev, wlans, hosts):
        # ---- DSL tab ----
        snr_down = dsl.get("NewDownstreamNoiseMargin", 0) / 10
        snr_up = dsl.get("NewUpstreamNoiseMargin", 0) / 10
        att_down = dsl.get("NewDownstreamAttenuation", 0) / 10
        att_up = dsl.get("NewUpstreamAttenuation", 0) / 10

        def verdict(value: float, warn: float, bad: float) -> str:
            if value < bad:
                return f"[#f85149]{value:.1f}[/]"
            if value < warn:
                return f"[#d29922]{value:.1f}[/]"
            return f"[#3fb950]{value:.1f}[/]"

        dsl_text = (
            "[bold #58a6ff]📡 DSL Line Report[/]\n\n"
            f"[#8b949e]Status:[/]      [bold]{dsl.get('NewStatus','?')}[/]\n"
            f"[#8b949e]Modulation:[/]  {dsl.get('NewModulationType','?')}\n"
            f"[#8b949e]Standard:[/]    {dsl.get('NewStandard','?')}\n\n"
            "[bold #58a6ff]Sync Rates[/]\n"
            f"  Current      ↓ {dsl.get('NewDownstreamCurrRate',0)/1000:7.1f} Mbps   ↑ {dsl.get('NewUpstreamCurrRate',0)/1000:7.1f} Mbps\n"
            f"  Maximum      ↓ {dsl.get('NewDownstreamMaxRate',0)/1000:7.1f} Mbps   ↑ {dsl.get('NewUpstreamMaxRate',0)/1000:7.1f} Mbps\n\n"
            "[bold #58a6ff]Line Quality[/]\n"
            f"  SNR margin   ↓ {verdict(snr_down, 6, 3)} dB       ↑ {verdict(snr_up, 6, 3)} dB\n"
            f"  Attenuation  ↓ {att_down:.1f} dB       ↑ {att_up:.1f} dB\n\n"
            "[bold #58a6ff]Error Counters (cumulative since last resync)[/]\n"
            f"  CRC errors:               {dsl.get('NewCRCErrors', 0):>10}\n"
            f"  FEC errors:               {dsl.get('NewFECErrors', 0):>10}\n"
            f"  HEC errors:               {dsl.get('NewHECErrors', 0):>10}\n"
            f"  ATU-C CRC errors:         {dsl.get('NewATUCCRCErrors', 0):>10}\n"
            f"  ATU-C FEC errors:         {dsl.get('NewATUCFECErrors', 0):>10}\n"
            f"  ATU-C HEC errors:         {dsl.get('NewATUCHECErrors', 0):>10}\n\n"
            "[bold #58a6ff]Statistics Totals[/]\n"
            f"  Received blocks:          {stats.get('NewReceiveBlocks', 0):>10}\n"
            f"  Transmitted blocks:       {stats.get('NewTransmitBlocks', 0):>10}\n"
            f"  Cell delineation (CD):    {stats.get('NewCellDelin', 0):>10}\n"
            f"  Link retrain count:       {stats.get('NewLinkRetrain', 0):>10}\n"
            f"  Init errors:              {stats.get('NewInitErrors', 0):>10}\n"
            f"  Init timeouts:            {stats.get('NewInitTimeouts', 0):>10}\n"
            f"  Loss of framing:          {stats.get('NewLossOfFraming', 0):>10}\n"
            f"  Errored seconds:          {stats.get('NewErroredSecs', 0):>10}\n"
            f"  Severely errored secs:    {stats.get('NewSeverelyErroredSecs', 0):>10}\n"
            f"  FEC errors:               {stats.get('NewFECErrors', 0):>10}\n"
            f"  ATU-C FEC errors:         {stats.get('NewATUCFECErrors', 0):>10}\n"
            f"  HEC errors:               {stats.get('NewHECErrors', 0):>10}\n"
            f"  ATU-C HEC errors:         {stats.get('NewATUCHECErrors', 0):>10}\n"
            f"  CRC errors:               {stats.get('NewCRCErrors', 0):>10}\n"
            f"  ATU-C CRC errors:         {stats.get('NewATUCCRCErrors', 0):>10}\n"
        )
        self.query_one("#dsl-content", Static).update(dsl_text)

        # ---- WLAN tab ----
        wlan_lines = ["[bold #58a6ff]📶 Wireless LAN Configuration[/]\n"]
        band_name = {1: "2.4 GHz", 2: "5 GHz", 3: "Guest"}
        for idx, info in wlans:
            enabled = (
                "[#3fb950]● on[/]" if info.get("NewEnable") else "[#8b949e]○ off[/]"
            )
            wlan_lines.append(
                f"\n[bold]{band_name.get(idx, f'Radio {idx}')}[/]  {enabled}\n"
                f"  SSID:       {info.get('NewSSID','—')}\n"
                f"  Channel:    {info.get('NewChannel','—')}\n"
                f"  Standard:   {info.get('NewStandard','—')}\n"
                f"  Security:   {info.get('NewBeaconType','—')}\n"
                f"  MAC:        {info.get('NewBSSID','—')}\n"
                f"  Max bitrate:{info.get('NewMaxBitRate','—')}\n"
            )
        if len(wlans) < 2:
            wlan_lines.append(
                "\n[#8b949e](some bands returned no data — may be disabled)[/]"
            )
        self.query_one("#wlan-content", Static).update("\n".join(wlan_lines))

        # ---- Hosts table ----
        tbl = self.query_one("#hosts-table", DataTable)
        tbl.clear()
        self.row_to_host.clear()
        for h in hosts:
            active = "[#3fb950]●[/]" if h.get("NewActive") else "[#8b949e]○[/]"
            lease = h.get("NewLeaseTimeRemaining", 0)
            lease_str = fmt_uptime(lease) if lease > 0 else "static"
            name = h.get("NewHostName", "?") or "(unnamed)"
            mac = h.get("NewMACAddress", "") or ""
            row_key = tbl.add_row(
                name,
                h.get("NewIPAddress", "—") or "—",
                mac or "—",
                h.get("NewInterfaceType", "—") or "—",
                active,
                lease_str,
            )
            if mac:
                self.row_to_host[row_key] = (name, mac)

        # Cache host list so the manual DNS-sync button can use it without
        # firing another TR-064 round trip.
        self._last_hosts = hosts

        # ---- System tab ----
        sys_text = (
            "[bold #58a6ff]🖥  System Information[/]\n\n"
            f"[#8b949e]Model:[/]              {dev.get('NewModelName', '?')}\n"
            f"[#8b949e]Manufacturer:[/]       {dev.get('NewManufacturerName', '?')} ({dev.get('NewManufacturerOUI','?')})\n"
            f"[#8b949e]Description:[/]        {dev.get('NewDescription', '?')}\n"
            f"[#8b949e]Serial number:[/]      {dev.get('NewSerialNumber', '?')}\n"
            f"[#8b949e]Hardware version:[/]   {dev.get('NewHardwareVersion', '?')}\n"
            f"[#8b949e]Software version:[/]   {dev.get('NewSoftwareVersion', '?')}\n"
            f"[#8b949e]Provisioning code:[/]  {dev.get('NewProvisioningCode', '?')}\n"
            f"[#8b949e]Device uptime:[/]      {fmt_uptime(dev.get('NewUpTime', 0))}\n"
            f"[#8b949e]Device log (last bytes):[/]\n"
            f"  {(dev.get('NewDeviceLog','') or '(empty)').splitlines()[0] if dev.get('NewDeviceLog') else '(empty)'}\n\n"
            "[bold #58a6ff]TR-064 Services Discovered[/]\n"
            f"  {len(self.fc.services) if self.fc else 0} services available "
            "(see the Explorer tab to poke at them)\n\n"
            "[#8b949e]Environmental readings (CPU temp, RAM usage, etc.) are not[/]\n"
            "[#8b949e]exposed over TR-064 by AVM. For those, you'd need to scrape[/]\n"
            "[#8b949e]the web UI or use AVM's AHA-HTTP interface — a problem for[/]\n"
            "[#8b949e]future Andrew to regret inviting.[/]"
        )
        self.query_one("#system-content", Static).update(sys_text)

    # -------------------- Wake-on-LAN --------------------

    @on(Button.Pressed, "#wake-button")
    def on_wake_button(self) -> None:
        self.action_wake_selected()

    def action_wake_selected(self) -> None:
        """Wake the host selected in the Hosts DataTable via the FRITZ!Box."""
        # Only meaningful when the Hosts tab is active
        tabs = self.query_one(TabbedContent)
        if tabs.active != "hosts":
            tabs.active = "hosts"
            return

        tbl = self.query_one("#hosts-table", DataTable)
        status_label = self.query_one("#wake-status", Label)

        if self.fc is None:
            status_label.update("[#f85149]Not connected to FRITZ!Box.[/]")
            return

        if tbl.row_count == 0:
            status_label.update("[#d29922]No hosts — wait for the list to populate.[/]")
            return

        # cursor_row is the visual row index; map it back to the row key
        try:
            row_key = tbl.coordinate_to_cell_key(tbl.cursor_coordinate).row_key
        except Exception:  # noqa: BLE001
            status_label.update("[#d29922]Select a row first (arrow keys).[/]")
            return

        host = self.row_to_host.get(row_key)
        if host is None:
            status_label.update("[#d29922]That row has no MAC — can't wake it.[/]")
            return

        name, mac = host
        status_label.update(f"[#d29922]⚡ Waking {name} ({mac})...[/]")
        self._send_wake(name, mac)

    @work(thread=True)
    def _send_wake(self, name: str, mac: str) -> None:
        """Fire the TR-064 WoL call. Runs in a background thread so the UI
        stays responsive — the call typically returns in <1s but we don't
        want to risk blocking if the box is slow."""
        if self.fc is None:
            return
        try:
            self.fc.call_action(
                "Hosts1",
                "X_AVM-DE_WakeOnLANByMACAddress",
                NewMACAddress=mac,
            )
            self.call_from_thread(
                self._wake_result,
                f"[#3fb950]✓ Magic packet sent to {name} ({mac})[/]",
            )
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(
                self._wake_result,
                f"[#f85149]✗ Wake failed for {name}: {e}[/]",
            )

    def _wake_result(self, msg: str) -> None:
        self.query_one("#wake-status", Label).update(msg)

    # -------------------- DNS sync --------------------

    @on(Button.Pressed, "#sync-button")
    def on_sync_button(self) -> None:
        self.action_sync_dns()

    def action_sync_dns(self) -> None:
        """Push the current host list into the configured zone via TSIG-signed DNS UPDATE."""
        status_label = self.query_one("#sync-status", Label)

        if self._tsig_keyring is None:
            err = self._tsig_error or "no TSIG key loaded"
            status_label.update(f"[#f85149]✗ TSIG key unavailable: {err}[/]")
            return

        if not getattr(self, "_last_hosts", None):
            status_label.update(
                "[#d29922]No host data yet — wait for the slow refresh.[/]"
            )
            return

        status_label.update(
            f"[#d29922]🔄 Syncing {len(self._last_hosts)} hosts to {CONFIG['dnszone']}...[/]"
        )
        self._do_dns_sync(self._last_hosts)

    @work(thread=True, exclusive=True, group="dns-sync")
    def _do_dns_sync(self, hosts: list) -> None:
        """Run sync_once in a worker so the UI doesn't block on AXFR/UPDATE."""
        result = sync_once(
            hosts,
            zone=CONFIG["dnszone"],
            server=CONFIG["dnsserver"],
            overrides=CONFIG["dnsoverrides"],
            protected=CONFIG["protectednames"],
            keyring=self._tsig_keyring,
            keyalgorithm=self._tsig_algorithm,
        )
        self.call_from_thread(self._sync_result, result)

    def _sync_result(self, result) -> None:
        """Display sync outcome in the Hosts tab status label."""
        label = self.query_one("#sync-status", Label)
        if result.error:
            label.update(f"[#f85149]✗ Sync failed: {result.error}[/]")
            log.error("dns sync: %s", result.error)
        elif result.diff.empty:
            label.update(f"[#3fb950]✓ Up to date (current={result.current_count})[/]")
            log.info("dns sync: no changes (current=%d)", result.current_count)
        else:
            label.update(
                f"[#3fb950]✓ Synced {result.diff.summary()} "
                f"(desired={result.desired_count})[/]"
            )
            log.info(
                "dns sync %s (desired=%d, current=%d)",
                result.diff.summary(),
                result.desired_count,
                result.current_count,
            )

    # -------------------- Explorer tab --------------------

    @on(Select.Changed, "#explorer-service")
    def on_service_changed(self, event: Select.Changed) -> None:
        if self.fc is None or event.value == Select.BLANK:
            return
        service = self.fc.services.get(str(event.value))
        if service is None:
            return
        actions = sorted(service.actions.keys())
        action_select = self.query_one("#explorer-action", Select)
        action_select.set_options([(a, a) for a in actions])

    @on(Button.Pressed, "#explorer-go")
    def on_explorer_go(self) -> None:
        service_sel = self.query_one("#explorer-service", Select)
        action_sel = self.query_one("#explorer-action", Select)
        if service_sel.value == Select.BLANK or action_sel.value == Select.BLANK:
            self._log_explorer("[red]Pick a service and action first.[/]")
            return
        self._call_explorer(str(service_sel.value), str(action_sel.value))

    @work(thread=True)
    def _call_explorer(self, service: str, action: str) -> None:
        if self.fc is None:
            return
        try:
            result = self.fc.call_action(service, action)
            lines = [f"[bold #58a6ff]▶ {service}.{action}[/]"]
            for k, v in result.items():
                lines.append(f"  [#8b949e]{k}:[/] {v}")
            self.call_from_thread(self._log_explorer, "\n".join(lines))
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(
                self._log_explorer,
                f"[red]✗ {service}.{action} failed: {e}[/]",
            )

    def _log_explorer(self, text: str) -> None:
        log = self.query_one("#explorer-output", Log)
        log.write_line(text)
        log.write_line("")

    # -------------------- actions --------------------

    def action_refresh_now(self) -> None:
        self.refresh_fast()
        self.refresh_slow()

    def action_show_tab(self, tab_id: str) -> None:
        tabs = self.query_one(TabbedContent)
        tabs.active = tab_id


if __name__ == "__main__":
    FritzDashboard().run()
