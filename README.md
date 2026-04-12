# AI Lab Cloud

Secure remote access to your home AI Lab from anywhere — no VPN, no port
forwarding. AI Lab Cloud is a self-hosted hub that authenticates users via
GitHub OAuth and proxies browser traffic to home AI Lab instances over
persistent WebSocket tunnels.

```
Browser (anywhere)
       │  HTTPS
       ▼
AI Lab Cloud Hub (your VPS / Linode)
       │  WebSocket tunnel (outbound from home)
       ▼
AI Lab (your home machine)
       │  LXD proxy
       ▼
Container: openclaw / nullclaw / picoclaw
```

---

## Table of Contents

- [How It Works](#how-it-works)
- [Prerequisites](#prerequisites)
- [1. Create a GitHub OAuth App](#1-create-a-github-oauth-app)
- [2. Provision a VPS](#2-provision-a-vps)
- [3. Configure DNS](#3-configure-dns)
- [4. Install and Configure Nginx with TLS](#4-install-and-configure-nginx-with-tls)
- [5. Install Redis](#5-install-redis)
- [6. Install the Hub Snap](#6-install-the-hub-snap)
- [7. Configure the Hub](#7-configure-the-hub)
- [8. Connect Your Home AI Lab](#8-connect-your-home-ai-lab)
- [9. Verify the Tunnel](#9-verify-the-tunnel)
- [Security Model](#security-model)
- [URL Routing](#url-routing)
- [Configuration Reference](#configuration-reference)
- [Development / Local Setup](#development--local-setup)
- [Troubleshooting](#troubleshooting)

---

## How It Works

1. **Home device** — your Ubuntu machine running AI Lab. On startup, it
   opens an outbound WebSocket connection to the hub and holds it open.
   No inbound firewall rules are needed on your home router.

2. **Hub** — a FastAPI service running on a VPS. It authenticates browsers
   via GitHub OAuth, looks up the tunnel registered by the home device, and
   pipes HTTP/WebSocket traffic back through it.

3. **Browser** — visits `https://mydevice.cloud.example.com`, logs in with
   GitHub, and sees the AI Lab web interface exactly as if it were local.

The tunnel token prevents any device from impersonating another user's
GitHub account. Your GitHub credentials are never sent to the home device.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Ubuntu 22.04+ VPS | 1 vCPU / 1 GB RAM is sufficient for the hub alone |
| A domain you control | Used for the hub and wildcard subdomains |
| GitHub account | For the OAuth app |
| Home machine running AI Lab | See [AI Lab README](https://github.com/lemonade-sdk/ailab) |

---

## 1. Create a GitHub OAuth App

1. Go to **GitHub → Settings → Developer settings → OAuth Apps → New OAuth App**.
2. Fill in:
   - **Application name**: `AI Lab Cloud` (or whatever you like)
   - **Homepage URL**: `https://cloud.example.com` (your hub domain)
   - **Authorization callback URL**: `https://cloud.example.com/auth/callback`
3. Click **Register application**.
4. On the next page, note the **Client ID**.
5. Click **Generate a new client secret** and note the **Client Secret**.

Keep both values — you will need them in [step 7](#7-configure-the-hub).

---

## 2. Provision a VPS

Any VPS provider works. The hub is lightweight — a 1 GB RAM instance is
fine unless you expect many simultaneous tunnels.

Example with Ubuntu 24.04 on Linode / DigitalOcean / Hetzner:

```bash
# On the VPS, after first login:
apt update && apt upgrade -y
apt install -y snapd nginx certbot python3-certbot-nginx python3-certbot-dns-cloudflare
```

> **Tip**: record the VPS's public IPv4 address — you need it for DNS.

---

## 3. Configure DNS

The hub uses **wildcard subdomains** to route traffic to specific ports on
your home device. You need two DNS records:

| Type | Name | Value |
|---|---|---|
| `A` | `cloud.example.com` | `<VPS IPv4>` |
| `A` | `*.cloud.example.com` | `<VPS IPv4>` |

Replace `cloud.example.com` with whatever domain or subdomain you choose.

**How subdomains map to ports:**

| Browser visits | Proxied to (on home device) |
|---|---|
| `mydevice.cloud.example.com` | AI Lab Web UI — port 11500 |
| `mydevice-18789.cloud.example.com` | OpenClaw gateway — port 18789 |
| `mydevice-3000.cloud.example.com` | Nullclaw — port 3000 |
| `mydevice-18800.cloud.example.com` | PicoClaw — port 18800 |

Where `mydevice` is the device ID you set in [step 8](#8-connect-your-home-ai-lab).

**DNS propagation**: use a TTL of 300 seconds (5 minutes) or lower when
initially setting up so you can iterate quickly. Increase to 3600 once
everything is working.

**Verify records:**

```bash
dig cloud.example.com A +short
dig anything.cloud.example.com A +short   # both should return your VPS IP
```

---

## 4. Install and Configure Nginx with TLS

A wildcard TLS certificate requires a **DNS-01 ACME challenge** (HTTP-01
cannot prove ownership of `*.cloud.example.com`). This example uses
Certbot with the Cloudflare DNS plugin; adapt for your DNS provider.

### 4a. Obtain a wildcard certificate

Install the plugin for your DNS provider:

```bash
# Cloudflare example:
apt install -y python3-certbot-dns-cloudflare

# Create credentials file:
cat > /etc/certbot/cloudflare.ini <<'EOF'
dns_cloudflare_api_token = YOUR_CLOUDFLARE_API_TOKEN
EOF
chmod 600 /etc/certbot/cloudflare.ini
```

Issue the certificate:

```bash
certbot certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials /etc/certbot/cloudflare.ini \
  -d cloud.example.com \
  -d '*.cloud.example.com' \
  --email you@example.com \
  --agree-tos \
  --non-interactive
```

Certificates are written to `/etc/letsencrypt/live/cloud.example.com/`.

**Other DNS providers**: replace `--dns-cloudflare` with the relevant
Certbot plugin (e.g. `--dns-route53`, `--dns-digitalocean`). See the
[Certbot plugin list](https://certbot.eff.org/docs/using.html#dns-plugins).

### 4b. Write the Nginx configuration

Create `/etc/nginx/sites-available/ailab-cloud`:

```nginx
# Redirect bare HTTP to HTTPS
server {
    listen 80;
    server_name cloud.example.com *.cloud.example.com;
    return 301 https://$host$request_uri;
}

# Hub and all device subdomains
server {
    listen 443 ssl;
    server_name cloud.example.com *.cloud.example.com;

    ssl_certificate     /etc/letsencrypt/live/cloud.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/cloud.example.com/privkey.pem;

    # Recommended TLS settings
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

    # Increase timeouts for long-lived tunnel WebSockets
    proxy_read_timeout  3600s;
    proxy_send_timeout  3600s;

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;

        # Required for WebSocket upgrade (tunnels and shell terminal)
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Pass through the real host so the hub knows which device subdomain
        # was requested
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable it:

```bash
ln -s /etc/nginx/sites-available/ailab-cloud /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

### 4c. Set up certificate auto-renewal

Certbot installs a systemd timer automatically. Verify it:

```bash
systemctl status certbot.timer
# Force a dry-run to confirm renewal works:
certbot renew --dry-run
```

---

## 5. Install Redis

The hub uses Redis to persist tunnel registrations and tunnel tokens across
restarts.

```bash
apt install -y redis-server

# Enable and start
systemctl enable --now redis-server

# Verify
redis-cli ping   # should print: PONG
```

Redis binds to `127.0.0.1` by default — this is correct; it should not be
exposed publicly.

---

## 6. Install the Hub Snap

```bash
sudo snap install ailab-cloud
```

> Until the snap is published to the store, build it locally:
>
> ```bash
> git clone https://github.com/lemonade-sdk/ailab-cloud
> cd ailab-cloud
> snapcraft
> sudo snap install --dangerous ailab-cloud_*.snap
> ```

---

## 7. Configure the Hub

Set the required snap settings. The hub will not start until all required
settings are present.

```bash
# Required
sudo snap set ailab-cloud domain=cloud.example.com
sudo snap set ailab-cloud github.client-id=<GitHub OAuth App client ID>
sudo snap set ailab-cloud github.client-secret=<GitHub OAuth App client secret>

# Optional — defaults shown
sudo snap set ailab-cloud redis.url=redis://localhost:6379
sudo snap set ailab-cloud web.host=127.0.0.1   # nginx proxies from 443
sudo snap set ailab-cloud web.port=8080
```

The `session.secret` (used to sign login cookies) is generated automatically
on first `snap set` and does not need to be set manually. If you need to
rotate it:

```bash
sudo snap set ailab-cloud session.secret=$(openssl rand -hex 32)
```

Start the service:

```bash
sudo snap start ailab-cloud.hub
sudo snap logs ailab-cloud.hub -f
```

Verify it is reachable:

```bash
curl -s https://cloud.example.com/health
# → {"status":"ok"}
```

---

## 8. Connect Your Home AI Lab

### 8a. Get your tunnel token

Open `https://cloud.example.com` in a browser and log in with GitHub.
Then visit:

```
https://cloud.example.com/auth/tunnel-token
```

You will receive a JSON response containing your token:

```json
{"github_user": "yourname", "token": "abc123..."}
```

Copy the token value.

### 8b. Configure AI Lab on your home machine

```bash
sudo snap set ailab cloud.enabled=true
sudo snap set ailab cloud.host=https://cloud.example.com
sudo snap set ailab cloud.user=yourname
sudo snap set ailab cloud.token=abc123...
sudo snap set ailab cloud.device-id=myhome   # any short identifier you choose
```

The device ID becomes part of your URLs:
`https://myhome.cloud.example.com` → AI Lab Web UI.

Restart the AI Lab web daemon to pick up the new settings:

```bash
sudo snap restart ailab.web
sudo snap logs ailab.web -f
```

You should see a log line confirming the tunnel is connected:

```
INFO  ailab.cloud  Tunnel registered as 'myhome' for user 'yourname'
```

### 8c. Open the remote UI

Visit `https://myhome.cloud.example.com` in any browser. Log in with
GitHub (same account as `cloud.user`). The AI Lab dashboard will load,
proxied through the tunnel.

---

## 9. Verify the Tunnel

```bash
# On the VPS — check the hub log
sudo snap logs ailab-cloud.hub | grep myhome

# On the home machine — check the tunnel client
sudo snap logs ailab.web | grep -i tunnel

# From any browser — health check
curl -s https://cloud.example.com/health
```

If the "Open OpenClaw" button appears and works, the full round-trip
(browser → hub → tunnel → container → hub → browser) is working.

---

## Security Model

**Authentication layers:**

1. **GitHub OAuth** — the browser user must authenticate with GitHub. The
   hub only routes traffic if the authenticated GitHub login matches the
   `github_user` the home device registered with.

2. **Tunnel token** — a random 32-byte secret stored in Redis, issued per
   GitHub user. The home device must present this token when opening the
   tunnel. An unauthenticated device cannot impersonate any GitHub user.

3. **TLS** — all traffic between the browser, hub, and (if you use HTTPS for
   the hub connection URL) the tunnel WebSocket is encrypted in transit.

**What the hub can see:**

The hub proxies HTTP/WebSocket frames between the browser and the home
device. Request bodies (including LLM prompts sent to openclaw) pass
through the hub's memory briefly during proxying. If you are not comfortable
with this, run the hub on hardware you control, or use AI Lab locally only.

**Rotating the tunnel token:**

```bash
# In a browser, while logged in:
curl -X POST https://cloud.example.com/auth/tunnel-token/regenerate \
  -H "Cookie: session=<your_session_cookie>"

# Then update the home device:
sudo snap set ailab cloud.token=<new_token>
sudo snap restart ailab.web
```

---

## URL Routing

The hub supports two routing modes simultaneously.

### Host-header routing (recommended, requires wildcard DNS)

```
https://{device_id}.{domain}/           → port 11500 (AI Lab Web UI)
https://{device_id}-{port}.{domain}/    → specific port
```

Examples:
```
https://myhome.cloud.example.com/           → AI Lab
https://myhome-18789.cloud.example.com/     → OpenClaw
https://myhome-3000.cloud.example.com/      → Nullclaw
```

### Path-based routing (always available, no DNS changes needed)

```
https://{domain}/d/{device_id}/          → port 11500
https://{domain}/d/{device_id}:{port}/   → specific port
```

Examples:
```
https://cloud.example.com/d/myhome/
https://cloud.example.com/d/myhome:18789/
```

Path-based routing is useful for testing or for DNS setups that do not
support wildcard records.

---

## Configuration Reference

All hub settings are snap settings read at service start by the wrapper
script. They map to `AILAB_CLOUD_*` environment variables.

| Snap setting | Environment variable | Required | Default | Description |
|---|---|---|---|---|
| `domain` | `AILAB_CLOUD_DOMAIN` | Yes | — | Base domain, e.g. `cloud.example.com` |
| `github.client-id` | `AILAB_CLOUD_GITHUB_CLIENT_ID` | Yes | — | GitHub OAuth App client ID |
| `github.client-secret` | `AILAB_CLOUD_GITHUB_CLIENT_SECRET` | Yes | — | GitHub OAuth App client secret |
| `session.secret` | `AILAB_CLOUD_SESSION_SECRET` | Yes | auto-generated | Secret for signing session cookies |
| `redis.url` | `AILAB_CLOUD_REDIS_URL` | No | `redis://localhost:6379` | Redis connection URL |
| `web.host` | `AILAB_CLOUD_HOST` | No | `0.0.0.0` | Bind address |
| `web.port` | `AILAB_CLOUD_PORT` | No | `8080` | Bind port |

**Home device AI Lab settings** (set with `snap set ailab ...`):

| Setting | Description |
|---|---|
| `cloud.enabled` | Set to `true` to enable the tunnel client |
| `cloud.host` | Hub URL, e.g. `https://cloud.example.com` |
| `cloud.user` | Your GitHub username |
| `cloud.token` | Tunnel token from `/auth/tunnel-token` |
| `cloud.device-id` | Short identifier for this machine, e.g. `myhome` |

---

## Development / Local Setup

To run the hub without a VPS or domain during development:

```bash
git clone https://github.com/lemonade-sdk/ailab-cloud
cd ailab-cloud

# Install dependencies
pip install -e ".[dev]"   # or: pip install fastapi uvicorn httpx redis itsdangerous

# Start a local Redis
docker run -d -p 6379:6379 redis:7-alpine
# or: snap install redis && snap start redis

# Set required environment variables
export AILAB_CLOUD_DOMAIN=localhost:8080
export AILAB_CLOUD_GITHUB_CLIENT_ID=your_client_id
export AILAB_CLOUD_GITHUB_CLIENT_SECRET=your_client_secret
export AILAB_CLOUD_SESSION_SECRET=$(openssl rand -hex 32)
export AILAB_CLOUD_REDIS_URL=redis://localhost:6379

# Run
uvicorn ailab_cloud.main:app --reload --host 127.0.0.1 --port 8080
```

For local development, create a GitHub OAuth App with callback URL
`http://localhost:8080/auth/callback`.

Path-based routing (`/d/{device_id}/...`) works without DNS or TLS in
development. The home device connects with:

```bash
sudo snap set ailab cloud.host=http://localhost:8080
```

---

## Troubleshooting

**Hub won't start — missing environment variables**

```bash
sudo snap logs ailab-cloud.hub | tail -20
```

Look for `Missing required environment variables`. Set all required snap
settings (see [Configuration Reference](#configuration-reference)).

**`curl https://cloud.example.com/health` returns a certificate error**

The wildcard certificate may not cover the bare domain. Make sure you issued
the cert for both `cloud.example.com` and `*.cloud.example.com` (both `-d`
flags in the certbot command).

**Tunnel connects but browser shows 403 Access Denied**

The GitHub account you logged in with does not match the `cloud.user` set on
the home device. Both must be the same GitHub login.

**"Device is not connected" in the browser**

The tunnel client on the home device is not running or was disconnected.
Check:

```bash
sudo snap logs ailab.web -f
```

The client reconnects automatically with exponential back-off. You can force
a restart:

```bash
sudo snap restart ailab.web
```

**WebSocket shell terminal does not work through the tunnel**

The Nginx `proxy_read_timeout` may be too short. The `3600s` value in the
config above supports idle shells for up to one hour. Increase if needed.

**Subdomain routing not working (host-header mode)**

Verify your wildcard DNS record resolves:

```bash
dig anything.cloud.example.com A +short
```

If it does not return your VPS IP, the `*.cloud.example.com` A record is
missing or not yet propagated. Path-based routing (`/d/...`) will work in
the meantime.

**Redis connection refused**

```bash
systemctl status redis-server
redis-cli ping
```

If Redis is not running: `systemctl start redis-server`. If it is running
but the hub cannot connect, check that `redis.url` matches the actual Redis
socket/port.
