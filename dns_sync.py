"""
DNS sync — keeps a sub-zone in BIND in sync with the Fritzbox host table.

Pulls authoritative truth from BIND (so script restarts are stateless and
manual edits don't get clobbered), diffs it against what the Fritzbox is
currently reporting, and applies only the actual changes via dnspython's
native dynamic-update support — no nsupdate binary required, so this works
on Windows, macOS, Linux, anywhere Python runs.

Designed to be called from FritzDashboard's manual-sync handler with the
host list it's already gathering — no extra TR-064 round-trips.

Quick mental model
------------------
desired = build_desired_state(hosts)        # Fritzbox  -> {label: ip}
current = pull_current_zone(zone, server)   # BIND      -> {label: ip}
diff    = compute_diff(current, desired)    # what changed
apply_diff(diff, ...)                        # dnspython UPDATE, atomic

Authentication is via TSIG. Pass the key as a (name, secret, algorithm)
tuple — typically loaded once at startup from the BIND-style key file.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import dns.exception
import dns.message
import dns.name
import dns.query
import dns.rcode
import dns.rdatatype
import dns.tsig
import dns.tsigkeyring
import dns.update
import dns.zone

log = logging.getLogger("dns_sync")


# ---------------------------------------------------------------------------
# Hostname sanitization
# ---------------------------------------------------------------------------

# RFC 1035 labels: letters, digits, hyphens. Everything else becomes a hyphen,
# then we collapse runs and strip leading/trailing hyphens. Lowercase because
# DNS is case-insensitive on the wire and lowercase is the convention.
_BAD_CHARS = re.compile(r"[^a-z0-9-]+")
_DASH_RUN = re.compile(r"-+")


def sanitize_label(raw: str) -> str:
    """Convert a Fritzbox-shown name into a valid DNS label.

    Returns the empty string if nothing usable remains — caller should skip
    those entries.
    """
    if not raw:
        return ""
    label = raw.strip().lower()
    label = _BAD_CHARS.sub("-", label)
    label = _DASH_RUN.sub("-", label).strip("-")
    # RFC 1035: max 63 octets per label.
    return label[:63]


# ---------------------------------------------------------------------------
# Loading the MAC -> label override map from a YAML file
# ---------------------------------------------------------------------------


def load_overrides(path: str | None) -> dict[str, str]:
    """Load a MAC-address -> DNS-label mapping from a YAML file.

    YAML format is a flat top-level mapping, e.g.

        "AA:BB:CC:DD:EE:FF": kitchen-sensor
        "11:22:33:44:55:66": hallway-pi

    Returns {} if `path` is falsy or the file is missing — overrides are
    optional, the dashboard works fine without them. Keys are uppercased
    so they match what `build_desired` sees from the FRITZ!Box.
    """
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        log.warning("dns overrides file not found: %s", path)
        return {}
    import yaml  # deferred — only needed when this feature is used

    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: expected a top-level mapping, got {type(data).__name__}"
        )
    return {str(k).upper(): str(v) for k, v in data.items()}


# ---------------------------------------------------------------------------
# Building desired state from the Fritzbox host list
# ---------------------------------------------------------------------------


def build_desired(
    hosts: list[dict],
    *,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Turn the TR-064 host list into a {label: ipv4} mapping.

    Skips inactive hosts, hosts with no IP, and hosts whose name sanitizes
    away to nothing. `overrides` maps MAC (uppercase, colon-separated) to
    a preferred label, useful for devices whose Fritzbox name is junk like
    "ESP_4F3A21" but you want them as "kitchen-sensor".

    On collisions (two devices sanitize to the same label), the first one
    wins and the second is logged. Real-world this happens when two iPhones
    both call themselves "iPhone" — fix it in the Fritzbox UI or via overrides.
    """
    overrides = overrides or {}
    desired: dict[str, str] = {}
    seen_macs: dict[str, str] = {}

    for h in hosts:
        if not h.get("NewActive"):
            continue
        ip = h.get("NewIPAddress")
        if not ip:
            continue

        mac = (h.get("NewMACAddress") or "").upper()
        raw_name = h.get("NewHostName") or ""

        label = overrides.get(mac) or sanitize_label(raw_name)
        if not label:
            log.debug("skipping host with no usable name: mac=%s ip=%s", mac, ip)
            continue

        if label in desired:
            log.warning(
                "label collision: %r already used by mac=%s, skipping mac=%s ip=%s",
                label,
                seen_macs.get(label),
                mac,
                ip,
            )
            continue

        desired[label] = ip
        seen_macs[label] = mac

    return desired


