# AI Lab Cloud

Secure remote access to your home AI Lab from anywhere — no VPN, no port
forwarding. AI Lab Cloud is a self-hosted hub that authenticates users via
GitHub OAuth and proxies browser traffic to home AI Lab instances over
persistent WebSocket tunnels.

The snap is **fully self-contained**: Redis, Caddy (TLS reverse proxy with
automatic certificate management), and the hub API server are all bundled.
Point DNS at the server, set four settings, and you're live.

```
Browser (anywhere)
       │  HTTPS / wss://
       ▼
AI Lab Cloud Hub  ──  Caddy (TLS, bundled)
                  ──  Hub API (FastAPI, bundled)
                  ──  Redis (bundled)
       │
  WebSocket tunnel (outbound from home, no inbound firewall rules needed)
       │
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
- [4. Install the Snap](#4-install-the-snap)
- [5. Configure and Start](#5-configure-and-start)
- [6. Connect Your Home AI Lab](#6-connect-your-home-ai-lab)
- [7. Verify Everything Works](#7-verify-everything-works)
- [Security Model](#security-model)
- [URL Routing](#url-routing)
- [Configuration Reference](#configuration-reference)
- [Supported DNS Providers](#supported-dns-providers)
- [Development / Local Setup](#development--local-setup)
- [Troubleshooting](#troubleshooting)

---

## How It Works

1. **Home device** — your Ubuntu machine running AI Lab. On startup it opens
   an outbound WebSocket connection to the hub and holds it open indefinitely.
   No inbound firewall rules or port forwarding are needed on your home router.

2. **Hub** — a FastAPI service running on a VPS, fronted by Caddy. It
   authenticates browsers via GitHub OAuth, looks up the tunnel registered by
   the home device, and pipes HTTP/WebSocket traffic back through it.

3. **Browser** — visits `https://mydevice.cloud.example.com`, logs in with
   GitHub, and sees the AI Lab dashboard proxied through the tunnel.

