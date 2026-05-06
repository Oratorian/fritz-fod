"""
FRITZ!Box Watchtool
-------------------
Monitors DSL sync, internet connectivity, line quality, and throughput on an
AVM FRITZ!Box via the TR-064 interface. Polls every N minutes, prints a
dashboard, logs to file, and alerts on state changes via Windows toast
notifications and a Discord webhook.

Requires:
    pip install fritzconnection requests winotify

FRITZ!Box settings required:
    Home Network -> Network -> Network Settings:
        [x] Allow access for applications
        [x] Transmit status information over UPnP
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import requests
from fritzconnection import FritzConnection
from fritzconnection.core.exceptions import FritzConnectionException
from dotenv import load_dotenv

try:
    from winotify import Notification, audio

    HAS_WINOTIFY = True
except ImportError:
    HAS_WINOTIFY = False

# Pull values from a local .env if present. Real shell env vars win.
load_dotenv()

#fmt: off
# ---------------------------------------------------------------------------
# Config — edit these or pull from environment variables
# ---------------------------------------------------------------------------

CONFIG = {
    "fritz_address":            os.environ.get("FRITZ_ADDRESS", ""),
    "fritz_user":               os.environ.get("FRITZ_USERNAME", ""),
    "fritz_password":           os.environ.get("FRITZ_PASSWORD", ""),
    "poll_interval_seconds":    300,  # 5 minutes
    "discord_webhook_url":      os.environ.get("DISCORD_WEBHOOK_URL", ""),
    "log_file":                 os.environ.get("LOG_FILE", "fritz_watchtool.log"),
    "state_file":               os.environ.get("STATE_FILE", "fritz_watchtool.state.json"),
    
    # Thresholds for line-quality warnings
    "snr_margin_warn_db":       6.0,  # warn if SNR margin drops below this
    "crc_rate_warn_per_min":    10,   # warn if CRC errors/minute exceed this
}

#fmt: on
# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("fritz_watchtool")
log.setLevel(logging.INFO)

_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
log.addHandler(_console)

_file = RotatingFileHandler(
    CONFIG["log_file"], maxBytes=1_000_000, backupCount=3, encoding="utf-8"
)
_file.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s"))
log.addHandler(_file)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    """A single point-in-time reading from the FRITZ!Box."""

    timestamp: str

    # DSL layer
    dsl_status: str  # "Up", "NoSignal", "Unavailable", ...
    sync_down_kbps: int  # current sync rate downstream
    sync_up_kbps: int  # current sync rate upstream
    snr_margin_down_db: float
    snr_margin_up_db: float
    attenuation_down_db: float
    attenuation_up_db: float
    crc_errors: int  # cumulative since last resync

    # WAN / PPP layer
    wan_status: str  # "Connected", "Disconnected", ...
    wan_uptime_seconds: int
    external_ip: str

    # Throughput (cumulative counters)
    bytes_sent: int
    bytes_received: int

    def connected(self) -> bool:
        return self.dsl_status == "Up" and self.wan_status == "Connected"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

# Track which (service, action) pairs have already logged a failure, so we
# don't spam the log every single poll for a permanently-missing action.
_failed_actions: set[tuple[str, str]] = set()


def _safe_call(fc: FritzConnection, service: str, action: str) -> dict[str, Any]:
    """Call a TR-064 action, returning {} on failure and logging once."""
    try:
        return fc.call_action(service, action)
    except FritzConnectionException as e:
        key = (service, action)
        if key not in _failed_actions:
            log.warning(
                "TR-064 call %s.%s failed (%s) — will skip on future polls",
                service,
                action,
                e,
            )
            _failed_actions.add(key)
        return {}


def fetch_snapshot(fc: FritzConnection) -> Snapshot:
    """Call all the TR-064 services we care about and bundle into a Snapshot.

    Each call is isolated so a single missing action on your firmware doesn't
    take down the whole poll. Missing fields just end up as defaults.
    """
    # DSL layer (present on DSL boxes — will be empty on pure-fibre setups)
    dsl = _safe_call(fc, "WANDSLInterfaceConfig1", "GetInfo")

    # WAN IP / PPP layer — try the PPP service first (PPPoE setups), then
    # fall back to IP (DHCP/bridge setups). Whichever one answers wins.
    wan_ip = _safe_call(fc, "WANPPPConnection1", "GetStatusInfo")
    if not wan_ip:
        wan_ip = _safe_call(fc, "WANIPConnection1", "GetStatusInfo")

    # External IP — try PPP first, then IP
    external_ip = ""
    ext = _safe_call(fc, "WANPPPConnection1", "GetExternalIPAddress")
    if not ext:
        ext = _safe_call(fc, "WANIPConnection1", "GetExternalIPAddress")
    if ext:
        external_ip = ext.get("NewExternalIPAddress", "") or ""

    # Throughput counters (32-bit; wrap around at 4 GB — AVM also offers a
    # 64-bit variant, but not all firmware versions expose it)
    bytes_sent = _safe_call(fc, "WANCommonInterfaceConfig1", "GetTotalBytesSent")
    bytes_recv = _safe_call(fc, "WANCommonInterfaceConfig1", "GetTotalBytesReceived")

    return Snapshot(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        dsl_status=dsl.get("NewStatus", "Unknown"),
        sync_down_kbps=dsl.get("NewDownstreamCurrRate", 0),
        sync_up_kbps=dsl.get("NewUpstreamCurrRate", 0),
        snr_margin_down_db=dsl.get("NewDownstreamNoiseMargin", 0) / 10.0,
        snr_margin_up_db=dsl.get("NewUpstreamNoiseMargin", 0) / 10.0,
        attenuation_down_db=dsl.get("NewDownstreamAttenuation", 0) / 10.0,
        attenuation_up_db=dsl.get("NewUpstreamAttenuation", 0) / 10.0,
        crc_errors=dsl.get("NewCRCErrors", 0),
        wan_status=wan_ip.get("NewConnectionStatus", "Unknown"),
        wan_uptime_seconds=wan_ip.get("NewUptime", 0),
        external_ip=external_ip,
        bytes_sent=bytes_sent.get("NewTotalBytesSent", 0),
        bytes_received=bytes_recv.get("NewTotalBytesReceived", 0),
    )


# ---------------------------------------------------------------------------
# State diffing — what counts as "something changed worth alerting about"
# ---------------------------------------------------------------------------


def detect_changes(prev: Snapshot | None, curr: Snapshot) -> list[str]:
    """Return a list of human-readable change descriptions."""
    events: list[str] = []

    if prev is None:
        # First run — announce initial state so you know it's alive
        state = "UP" if curr.connected() else "DOWN"
        events.append(f"Watchtool started — connection is {state}")
        return events

    # Connectivity transitions
    if prev.connected() and not curr.connected():
        events.append(
            f"❌ CONNECTION LOST — DSL={curr.dsl_status}, WAN={curr.wan_status}"
        )
    elif not prev.connected() and curr.connected():
        events.append(
            f"✅ CONNECTION RESTORED — "
            f"sync {curr.sync_down_kbps / 1000:.1f}/{curr.sync_up_kbps / 1000:.1f} Mbps, "
            f"IP {curr.external_ip}"
        )

    # DSL resync detection — uptime reset while we were "up"
    # (skip if we already emitted a "restored" event to avoid duplicates)
    already_reported_restore = any("RESTORED" in e for e in events)
    if (
        prev.wan_uptime_seconds > 0
        and curr.wan_uptime_seconds < prev.wan_uptime_seconds
        and curr.connected()
        and prev.connected()
        and not already_reported_restore
    ):
        events.append(
            f"⚠️  WAN reconnected silently (uptime reset from "
            f"{format_uptime(prev.wan_uptime_seconds)} to "
            f"{format_uptime(curr.wan_uptime_seconds)})"
        )

    # External IP change
    if prev.external_ip and curr.external_ip and prev.external_ip != curr.external_ip:
        events.append(
            f"🌐 External IP changed: {prev.external_ip} → {curr.external_ip}"
        )

    # Sync rate drop (>10% down) — often signals a line retrain
    if prev.sync_down_kbps > 0 and curr.sync_down_kbps > 0:
        drop = (prev.sync_down_kbps - curr.sync_down_kbps) / prev.sync_down_kbps
        if drop > 0.10:
            events.append(
                f"📉 Downstream sync dropped "
                f"{prev.sync_down_kbps / 1000:.1f} → {curr.sync_down_kbps / 1000:.1f} Mbps "
                f"({drop:.0%})"
            )

    # SNR margin warning — only alert on *entering* the warn zone
    threshold = CONFIG["snr_margin_warn_db"]
    if prev.snr_margin_down_db >= threshold > curr.snr_margin_down_db:
        events.append(
            f"⚠️  Downstream SNR margin low: {curr.snr_margin_down_db:.1f} dB "
            f"(threshold {threshold} dB) — line getting noisy"
        )

    # CRC error rate — check errors per minute since last poll
    elapsed_min = CONFIG["poll_interval_seconds"] / 60
    crc_delta = curr.crc_errors - prev.crc_errors
    # Ignore negative delta (counter reset on resync)
    if crc_delta > 0:
        rate = crc_delta / elapsed_min
        if rate > CONFIG["crc_rate_warn_per_min"]:
            events.append(
                f"⚠️  High CRC error rate: {rate:.1f}/min ({crc_delta} errors in "
                f"{elapsed_min:.0f} min)"
            )

    return events


# ---------------------------------------------------------------------------
# Dashboard rendering
# ---------------------------------------------------------------------------


def format_uptime(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    td = timedelta(seconds=seconds)
    days = td.days
    hours, rem = divmod(td.seconds, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _display_width(text: str) -> int:
    """Approximate terminal display width. Most chars are 1 wide; emoji and
    East-Asian wide chars are 2. Good enough for our headers."""
    import unicodedata

    width = 0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 2
        elif ord(ch) >= 0x1F000:  # most emoji live up here
            width += 2
        else:
            width += 1
    return width


def _pad_to_width(text: str, target: int) -> str:
    """Right-pad a string with spaces to reach a given *display* width."""
    pad = target - _display_width(text)
    return text + (" " * max(0, pad))


def render_dashboard(s: Snapshot, prev: Snapshot | None) -> str:
    """Pretty-print the current snapshot as a dashboard. The box auto-sizes
    to whatever the widest row is, so it always looks tidy regardless of
    values."""
    ok = "🟢" if s.connected() else "🔴"
    header = f"{ok}  {s.timestamp}"

    # Throughput row (only if we have a previous snapshot to diff against)
    throughput_row = None
    if prev is not None:
        elapsed = CONFIG["poll_interval_seconds"]
        down_delta = max(0, s.bytes_received - prev.bytes_received)
        up_delta = max(0, s.bytes_sent - prev.bytes_sent)
        down_mbps = (down_delta * 8) / (elapsed * 1_000_000)
        up_mbps = (up_delta * 8) / (elapsed * 1_000_000)
        throughput_row = (
            f"Avg {elapsed // 60}m",
            f"↓ {down_mbps:.2f} Mbps    ↑ {up_mbps:.2f} Mbps",
        )

    # Each row is a (label, value) pair. None rows become blank separators.
    rows: list[tuple[str, str] | None] = [
        ("DSL", f"{s.dsl_status}"),
        (
            "Sync",
            f"↓ {s.sync_down_kbps / 1000:.1f} Mbps    ↑ {s.sync_up_kbps / 1000:.1f} Mbps",
        ),
        ("SNR", f"↓ {s.snr_margin_down_db:.1f} dB    ↑ {s.snr_margin_up_db:.1f} dB"),
        (
            "Atten",
            f"↓ {s.attenuation_down_db:.1f} dB    ↑ {s.attenuation_up_db:.1f} dB",
        ),
        ("CRC", f"{s.crc_errors}"),
        None,
        ("WAN", s.wan_status),
        ("Uptime", format_uptime(s.wan_uptime_seconds)),
        ("IP", s.external_ip or "—"),
        None,
        ("Total ↓", format_bytes(s.bytes_received)),
        ("Total ↑", format_bytes(s.bytes_sent)),
    ]
    if throughput_row is not None:
        rows.append(throughput_row)

    # Work out column widths (using display width, not len())
    label_width = max(_display_width(r[0]) for r in rows if r is not None)
    value_width = max(_display_width(r[1]) for r in rows if r is not None)
    # Total inner width: "  " + label + "  " + value + "  "
    inner_width = 2 + label_width + 2 + value_width + 2
    inner_width = max(inner_width, _display_width(header) + 4)

    title = " FRITZ!Box Status "
    pad_total = inner_width - len(title)
    left = pad_total // 2
    right = pad_total - left

    lines = [
        "",
        "┌" + "─" * left + title + "─" * right + "┐",
        "│  " + _pad_to_width(header, inner_width - 2) + "│",
        "├" + "─" * inner_width + "┤",
    ]
    for r in rows:
        if r is None:
            lines.append("│" + " " * inner_width + "│")
        else:
            label, value = r
            line = "  " + _pad_to_width(label, label_width) + "  " + value
            lines.append("│" + _pad_to_width(line, inner_width) + "│")
    lines.append("└" + "─" * inner_width + "┘")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def send_windows_toast(title: str, message: str) -> None:
    if not HAS_WINOTIFY:
        log.warning("winotify not installed — skipping desktop notification")
        return
    try:
        toast = Notification(
            app_id="FRITZ!Box Watchtool",
            title=title,
            msg=message,
            duration="short",
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to show Windows toast: %s", e)


def send_discord_webhook(events: list[str], snapshot: Snapshot) -> None:
    url = CONFIG["discord_webhook_url"]
    if not url:
        return

    # One embed summarising everything, color-coded by severity
    color = 0x2ECC71 if snapshot.connected() else 0xE74C3C  # green/red
    if any("⚠️" in e or "📉" in e for e in events) and snapshot.connected():
        color = 0xF39C12  # amber for warnings while still connected

    fields = [
        {
            "name": "DSL",
            "value": (
                f"{snapshot.dsl_status} · "
                f"↓{snapshot.sync_down_kbps / 1000:.1f} / "
                f"↑{snapshot.sync_up_kbps / 1000:.1f} Mbps"
            ),
            "inline": True,
        },
        {
            "name": "WAN",
            "value": f"{snapshot.wan_status} · uptime {format_uptime(snapshot.wan_uptime_seconds)}",
            "inline": True,
        },
        {
            "name": "IP",
            "value": snapshot.external_ip or "—",
            "inline": True,
        },
    ]

    payload = {
        "username": "FRITZ!Box Watchtool",
        "embeds": [
            {
                "title": "Status change",
                "description": "\n".join(events),
                "color": color,
                "fields": fields,
                "timestamp": datetime.now().astimezone().isoformat(),
            }
        ],
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code >= 300:
            log.warning("Discord webhook returned %s: %s", r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.warning("Discord webhook failed: %s", e)


# ---------------------------------------------------------------------------
# State persistence — so alerts still work across restarts
# ---------------------------------------------------------------------------


def load_previous_snapshot() -> Snapshot | None:
    path = Path(CONFIG["state_file"])
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Snapshot(**data)
    except (json.JSONDecodeError, TypeError) as e:
        log.warning("Could not read state file (%s), starting fresh", e)
        return None


def save_snapshot(s: Snapshot) -> None:
    Path(CONFIG["state_file"]).write_text(
        json.dumps(asdict(s), indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def connect() -> FritzConnection:
    if not CONFIG["fritz_password"]:
        log.error(
            "No FRITZ!Box password configured. "
            "Set FRITZ_USERNAME and FRITZ_PASSWORD env vars or edit CONFIG."
        )
        sys.exit(1)
    return FritzConnection(
        address=CONFIG["fritz_address"],
        user=CONFIG["fritz_user"] or None,
        password=CONFIG["fritz_password"],
        use_cache=True,
    )


def probe_box(fc: FritzConnection) -> None:
    """Do an initial poll and log what worked and what didn't. Helps diagnose
    which TR-064 actions your specific firmware exposes."""
    log.info("Probing TR-064 actions...")
    snap = fetch_snapshot(fc)

    # Report what we got
    parts = []
    if snap.dsl_status != "Unknown":
        parts.append(f"DSL={snap.dsl_status}")
    if snap.wan_status != "Unknown":
        parts.append(f"WAN={snap.wan_status}")
    if snap.external_ip:
        parts.append(f"IP={snap.external_ip}")
    if snap.bytes_received > 0:
        parts.append(f"bytes_recv={snap.bytes_received}")

    if parts:
        log.info("Probe OK: %s", ", ".join(parts))
    else:
        log.warning(
            "Probe returned no usable data — check that "
            "'Allow access for applications' and 'Transmit status information "
            "over UPnP' are enabled in the FRITZ!Box web UI."
        )


def main() -> None:
    log.info("Starting FRITZ!Box Watchtool against %s", CONFIG["fritz_address"])
    try:
        fc = connect()
    except FritzConnectionException as e:
        log.error("Could not connect to FRITZ!Box: %s", e)
        sys.exit(1)

    probe_box(fc)

    prev = load_previous_snapshot()
    interval = CONFIG["poll_interval_seconds"]

    while True:
        try:
            curr = fetch_snapshot(fc)
        except FritzConnectionException as e:
            log.error("Poll failed: %s", e)
            time.sleep(interval)
            continue
        except Exception as e:  # noqa: BLE001 — catch-all so the loop keeps running
            log.exception("Unexpected error during poll: %s", e)
            time.sleep(interval)
            continue

        print(render_dashboard(curr, prev))

        events = detect_changes(prev, curr)
        for ev in events:
            log.info("EVENT: %s", ev)

        if events:
            title = "FRITZ!Box: " + (
                "offline" if not curr.connected() else "status change"
            )
            body = "\n".join(events)
            send_windows_toast(title, body)
            send_discord_webhook(events, curr)

        save_snapshot(curr)
        prev = curr

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log.info("Shutting down")
            break


if __name__ == "__main__":
    main()