# ---------------------------------------------------------------------------
# Pulling current state from BIND via AXFR
# ---------------------------------------------------------------------------


def pull_current(
    zone: str,
    server: str = "127.0.0.1",
    timeout: float = 5.0,
    keyring=None,
    keyalgorithm=dns.tsig.HMAC_SHA256,
) -> dict[str, str]:
    """Read all A records from a zone via AXFR.

    Returns {label: ip}. Empty dict if the zone is empty or transfer fails.
    Records at the zone apex (SOA, NS) are skipped — we only manage
    immediate child labels.

    If keyring is provided, the AXFR is TSIG-signed (required if BIND has
    `allow-transfer { key foo; };`).
    """
    try:
        z = dns.zone.from_xfr(
            dns.query.xfr(
                server,
                zone,
                timeout=timeout,
                keyring=keyring,
                keyalgorithm=keyalgorithm,
            )
        )
    except (dns.exception.DNSException, OSError) as e:
        log.error("AXFR of %s from %s failed: %s", zone, server, e)
        return {}

    out: dict[str, str] = {}
    for name, _ttl, rdata in z.iterate_rdatas("A"):
        # Skip the zone apex; we only manage child labels.
        label = name.to_text().rstrip(".")
        if label == "@" or label == "":
            continue
        # Take the first A if there are multiple (unusual for our case).
        if label not in out:
            out[label] = rdata.address
    return out


# ---------------------------------------------------------------------------
# TSIG key handling
# ---------------------------------------------------------------------------

# Match BIND-style key files:
#   key "fritzdash-key" {
#       algorithm hmac-sha256;
#       secret "base64stuff==";
#   };
_KEY_RE = re.compile(
    r'key\s+"?(?P<name>[^"\s{]+)"?\s*\{\s*'
    r"algorithm\s+(?P<algo>[a-z0-9-]+)\s*;\s*"
    r'secret\s+"(?P<secret>[^"]+)"\s*;\s*'
    r"\}\s*;",
    re.IGNORECASE | re.DOTALL,
)

_ALGO_MAP = {
    "hmac-md5": dns.tsig.HMAC_MD5,
    "hmac-sha1": dns.tsig.HMAC_SHA1,
    "hmac-sha224": dns.tsig.HMAC_SHA224,
    "hmac-sha256": dns.tsig.HMAC_SHA256,
    "hmac-sha384": dns.tsig.HMAC_SHA384,
    "hmac-sha512": dns.tsig.HMAC_SHA512,
}


def load_tsig_key(path: str) -> tuple[dict, str]:
    """Parse a BIND-style key file and return (keyring, algorithm).

    Both values are ready to pass into dnspython calls.
    """
    with open(path) as f:
        text = f.read()
    m = _KEY_RE.search(text)
    if not m:
        raise ValueError(f"could not parse TSIG key from {path}")
    name = m.group("name")
    algo = _ALGO_MAP.get(m.group("algo").lower())
    if algo is None:
        raise ValueError(f"unsupported TSIG algorithm: {m.group('algo')}")
    secret = m.group("secret")
    keyring = dns.tsigkeyring.from_text({name: secret})
    return keyring, algo


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


@dataclass
class Diff:
    add: dict[str, str] = field(default_factory=dict)  # name -> ip
    update: dict[str, str] = field(default_factory=dict)  # name -> new_ip
    delete: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.add or self.update or self.delete)

    def summary(self) -> str:
        return f"+{len(self.add)} ~{len(self.update)} -{len(self.delete)}"