The tunnel token prevents any device from impersonating another user's GitHub
account. Your GitHub credentials are never sent to the home device.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Ubuntu 22.04+ VPS | 1 vCPU / 1 GB RAM is sufficient |
| A domain you control | For the hub and wildcard subdomains |
| GitHub account | For the OAuth app |
| API token for your DNS provider | For wildcard TLS (see [Supported DNS Providers](#supported-dns-providers)) |
| Home machine running AI Lab | See [AI Lab README](https://github.com/lemonade-sdk/ailab) |

---

## 1. Create a GitHub OAuth App

1. Go to **GitHub → Settings → Developer settings → OAuth Apps → New OAuth App**.
2. Fill in:
   - **Application name**: `AI Lab Cloud`
   - **Homepage URL**: `https://cloud.example.com`
   - **Authorization callback URL**: `https://cloud.example.com/auth/callback`
3. Click **Register application**, then note the **Client ID**.
4. Click **Generate a new client secret** and save the **Client Secret**.

---

## 2. Provision a VPS

Any provider works (Linode, DigitalOcean, Hetzner, etc.). A 1 GB RAM instance
is sufficient unless you expect many simultaneous tunnels.

```bash
# Ubuntu 24.04 — first login
apt update && apt upgrade -y
sudo snap install snapd
```

Note the VPS's public IPv4 address — you need it in the next step.

---

## 3. Configure DNS

The hub uses **wildcard subdomains** to route traffic to specific ports on
your home devices. Two records are required:

| Type | Name | Value |
|---|---|---|
| `A` | `cloud.example.com` | `<VPS IPv4>` |
| `A` | `*.cloud.example.com` | `<VPS IPv4>` |

Replace `cloud.example.com` with your chosen domain or subdomain.

**How subdomains map to ports on the home device:**

| Browser visits | Proxied to |
|---|---|
| `mydevice.cloud.example.com` | AI Lab Web UI — port 11500 |
| `mydevice-18789.cloud.example.com` | OpenClaw — port 18789 |
| `mydevice-3000.cloud.example.com` | Nullclaw — port 3000 |
| `mydevice-18800.cloud.example.com` | PicoClaw — port 18800 |

`mydevice` is the device ID you set on your home machine in
[step 6](#6-connect-your-home-ai-lab). Device IDs must use lowercase letters,
digits, and hyphens, and they are globally unique on the hub.

**DNS propagation tips:**

- Set TTL to 300 s (5 min) during initial setup; raise it to 3600 s once
  everything is working.
- Verify both records resolve to your VPS IP before continuing:

```bash
dig cloud.example.com A +short
dig anything.cloud.example.com A +short   # wildcard — should return same IP
```

---

## 4. Install the Snap

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

The snap contains three services that start automatically:

| Service | Role |
|---|---|
| `ailab-cloud.redis` | Key-value store for tunnel registrations and tokens |
| `ailab-cloud.hub` | FastAPI API server (starts after Redis) |
| `ailab-cloud.caddy` | TLS reverse proxy; issues and renews certificates automatically |

---

## 5. Configure and Start

Set the four required settings, plus DNS credentials for wildcard TLS:

```bash
# Required
sudo snap set ailab-cloud domain=cloud.example.com
sudo snap set ailab-cloud github.client-id=<client ID from step 1>
sudo snap set ailab-cloud github.client-secret=<client secret from step 1>

# Wildcard TLS — set dns.provider and dns.token for your DNS provider.
# See "Supported DNS Providers" below for per-provider instructions.
sudo snap set ailab-cloud dns.provider=cloudflare
sudo snap set ailab-cloud dns.token=<Cloudflare API token>
```

The `session.secret` (used to sign login cookies) is generated automatically
on first `snap set` — you do not need to set it manually.

Start all three services:

```bash
sudo snap start ailab-cloud
```

Watch the Caddy log to confirm the certificate was issued:

```bash
sudo snap logs ailab-cloud.caddy -f
```

You should see lines like:

```
... obtained certificate for cloud.example.com
... obtained certificate for *.cloud.example.com
```

Verify the hub is reachable:

```bash
curl -s https://cloud.example.com/health
# → {"status":"ok"}
```

**Changing domain or DNS settings after first start:**

```bash
sudo snap set ailab-cloud dns.provider=hetzner
sudo snap set ailab-cloud dns.token=<new token>
# Caddy restarts automatically to pick up the new settings.
```

The configure hook restarts Caddy automatically whenever you run
`snap set ailab-cloud ...` if the service is already running.

---

## 6. Connect Your Home AI Lab

### 6a. Get your tunnel token

Open `https://cloud.example.com` in a browser and log in with GitHub.
Then visit:

```
https://cloud.example.com/auth/tunnel-token
```

Copy the `token` value from the JSON response.

### 6b. Configure AI Lab on your home machine

```bash
sudo snap set ailab cloud.enabled=true
sudo snap set ailab cloud.host=https://cloud.example.com
sudo snap set ailab cloud.user=yourname          # your GitHub username
sudo snap set ailab cloud.token=<token>
sudo snap set ailab cloud.device-id=myhome       # short identifier, becomes part of URLs
sudo snap restart ailab.web
```

Check the home device log for confirmation:

```bash
sudo snap logs ailab.web -f
# Look for: INFO  ailab.cloud  Tunnel registered as 'myhome' for user 'yourname'
```

### 6c. Open the remote UI

Visit `https://myhome.cloud.example.com` in any browser and log in with the
same GitHub account you used in step 6b. The full AI Lab dashboard loads,
including the interactive terminal and all "Open …" buttons for installed tools.

---

## 7. Verify Everything Works

```bash
# Hub health
curl -s https://cloud.example.com/health

# Services on the VPS
sudo snap services ailab-cloud

# Detailed logs
sudo snap logs ailab-cloud.redis  -f
sudo snap logs ailab-cloud.hub    -f
sudo snap logs ailab-cloud.caddy  -f
```

---

## Security Model

**Authentication layers:**

1. **GitHub OAuth** — the browser user must authenticate with GitHub. The hub
   only routes traffic if the authenticated GitHub login matches the
   `github_user` the home device registered under.

2. **Tunnel token** — a random 32-byte secret stored in Redis and issued per
    GitHub user. The home device must present this token when opening the
    tunnel. An unauthenticated device cannot claim any GitHub username.

3. **TLS** — all browser↔hub and (optionally) hub↔home-device traffic is
    encrypted. Caddy provisions and renews certificates automatically via ACME.

4. **Device and port binding** — a `device_id` stays bound to the GitHub user
   that first claimed it, and the hub only forwards to ports the home device
   explicitly advertised during registration.

**What the hub can see:**

The hub proxies HTTP/WebSocket frames between the browser and the home device.
Request and response bodies (including LLM prompts sent to openclaw) pass
through the hub's memory briefly during proxying. Run the hub on hardware you
control if this matters to you.

**Rotating the tunnel token:**

```bash
# While logged in, call the regenerate endpoint:
curl -X POST https://cloud.example.com/auth/tunnel-token/regenerate \
  --cookie "session=<your_session_cookie>"

# Update the home device:
sudo snap set ailab cloud.token=<new_token>
sudo snap restart ailab.web
```

---

## URL Routing

The hub supports two routing modes simultaneously.

### Host-header routing (wildcard DNS required)

```
https://{device_id}.{domain}/           → AI Lab Web UI (port 11500)
https://{device_id}-{port}.{domain}/    → specific port on the device
```

Examples:

```
https://myhome.cloud.example.com/            → AI Lab Web UI
https://myhome-18789.cloud.example.com/      → OpenClaw
https://myhome-3000.cloud.example.com/       → Nullclaw
https://myhome-18800.cloud.example.com/      → PicoClaw
```

Requires `dns.provider` + `dns.token` to be set (for the wildcard TLS cert).
Without a wildcard cert, Caddy cannot terminate TLS for `*.domain` subdomains.

### Path-based routing (always available, no DNS-01 needed)

```
https://{domain}/d/{device_id}/          → AI Lab Web UI (port 11500)
https://{domain}/d/{device_id}:{port}/   → specific port
```

Examples:

```
https://cloud.example.com/d/myhome/
https://cloud.example.com/d/myhome:18789/
```

Path-based routing works with a standard HTTP-01 certificate (bare domain
only). It is the fallback if `dns.provider` is not set, and it is also
useful during initial setup before DNS propagation completes.

---

## Configuration Reference

All hub settings are snap settings, read by the wrapper scripts at service
start and exported as `AILAB_CLOUD_*` environment variables.

### Required

| Snap setting | Description |
|---|---|
| `domain` | Base domain, e.g. `cloud.example.com` |
| `github.client-id` | GitHub OAuth App client ID |
| `github.client-secret` | GitHub OAuth App client secret |

### Wildcard TLS (required for host-header routing)

| Snap setting | Description |
|---|---|
| `dns.provider` | DNS provider module name (see table below) |
| `dns.token` | API token (or access key ID for route53) |
| `dns.token-secret` | Secret access key (route53 only) |
| `dns.user` | API username (namecheap, godaddy only) |

### Optional

| Snap setting | Default | Description |
|---|---|---|
| `session.secret` | auto-generated | Secret for signing session cookies |
| `redis.url` | `redis://127.0.0.1:6379` | Override to use an external Redis |
| `web.host` | `127.0.0.1` | Hub bind address (Caddy proxies from 443) |
| `web.port` | `8080` | Hub bind port |

### Home device settings (`snap set ailab ...`)

| Setting | Description |
|---|---|
| `cloud.enabled` | `true` to start the tunnel client |
| `cloud.host` | Hub URL, e.g. `https://cloud.example.com` |
| `cloud.user` | Your GitHub username |
| `cloud.token` | Tunnel token from `/auth/tunnel-token` |
| `cloud.device-id` | Short identifier using lowercase letters, digits, and hyphens; becomes part of the URL and must be unique on the hub |

---

## Supported DNS Providers

The bundled Caddy binary includes DNS provider plugins for the most common
registrars and DNS services. The `dns.provider` snap setting corresponds
directly to the Caddy DNS module name.

### Single-token providers

Set `dns.provider` and `dns.token`:

| Provider | `dns.provider` value | Token type |
|---|---|---|
| Cloudflare | `cloudflare` | API token (Zone:DNS:Edit scope) |
| DigitalOcean | `digitalocean` | Personal access token |
| Hetzner | `hetzner` | DNS API token |
| DuckDNS | `duckdns` | DuckDNS token |

```bash
# Cloudflare example
sudo snap set ailab-cloud dns.provider=cloudflare
sudo snap set ailab-cloud dns.token=<API_TOKEN>
```

### AWS Route 53

Set `dns.provider=route53`, `dns.token` (access key ID), and
`dns.token-secret` (secret access key):

```bash
sudo snap set ailab-cloud dns.provider=route53
sudo snap set ailab-cloud dns.token=AKIAIOSFODNN7EXAMPLE
sudo snap set ailab-cloud dns.token-secret=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

The IAM user or role must have `route53:ChangeResourceRecordSets` and
`route53:ListHostedZonesByName` permissions on the hosted zone.

### Namecheap / GoDaddy

Set `dns.provider`, `dns.token` (API key), and `dns.user` (API username):

```bash
# Namecheap
sudo snap set ailab-cloud dns.provider=namecheap
sudo snap set ailab-cloud dns.user=<Namecheap username>
sudo snap set ailab-cloud dns.token=<API key>

# GoDaddy
sudo snap set ailab-cloud dns.provider=godaddy
sudo snap set ailab-cloud dns.user=<GoDaddy API key>
sudo snap set ailab-cloud dns.token=<GoDaddy API secret>
```

### Getting a Cloudflare API token (recommended)

1. Log in to Cloudflare → **My Profile → API Tokens → Create Token**.
2. Use the **Edit zone DNS** template.
3. Under **Zone Resources**, select the specific zone for your domain.
4. Click **Continue to summary** → **Create Token**.
5. Copy the token — it is shown only once.

---

## Development / Local Setup

To run the hub locally without a VPS or domain:

```bash
git clone https://github.com/lemonade-sdk/ailab-cloud
cd ailab-cloud
pip install -e .

# Local Redis (Docker or snap)
docker run -d -p 6379:6379 redis:7-alpine

# Required environment variables
export AILAB_CLOUD_DOMAIN=localhost:8080
export AILAB_CLOUD_GITHUB_CLIENT_ID=<client_id>
export AILAB_CLOUD_GITHUB_CLIENT_SECRET=<client_secret>
export AILAB_CLOUD_SESSION_SECRET=$(openssl rand -hex 32)
export AILAB_CLOUD_REDIS_URL=redis://127.0.0.1:6379

uvicorn ailab_cloud.main:app --reload --host 127.0.0.1 --port 8080
```

For local development, create a GitHub OAuth App with callback URL
`http://localhost:8080/auth/callback`.

Path-based routing (`/d/{device_id}/...`) works without TLS or DNS in
development. The home device connects with:

```bash
sudo snap set ailab cloud.host=http://localhost:8080
```

---

## Troubleshooting

**Services won't start after `snap start ailab-cloud`**

```bash
sudo snap logs ailab-cloud.hub -n 30
```

Look for `Missing required environment variables` and set any that are listed.

**Caddy log shows `no such host` or DNS challenge errors**

The wildcard DNS record is not yet in place or has not propagated. Verify:

```bash
dig anything.cloud.example.com A +short   # should return your VPS IP
```

Until the wildcard record is set, path-based routing
(`https://cloud.example.com/d/myhome/`) is still available via the bare-domain
certificate.

**Caddy log shows `permission denied` binding to port 443**

The snap's `network-bind` plug should allow this. Confirm the plug is
connected:

```bash
sudo snap connections ailab-cloud | grep network-bind
```

If the plug is disconnected:

```bash
sudo snap connect ailab-cloud:network-bind
sudo snap restart ailab-cloud.caddy
```

**Browser shows 403 Access Denied**

The GitHub account you logged in with does not match the `cloud.user` set on
the home device. Both must use the same GitHub login.

**"Device is not connected" in the browser**

The tunnel client on the home device is not running or was disconnected:

```bash
# On the home device:
sudo snap logs ailab.web -f
sudo snap restart ailab.web
```

The client reconnects automatically with exponential back-off.

**WebSocket shell terminal drops or freezes through the tunnel**

Caddy's default idle timeout may be closing the connection. The bundled Caddy
config does not set explicit timeouts (Caddy's defaults are generous), but if
you are running a custom Caddy config in front, add:

```
reverse_proxy 127.0.0.1:8080 {
    transport http {
        read_timeout  1h
        write_timeout 1h
    }
}
```

**Wildcard subdomain routing not working despite correct DNS**

Verify that `dns.provider` and `dns.token` are set and that Caddy successfully
obtained the wildcard certificate:

```bash
sudo snap logs ailab-cloud.caddy | grep -i "certificate\|error"
```

If the certificate was not obtained, check that the API token has the correct
permissions for your DNS provider (see
[Supported DNS Providers](#supported-dns-providers)).

**Redis connection refused on startup**

Redis starts before the hub (`after: [redis]` in the snap). If the hub starts
before Redis is ready, it will retry the connection automatically. Check Redis:

```bash
sudo snap logs ailab-cloud.redis -n 20
sudo snap services ailab-cloud
```
