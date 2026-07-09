# cloudflarebot

A monitoring bot for the **Cloudflare Security Analytics** L7 DDoS chart of
`platform10.me`, wired to a **Lark/Feishu** group bot ("OSE Cloudflare Bot").

It:

1. Logs into the Cloudflare dashboard, opens **Security Analytics → L7 DDoS,
   last 6 hours**, and turns on **Live data**.
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

### Run as a background service (Linux server)

```ini
# /etc/systemd/system/cloudflarebot.service
[Unit]
Description=Cloudflare spike monitor (Lark bot)
After=network-online.target

[Service]
WorkingDirectory=/opt/cloudflarebot
ExecStart=/opt/cloudflarebot/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now cloudflarebot
journalctl -u cloudflarebot -f
```

---

## Usage

- **Automatic alerts:** whenever a new spike appears on the live 6h L7 DDoS chart,
  the group gets a message with the bucket time, request count, how many × the
  baseline it is, and Qwen's `ABNORMAL` / `NORMAL` verdict + explanation.
- **On-demand:** in the group, **@OSE Cloudflare Bot /mo** →
  👌 reaction → screenshot of the live chart + AI explanation → ✅ reaction.

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