def compute_diff(
    current: dict[str, str],
    desired: dict[str, str],
    protected: frozenset[str] = frozenset(),
) -> Diff:
    """Compute the add/update/delete diff between current and desired states.

    Names in `protected` are never deleted, even if they're absent from
    `desired`. Used to keep hand-curated records (NS targets, service
    aliases, static infra) safe from the destructive sync. Note: protected
    names CAN still be updated if they appear in desired with a different
    IP — the protection is specifically against deletion.
    """
    d = Diff()
    for name, ip in desired.items():
        if name not in current:
            d.add[name] = ip
        elif current[name] != ip:
            d.update[name] = ip
    for name in current:
        if name not in desired and name not in protected:
            d.delete.append(name)
    return d


# ---------------------------------------------------------------------------
# Applying via nsupdate
# ---------------------------------------------------------------------------


def apply_diff(
    diff: Diff,
    *,
    zone: str,
    server: str = "127.0.0.1",
    port: int = 53,
    ttl: int = 60,
    keyring=None,
    keyalgorithm=dns.tsig.HMAC_SHA256,
    timeout: float = 10.0,
    dry_run: bool = False,
) -> None:
    """Apply a diff atomically as a single DNS UPDATE message.

    All changes go in one transaction (RFC 2136), so either everything
    lands or nothing does. Updates are expressed as delete-then-add per
    convention. Uses dnspython natively — no nsupdate binary required.
    """
    if diff.empty:
        return

    upd = dns.update.Update(zone, keyring=keyring, keyalgorithm=keyalgorithm)

    for name in diff.delete:
        upd.delete(name, "A")
    for name, ip in diff.update.items():
        upd.delete(name, "A")
        upd.add(name, ttl, "A", ip)
    for name, ip in diff.add.items():
        upd.add(name, ttl, "A", ip)

    if dry_run:
        # to_text() includes both the question and update sections
        log.info("DNS UPDATE dry-run:\n%s", upd.to_text())
        return

    # TCP is more reliable than UDP for updates of any meaningful size,
    # and BIND accepts updates over either.
    response = dns.query.tcp(upd, server, port=port, timeout=timeout)
    rcode = response.rcode()
    if rcode != dns.rcode.NOERROR:
        raise RuntimeError(f"DNS UPDATE failed: rcode={dns.rcode.to_text(rcode)}")
    log.info("applied diff: %s", diff.summary())


# ---------------------------------------------------------------------------
# Top-level convenience: do one full sync pass
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    diff: Diff
    desired_count: int
    current_count: int
    error: str | None = None


def sync_once(
    hosts: list[dict],
    *,
    zone: str,
    server: str = "127.0.0.1",
    ttl: int = 60,
    overrides: dict[str, str] | None = None,
    protected: frozenset[str] = frozenset(),
    keyring=None,
    keyalgorithm=dns.tsig.HMAC_SHA256,
    dry_run: bool = False,
) -> SyncResult:
    """One end-to-end pass: build desired, pull current, diff, apply.

    Returns a SyncResult so the dashboard can display "+2 ~1 -0" or similar.
    Exceptions are caught and stuffed into result.error rather than raised,
    because the dashboard shouldn't crash because BIND blinked.

    `protected` is a set of bare labels (no zone suffix) that the script
    must never delete — typically NS targets, infra aliases, and any
    hand-curated records you don't want bulldozed.

    `keyring` and `keyalgorithm` come from load_tsig_key(); pass them in
    if BIND requires TSIG for transfers and updates (recommended).
    """
    try:
        desired = build_desired(hosts, overrides=overrides)
        current = pull_current(
            zone,
            server=server,
            keyring=keyring,
            keyalgorithm=keyalgorithm,
        )
        diff = compute_diff(current, desired, protected=protected)
        if not diff.empty:
            apply_diff(
                diff,
                zone=zone,
                server=server,
                ttl=ttl,
                keyring=keyring,
                keyalgorithm=keyalgorithm,
                dry_run=dry_run,
            )
        return SyncResult(
            diff=diff, desired_count=len(desired), current_count=len(current)
        )
    except Exception as e:
        log.exception("sync_once failed")
        return SyncResult(
            diff=Diff(),
            desired_count=0,
            current_count=0,
            error=str(e),
        )
