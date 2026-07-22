# cloudflarebot

A monitoring bot for the **Cloudflare Security Analytics** L7 DDoS chart of
`platform10.me`, wired to a **Lark/Feishu** group bot ("OSE Cloudflare Bot").

It:

1. Pulls the **L7 DDoS request timeseries** (last 6 hours, 5-minute buckets) from
   Cloudflare's official **GraphQL Analytics API** with a read-only token
   (`CF_MODE=api`, the default). No browser, no login, no bot challenge — works
   from any server. (A legacy `CF_MODE=browser` scraper still exists but only
   works from a residential IP; Cloudflare's bot challenge blocks datacenter IPs.)
2. Continuously watches the live request-count timeseries and detects **traffic
   spikes** with an adaptive threshold (rolling mean + N×std, robust to prior
   spikes). It never misses a new spike and never re-alerts an old one.
3. For every new spike, asks **Qwen (`qwen3.6:35b-a3b` via Ollama)** whether the
   spike looks abnormal, then posts an alert to the Lark group
   **"OSE BOT - Ops & Maintenance"**.
4. Runs a **persistent WebSocket subscription** (Lark long-connection mode). When
   you **@mention the bot with `/mo`**, it reacts **👌 (OK)** while working,
   posts a **screenshot of the live chart + an AI explanation**, then reacts
   **✅ (DONE)**.

---

## Architecture

| Thread | Responsibility |
|--------|----------------|
| main (Thread A) | Lark WebSocket subscription — receives `im.message.receive_v1`, dispatches `/mo` |
| cf-monitor (Thread B) | Owns Playwright: CF login, live-chart capture, spike detection, `/mo` screenshots |
| spike-alert (transient) | Qwen review + Lark alert per spike, so the monitor loop never stalls |

Playwright objects are thread-affine, so **all** browser work stays on Thread B;
the WebSocket handler only enqueues jobs to it.

Data is captured by **intercepting the dashboard's own GraphQL responses**
(`page.on("response")`), so authentication (session cookies + CSRF) is handled by
the browser — the bot never forges an API request. A defensive, shape-based
parser reads the `data.viewer.zones[].httpRequestsAdaptiveGroups[]` timeseries and
sums per-bucket counts, tolerant to Cloudflare renaming fields.

| File | Purpose |
|------|---------|
| `main.py` | Entry point; wires the threads together |
| `cloudflare_monitor.py` | Playwright login + live capture + spike loop + `/mo` |
| `spike_detector.py` | Adaptive detection + persistent de-dup (`state/spikes.json`) |
| `qwen_client.py` | Ollama `/api/chat` calls (spike review + `/mo` explanation) |
| `lark_bot.py` | Lark WS subscription + send text/image + reactions |
| `config.py` | Loads `.env` |

---

## Prerequisites

- **Python 3.10+**
- **Ollama** running with the model pulled: `ollama pull qwen3.6:35b-a3b`
  (the bot calls `http://localhost:11434` by default)
- A Cloudflare dashboard login with access to the `platform10.me` zone
- The Lark app credentials (already in `.env`)

## Setup

```bash
git clone https://github.com/mrcodestealer/cloudflarebot.git
cd cloudflarebot

python -m venv .venv && . .venv/bin/activate      # optional
pip install -r requirements.txt
python -m playwright install chromium              # one-time browser download

cp .env.example .env        # then fill in the values (see below)
```

> **`.env` is git-ignored** (it holds secrets). Create it on each machine (PC and
> server). If you accept the risk of storing secrets in your **private** repo,
> you may commit it instead — but that is not recommended.

### Create the Cloudflare API token (API mode — recommended)

1. Go to <https://dash.cloudflare.com/profile/api-tokens> → **Create Token** →
   **Create Custom Token**.
2. **Permissions:** `Zone` → `Analytics` → `Read`, and (to auto-resolve the zone
   id) `Zone` → `Zone` → `Read`.
3. **Zone Resources:** Include → Specific zone → `platform10.me`.
4. Create, copy the token, and put it in `.env` as `CF_API_TOKEN=…`. Optionally
   set `CF_ZONE_TAG=` to the zone id (from the zone's Overview page) to skip the
   lookup. This token is read-only analytics — it cannot change anything.

### ⚠️ Two required manual steps (bot can't do these itself)

1. **Add the bot to the group.** In Lark, open **"OSE BOT - Ops & Maintenance"**
   → group settings → **Add members / bots** → search **"OSE Cloudflare Bot"** →
   add it. Until it is a member you will see `230002 Bot/User can NOT be out of
   the chat` and no messages are sent. (A bot cannot add itself.)
2. **Lark Developer Console → your app:**
   - **Event Subscriptions**: set delivery to **"Use long connection to receive
     events"** (Subscription mode / persistent connection), and subscribe the
     event **`im.message.receive_v1`** ("Message received").
   - **Permissions/Scopes**: `im:message`, `im:message:send_as_bot`,
     `im:chat` (read chats), image/resource upload, and message reactions.
   - Publish a version so the scopes/events take effect.

---

## Configure `.env`

Key settings (see `.env.example` for the full list):

```ini
CF_EMAIL=...            # Cloudflare login
CF_PASSWORD='...'       # quote it if it contains special chars
CF_ANALYTICS_URL=https://dash.cloudflare.com/<acct>/<zone>/security/analytics?mitigation-service=l7ddos&time-window=360
CF_HEADLESS=true        # set false the first time if a CAPTCHA appears

LARK_APP_ID=cli_...
LARK_APP_SECRET=...
LARK_DOMAIN=https://open.larksuite.com   # this tenant is Lark (SG), not Feishu
LARK_CHAT_ID=oc_...
LARK_BOT_OPEN_ID=ou_...  # so only @mentions of THIS bot trigger commands

OLLAMA_HOST=http://localhost:11434
QWEN_MODEL=qwen3.6:35b-a3b

# Spike tuning (adaptive)
SPIKE_STD_MULTIPLIER=4.0     # spike = count > mean + 4*std of baseline
SPIKE_BASELINE_WINDOW=20     # buckets used for the rolling baseline
SPIKE_MIN_FLOOR=1000         # ignore "spikes" below this many requests
POLL_INTERVAL_SECONDS=30     # how often the captured series is evaluated
```

## Run

```bash
python main.py
```

- **First login:** if Cloudflare shows a CAPTCHA, run once with `CF_HEADLESS=false`,
  log in manually in the window — the session is saved to `.cf_profile/` and reused
  headlessly afterwards.
- Logs stream to stdout. State (already-alerted spikes) is in `state/spikes.json`.

### Installing the Chromium browser

```bash
python -m playwright install chromium          # downloads the browser binary
```

`--with-deps` only works on Debian/Ubuntu. On **RHEL / Rocky / Alma / Fedora**
install the OS libraries with dnf:

```bash
sudo dnf install -y nss nspr atk at-spi2-atk at-spi2-core cups-libs libdrm \
  libxkbcommon libXcomposite libXdamage libXext libXfixes libXrandr \
  mesa-libgbm libxcb pango cairo alsa-lib
```

### Run as a background service (systemd)

A ready template is in [`cloudflarebot.service`](cloudflarebot.service). The
easiest install auto-fills the correct paths — run from the repo directory:

```bash
DIR=$(pwd); PY=$(which python); GIT=$(command -v git)
sudo tee /etc/systemd/system/cloudflarebot.service >/dev/null <<EOF
[Unit]
Description=Cloudflare L7 DDoS spike monitor (Lark bot)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
WorkingDirectory=$DIR
ExecStartPre=-$GIT -C $DIR pull origin main
ExecStart=$PY $DIR/main.py
Environment=HOME=$HOME
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflarebot.service
```

The `ExecStartPre` line means **every `systemctl restart` also `git pull`s the
latest code** first (the `-` prefix keeps startup working even if offline).

Manage it:

```bash
systemctl restart cloudflarebot.service     # pulls latest + restarts
systemctl status  cloudflarebot.service
journalctl -u cloudflarebot.service -f       # live logs
```

**When dependencies change** (new package in `requirements.txt`), a plain
restart won't install them — use the one-shot deploy script instead:

```bash
bash deploy.sh      # git pull + pip install -r requirements.txt + restart
```

---

## Usage

- **Automatic alerts:** whenever a new spike appears on the live 6h L7 DDoS chart,
  the group gets a message with the bucket time, request count, how many × the
  baseline it is, and Qwen's `ABNORMAL` / `NORMAL` verdict + explanation.
- **On-demand:** in the group, **@OSE Cloudflare Bot /mo** →
  👌 reaction → screenshot of the live chart + AI explanation → ✅ reaction.
- **Test the alert format:** **@OSE Cloudflare Bot /testalert** → 👌 → posts a
  clearly-labelled **sample** spike alert (runs the real formatting + Qwen path
  on synthetic data, so it works even before any real spike) → ✅.
- **Deploy from chat (admin only):** in a **1:1 PM** with the bot, send **`/deploy`**
  (aliases: `/redeploy`, `/update`, `/pull`). The bot runs `git pull --ff-only`,
  reports the result, then restarts the service; when the new build is up it
  replies **✅ Back online — deployed `<commit>`**. Only user open_ids in
  `ADMIN_OPEN_IDS` are allowed — everyone else is refused. PM **`/whoami`** to get
  your own open_id for that list. The restart uses `systemd-run` so it survives
  the process being killed (override with `DEPLOY_RESTART_CMD`).

## How spike detection works

- Each poll, the captured `(bucket, count)` series is evaluated. A bucket is a
  spike if `count > mean + N*std` of the preceding baseline window **and** clears
  `SPIKE_MIN_FLOOR`.
- The baseline is **robust**: already-alerted spikes are excluded and the top ~10%
  of values are trimmed, so one attack can't blind the detector to the next.
- Alerted bucket timestamps are persisted to `state/spikes.json`, so restarts and
  repeated polls never re-alert an old spike; a genuinely new spike always fires.
- On first ever run, historical peaks already on the chart are recorded as "seen"
  (no spam) — except a spike in progress at that moment, which still alerts.

---

## Git workflow

```bash
git pull origin main      # on PC or server
# ...changes...
git add -A && git commit -m "..." && git push origin main
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `230002 Bot/User can NOT be out of the chat` | Bot not in the group — add it (manual step 1). |
| `1000040351 Incorrect domain name` on WS start | Wrong `LARK_DOMAIN`. This tenant is `https://open.larksuite.com`. |
| No `/mo` response | Event `im.message.receive_v1` not subscribed, or long-connection mode off, or bot not @-mentioned. |
| No data captured / empty chart | CF login failed (try `CF_HEADLESS=false` once), or the GraphQL shape changed — check debug logs. |
| Qwen errors in alert | Ollama not running or model not pulled; alerts still fire with a fallback note. |
