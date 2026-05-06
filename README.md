# FOD — FRITZ!Box Overengineered Dashboard

A thoroughly unnecessary set of Python tools for watching every byte, packet, photon, and existential anomaly passing through your AVM FRITZ!Box.

Four standalone scripts:

| Script | What it does |
|---|---|
| `fritz_dashboard.py` | Textual TUI, six tabs, Grafana-style dark theme. DSL/WAN/WLAN stats, sparklines, host table, raw TR-064 explorer, Wake-on-LAN, one-click DNS sync. |
| `fritz_dashboard_neo.py` | Same dashboard, Matrix-themed (boot rain, glitching labels, "DODGED" counter, green-on-black). |
| `fritz_watchtool.py` | Headless monitor. Polls every 5 minutes, alerts on connectivity / SNR / CRC changes via Windows toast and Discord webhook. |
| `dns_sync.py` | Library used by both dashboards. Pulls a BIND zone via AXFR, diffs against the FRITZ!Box host list, applies the diff as a single TSIG-signed DNS UPDATE. |

## Prerequisites

- **Python 3.12+**
- An AVM FRITZ!Box on your LAN
- In the FRITZ!Box web UI, enable:
  - *Home Network → Network → Network Settings → Allow access for applications*
  - *Home Network → Network → Network Settings → Transmit status information over UPnP*
- A FRITZ!Box account with TR-064 access (the box's admin user works)
- *(Optional, for the DNS sync feature)* a BIND server you control, with TSIG-signed `allow-transfer` and `allow-update` configured for the zone

## Get the code

### Option A — clone (recommended)

```powershell
git clone https://github.com/Oratorian/fritz-fod.git
cd fritz-fod
```

### Option B — fork first, then clone your fork

1. Visit https://github.com/Oratorian/fritz-fod and click **Fork** at the top right.
2. Clone *your* fork:
   ```powershell
   git clone https://github.com/<your-username>/fritz-fod.git
   cd fritz-fod
   ```
3. *(Optional)* track the original as `upstream` so you can pull updates:
   ```powershell
   git remote add upstream https://github.com/Oratorian/fritz-fod.git
   git fetch upstream
   ```

### Option C — download a ZIP

On the GitHub repo page, click **Code → Download ZIP**, then unzip it and `cd` into the resulting folder.

## Install

It's a good idea to use a virtual environment.

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell refuses to run the activation script, run this once as your user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`winotify` (used by the watchtool for desktop toasts) only installs on Windows — the requirement is gated by a `sys_platform` marker, so `pip install` works fine on macOS and Linux. The watchtool degrades gracefully and just skips toasts.

## Configure

All three runnable scripts read configuration from environment variables. Each script also auto-loads a `.env` file from the working directory at startup (via `python-dotenv`), so the easiest setup is:

```powershell
copy .env.example .env       # PowerShell
# or:  cp .env.example .env  # bash / zsh
```

…then edit `.env` and fill in your values. Real shell env vars override `.env`, so you can also just `export` / `$env:` them if you prefer.

### Required for everything

| Variable | Purpose |
|---|---|
| `FRITZ_ADDRESS` | IP or hostname of the FRITZ!Box (e.g. `192.168.178.1`) |
| `FRITZ_USERNAME` | TR-064 user (leave empty to use the box's default user) |
| `FRITZ_PASSWORD` | TR-064 password |

### Required for the "Sync DNS" button (dashboards only)

| Variable | Purpose |
|---|---|
| `DNS_ZONE` | The zone to manage, e.g. `home.lan` |
| `DNS_SERVER` | IP of your BIND server |
| `TSIG_KEY_PATH` | Path to a BIND-style TSIG key file (`key "name" { algorithm hmac-sha256; secret "..."; };`) |
| `DNS_PROTECTED_NAMES` | *(Optional)* comma-separated list of DNS labels the sync must never delete (e.g. `ns,central,qbit,sonarr,webmin`). Leave empty if you don't have any. |
| `DNS_OVERRIDES_FILE` | *(Optional)* path to a YAML file mapping MAC addresses to preferred DNS labels. See `dns_overrides.yaml.example`. |

### Optional for the watchtool

| Variable | Purpose |
|---|---|
| `DISCORD_WEBHOOK_URL` | Discord webhook for status-change alerts |
| `LOG_FILE` | Path for the rotating log (default `fritz_watchtool.log`) |
| `STATE_FILE` | Path for the JSON snapshot store (default `fritz_watchtool.state.json`) |

### Setting env vars from the shell instead

**PowerShell (current session):**
```powershell
$env:FRITZ_ADDRESS = "192.168.178.1"
$env:FRITZ_USERNAME = "fritzuser"
$env:FRITZ_PASSWORD = "supersecret"
```

**PowerShell (persisted across sessions):**
```powershell
[Environment]::SetEnvironmentVariable("FRITZ_ADDRESS", "192.168.178.1", "User")
```

**bash / zsh:**
```bash
export FRITZ_ADDRESS=192.168.178.1
export FRITZ_USERNAME=fritzuser
export FRITZ_PASSWORD=supersecret
```

### MAC → label overrides (YAML)

Some devices report unhelpful names to the FRITZ!Box (`ESP_4F3A21`, generic `iPhone`, etc.). To pin them to nicer DNS labels:

```powershell
copy dns_overrides.yaml.example dns_overrides.yaml
```

…edit the YAML, then point `DNS_OVERRIDES_FILE` at it in your `.env`:

```
DNS_OVERRIDES_FILE=./dns_overrides.yaml
```

The file is a flat mapping of (quoted) MAC address → label. The loader uppercases keys, so casing in the file doesn't matter.

## Run

With your venv active and env vars set:

```powershell
# Grafana-style dashboard
python fritz_dashboard.py

# Matrix-themed dashboard (same features, more drama)
python fritz_dashboard_neo.py

# Background monitor (long-running)
python fritz_watchtool.py
```

### Dashboard keybindings

| Key | Action |
|---|---|
| `1`–`6` | Switch tab (Overview, DSL, WLAN, Hosts, System, Explorer) |
| `r` | Refresh now |
| `w` | Wake selected host (Hosts tab) |
| `s` | Sync DNS now (Hosts tab) |
| `q` | Quit |

## Troubleshooting

- **"Could not connect" banner** — check `FRITZ_ADDRESS`, `FRITZ_USERNAME`, `FRITZ_PASSWORD`, and that the two FRITZ!Box settings above are enabled.
- **Watchtool reports "Probe returned no usable data"** — same check; the box is reachable but TR-064 is disabled.
- **TSIG key unavailable on sync** — the dashboard reads `TSIG_KEY_PATH` once at startup. Set the var, then restart.
- **DSL fields are empty** — expected on pure-fibre setups; the dashboards will simply leave DSL stats blank.
- **Throughput shows 0 right after launch** — the first poll has no previous sample to diff against. Wait one refresh cycle.

## Updating

```powershell
git pull                       # if you cloned directly
git pull upstream main         # if you forked and added the upstream remote
pip install -r requirements.txt
```

## License

MIT — see [LICENSE](LICENSE) for the full text.
