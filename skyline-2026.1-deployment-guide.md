# Skyline Dashboard 2026.1 — Complete Source Deployment Guide
## Ubuntu 24.04 LTS (LXD Container), Single-Node (APIServer + Console + DB)

> **Target versions:** `skyline-apiserver` 8.0.x (stable/2026.1) · `skyline-console` 8.0.x (stable/2026.1)
> **OS:** Ubuntu 24.04 LTS (Noble) in an LXD container
> **Listener port:** `9999` (HTTP) — avoids conflict with Horizon on 80/443

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Pre-Installation Analysis & Known Issues](#2-pre-installation-analysis--known-issues)
3. [OpenStack Prerequisites (run on controller node)](#3-openstack-prerequisites-run-on-controller-node)
4. [Container Baseline Setup](#4-container-baseline-setup)
5. [Part A — Install MariaDB (in container)](#5-part-a--install-mariadb-in-container)
6. [Part B — Install skyline-apiserver](#6-part-b--install-skyline-apiserver)
7. [Part C — Install skyline-console](#7-part-c--install-skyline-console)
8. [Port Configuration — Listen on 9999](#8-port-configuration--listen-on-9999)
9. [Service Management & Verification](#9-service-management--verification)
10. [Login Page Customisation](#10-login-page-customisation)
11. [Juju Charm Integration Notes](#11-juju-charm-integration-notes)
12. [Troubleshooting Reference](#12-troubleshooting-reference)

---

## 1. Architecture Overview

```
  Browser :9999
       │
       ▼
  ┌──────────────────────────┐
  │         nginx            │  ← serves static JS/CSS, proxies /api/*
  │  listen 0.0.0.0:9999     │
  └───────────┬──────────────┘
              │ proxy_pass → 127.0.0.1:28000
              ▼
  ┌──────────────────────────┐
  │  gunicorn (apiserver)    │  ← Python ASGI app in venv
  │  bind 127.0.0.1:28000    │
  └───────────┬──────────────┘
              │
              ▼
  ┌──────────────────────────┐
  │   MariaDB (localhost)    │  ← skyline database, all inside container
  └──────────────────────────┘
              │
              ▼ (OpenStack API calls over network)
  ┌──────────────────────────┐
  │  Keystone / Nova / etc.  │  ← your existing OpenStack cluster
  └──────────────────────────┘
```

**Key design decisions in this guide:**
- gunicorn is bound to `127.0.0.1:28000` (loopback only) — minimal attack surface
- skyline-apiserver runs inside a dedicated Python virtualenv at `/opt/skyline-venv`
- skyline-console Python wheel is installed into the **same** venv, keeping system Python clean
- nginx is the sole public listener, on port `9999`
- All components share one LXD container — suitable as a charm recipe

---

## 2. Pre-Installation Analysis & Known Issues

Read this section fully before starting. These are the failure modes most commonly encountered.

### 2.1 Python: virtualenv vs system packages

The official upstream docs use `sudo pip3 install ... --break-system-packages`. On Ubuntu 24.04 (which enforces PEP 668) this is fragile and pollutes system Python. **This guide uses a dedicated virtualenv** at `/opt/skyline-venv` instead. This is also how a proper Juju charm should manage Python applications.

### 2.2 Node.js version (most likely to age poorly)

The upstream 2026.1 install guide pins `lts/gallium` (Node 16). Node 16 reached end-of-life in September 2023. It may still build successfully because the skyline-console JavaScript code has not changed its Node requirement in recent cycles, but you should **verify the actual engines field** in the repo before assuming it forever:

```bash
cat /root/skyline-console/package.json | python3 -c \
  "import sys,json; e=json.load(sys.stdin).get('engines',{}); print(e)"
```

If the `engines.node` field shows `>=18` or similar, switch to `lts/hydrogen` (Node 18) or `lts/iron` (Node 20) accordingly. This guide follows the upstream documentation (`lts/gallium`, Node 16) but documents this check explicitly.

### 2.3 gunicorn bind address

The upstream docs bind gunicorn to `0.0.0.0:28000`, which exposes the raw Python API on all interfaces — unnecessary when nginx sits in front. **This guide binds gunicorn to `127.0.0.1:28000`.**

### 2.4 Nginx port conflict

Ports 80 and 443 are already used by Horizon on the same host or network. **This guide configures nginx to listen on port 9999.** The `skyline-nginx-generator` tool always generates a config that listens on 80/443 via SSL, so we edit the generated config to replace the listener ports.

### 2.5 ssl-cert / self-signed certificate requirement

`skyline-nginx-generator` generates an nginx config that references the `snakeoil` self-signed cert provided by the `ssl-cert` package (`/etc/ssl/certs/ssl-cert-snakeoil.pem`). Install this package or the nginx config will fail to start. This guide installs it.

### 2.6 ca-certificates inside LXD containers

Fresh LXD containers sometimes lack `ca-certificates`, causing `git clone` over HTTPS to fail. This guide installs it early.

### 2.7 `skyline-nginx-generator` binary location

After installing skyline-console inside the venv, the `skyline-nginx-generator` binary lives at `/opt/skyline-venv/bin/skyline-nginx-generator`. It **must** be called from within the activated venv or with its full path. Calling it as `skyline-nginx-generator` without activating the venv will result in "command not found".

### 2.8 Database on the same container

This guide installs MariaDB inside the container. The connection URL will therefore use `localhost` (or `127.0.0.1`). If you later move the database to a separate host, change `database_url` in `/etc/skyline/skyline.yaml` and re-run `db_sync`.

### 2.9 `make db_sync` requires activated venv

The `Makefile` inside `skyline-apiserver/` calls Python tools. It must be run with the virtualenv active, or the venv's Python must be on `PATH`. This guide activates the venv before running `make db_sync`.

### 2.10 `libgconf-2-4` removed from Ubuntu 24.04

The Ubuntu 24.04 package `libgconf-2-4` (needed for headless Cypress tests during `make package`) has been removed. The official docs already account for this: the Ubuntu 24.04 dependency list omits it. Do not attempt to install it; use the 24.04-specific package list in this guide.

---

## 3. OpenStack Prerequisites (run on controller node)

These steps run on your **existing OpenStack controller**, not in the LXD container.

### 3.1 Create the Skyline service user

```bash
# Source admin credentials
source /etc/kolla/admin-openrc.sh   # adjust path to your admin-openrc

# Create skyline user — you will be prompted for a password
openstack user create --domain default --password-prompt skyline

# Grant admin role in the service project
openstack role add --project service --user skyline admin
```

> **Note:** The `admin` role is required because Skyline makes admin-level API calls on behalf of users. Record the password you set — it becomes `SKYLINE_SERVICE_PASSWORD` later.

---

## 4. Container Baseline Setup

All remaining steps run **inside the LXD container** as `root` (or via `sudo`).

### 4.1 Enter the container

```bash
lxc exec <your-container-name> -- bash
```

### 4.2 Install baseline packages

```bash
apt update
apt install -y \
  ca-certificates \
  git \
  curl \
  python3 \
  python3-pip \
  python3-venv \
  build-essential \
  make \
  nginx \
  ssl-cert
```

> `ca-certificates` is listed first to prevent git clone failures over HTTPS.

### 4.3 Create the Python virtualenv

```bash
python3 -m venv /opt/skyline-venv
source /opt/skyline-venv/bin/activate
pip install --upgrade pip
```

> Leave the virtualenv activated for the remainder of the installation unless explicitly told to deactivate.

---

## 5. Part A — Install MariaDB (in container)

### 5.1 Install MariaDB server

```bash
apt install -y mariadb-server
systemctl enable mariadb
systemctl start mariadb
```

### 5.2 Secure the installation (optional but recommended)

```bash
mysql_secure_installation
```

Follow the prompts. Set a root password if you haven't already.

### 5.3 Create the Skyline database and user

```bash
mysql -u root <<'EOF'
CREATE DATABASE skyline DEFAULT CHARACTER SET utf8 DEFAULT COLLATE utf8_general_ci;
GRANT ALL PRIVILEGES ON skyline.* TO 'skyline'@'localhost' IDENTIFIED BY 'SKYLINE_DBPASS';
GRANT ALL PRIVILEGES ON skyline.* TO 'skyline'@'%'         IDENTIFIED BY 'SKYLINE_DBPASS';
FLUSH PRIVILEGES;
EOF
```

> Replace `SKYLINE_DBPASS` with a strong password of your choice. Record it.

---

## 6. Part B — Install skyline-apiserver

### 6.1 Clone the stable/2026.1 branch

```bash
cd /root
git clone https://opendev.org/openstack/skyline-apiserver.git \
  --branch stable/2026.1 \
  --single-branch
```

> If you see `server certificate verification failed`, run `apt install -y ca-certificates` and retry.

### 6.2 Install into the virtualenv

```bash
# Ensure the venv is active
source /opt/skyline-venv/bin/activate

pip install --upgrade pip
#pip install PyMySQL gunicorn
pip install /root/skyline-apiserver/
```

> This installs `skyline-apiserver` and all its Python dependencies cleanly inside `/opt/skyline-venv`. No system Python packages are modified.

### 6.3 Create required directories

```bash
mkdir -p /etc/skyline /var/log/skyline /etc/skyline/policy
```

### 6.4 Copy and configure gunicorn

```bash
cp /root/skyline-apiserver/etc/gunicorn.py /etc/skyline/gunicorn.py
```

Edit `/etc/skyline/gunicorn.py` and set the `bind` value to loopback:

```bash
sed -i "s|^bind = .*|bind = ['127.0.0.1:28000']|g" /etc/skyline/gunicorn.py
```

Verify the result:

```bash
grep "^bind" /etc/skyline/gunicorn.py
# Expected output:
# bind = ['127.0.0.1:28000']
```

### 6.5 Copy and configure skyline.yaml

```bash
cp /root/skyline-apiserver/etc/skyline.yaml.sample /etc/skyline/skyline.yaml
```

Edit `/etc/skyline/skyline.yaml`. The minimum required changes are shown below. Replace all placeholders in angle brackets:

```yaml
default:
  database_url: mysql+pymysql://skyline:SKYLINE_DBPASS@localhost:3306/skyline
  debug: false
  log_dir: /var/log/skyline

openstack:
  keystone_url: http://KEYSTONE_SERVER:5000/v3/
  default_region: RegionOne
  system_user_name: skyline
  system_user_password: SKYLINE_SERVICE_PASSWORD
  system_user_domain: Default
  system_project: service
  system_project_domain: Default
  interface_type: public
```

Substitution map:

| Placeholder | Replace with |
|---|---|
| `SKYLINE_DBPASS` | The MariaDB password set in step 5.3 |
| `KEYSTONE_SERVER` | The IP or hostname of your Keystone endpoint |
| `SKYLINE_SERVICE_PASSWORD` | The password for the `skyline` OpenStack user (step 3.1) |
| `RegionOne` | Your actual region name if different |

> **Tip:** To find your Keystone URL, run `openstack endpoint list --service keystone --interface public` on the controller node.

### 6.6 Populate the database

```bash
# Ensure venv is active
source /opt/skyline-venv/bin/activate

cd /root/skyline-apiserver/
make db_sync
```

Expected output ends with something like:
```
INFO  [alembic.runtime.migration] Running upgrade ... -> ..., ...
```

If you see a database connection error, verify the `database_url` in `skyline.yaml` and that MariaDB is running and accepting connections.

### 6.7 Create the systemd service

Create `/etc/systemd/system/skyline-apiserver.service`:

```ini
[Unit]
Description=Skyline APIServer
After=network.target mariadb.service
Wants=mariadb.service

[Service]
Type=simple
User=root
Environment="OS_CONFIG_DIR=/etc/skyline"
ExecStart=/opt/skyline-venv/bin/gunicorn \
  -c /etc/skyline/gunicorn.py \
  skyline_apiserver.main:app
LimitNOFILE=32768
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable skyline-apiserver
systemctl start skyline-apiserver
```

### 6.8 Verify the apiserver is running

```bash
systemctl status skyline-apiserver

# Confirm gunicorn is listening on loopback port 28000
ss -tlnp | grep 28000
# Expected: LISTEN 0 ... 127.0.0.1:28000 ...

# Quick API health check
curl -s http://127.0.0.1:28000/api/openstack/skyline/version | python3 -m json.tool
```

---

## 7. Part C — Install skyline-console

### 7.1 Install system build dependencies (Ubuntu 24.04 specific)

```bash
apt install -y \
  libgtk2.0-0 \
  libgtk-3-0 \
  libgbm-dev \
  libnotify-dev \
  libnss3 \
  libxss1 \
  libasound2t64 \
  libxtst6 \
  xauth \
  xvfb
```

> **Ubuntu 24.04 note:** Use `libasound2t64` (not `libasound2`). The package `libgconf-2-4` has been removed from Ubuntu 24.04 and must **not** be listed.

### 7.2 Install nvm (Node Version Manager)

```bash
wget -P /root/ \
  --tries=10 \
  --retry-connrefused \
  --waitretry=60 \
  --no-dns-cache \
  --no-cache \
  https://raw.githubusercontent.com/nvm-sh/nvm/master/install.sh

bash /root/install.sh
```

Load nvm into the current shell:

```bash
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
```

> Add these two lines to `/root/.bashrc` so nvm is available in future shells.

### 7.3 Check required Node.js version from package.json

Clone the repository first so you can inspect it:

```bash
cd /root
git clone https://opendev.org/openstack/skyline-console.git \
  --branch stable/2026.1 \
  --single-branch
```

**Inspect the engines field before installing Node:**

```bash
cat /root/skyline-console/package.json | python3 -c \
  "import sys, json; d=json.load(sys.stdin); print(d.get('engines', 'no engines field found'))"
```

Based on the documented requirements for 2026.1, the expected Node LTS is `gallium` (Node 16). If the output shows a different version requirement, install that version instead of the one below.

### 7.4 Install Node.js (lts/gallium — Node 16)

```bash
nvm install --lts=gallium
nvm alias default lts/gallium
nvm use default
```

Confirm versions:

```bash
node -v   # should print v16.x.x
npm -v    # should print 8.x.x
```

### 7.5 Install yarn

```bash
npm install -g yarn
```

### 7.6 Build the skyline-console wheel

```bash
cd /root/skyline-console
make package
```

This step compiles the React application and packages it as a Python wheel. It takes several minutes. Expected output ends with a `dist/` directory containing `skyline_console-*.whl`.

> **If the build fails with memory errors:** Add swap space or increase container RAM. The Node.js build process is memory-intensive. 2 GB RAM minimum is recommended; 4 GB is comfortable.

```bash
ls -lh dist/skyline_console-*.whl
```

### 7.7 Install the wheel into the virtualenv

```bash
source /opt/skyline-venv/bin/activate

pip install --force-reinstall /root/skyline-console/dist/skyline_console-*.whl
```

### 7.8 Confirm required directories exist

```bash
# These should already exist from the apiserver step; ensure they do
mkdir -p /etc/skyline /var/log/skyline
```

`/etc/skyline/skyline.yaml` must already be present (configured in step 6.5). The nginx configuration generator reads it.

### 7.9 Generate the nginx configuration

The `skyline-nginx-generator` binary is inside the venv:

```bash
source /opt/skyline-venv/bin/activate

/opt/skyline-venv/bin/skyline-nginx-generator -o /etc/nginx/nginx.conf
```

**Update the upstream backend address** to match the loopback-bound gunicorn:

```bash
sed -i \
  "s|server .* fail_timeout=0;|server 127.0.0.1:28000 fail_timeout=0;|g" \
  /etc/nginx/nginx.conf
```

The port change (80/443 → 9999) is handled in the next dedicated section.

---

## 8. Port Configuration — Listen on 9999

By default, `skyline-nginx-generator` creates a configuration that listens on ports 80 (HTTP) and 443 (HTTPS). Because Horizon already occupies those ports, we redirect all traffic to port `9999`.

### 8.1 Understand the generated nginx.conf structure

The generated `/etc/nginx/nginx.conf` contains:
- An HTTP `server` block listening on port `80` that redirects to HTTPS
- An HTTPS `server` block listening on port `443` serving the application

### 8.2 Edit nginx.conf to listen on 9999

Open `/etc/nginx/nginx.conf` in your editor. You will find two `listen` directives. Change them both:

```bash
# Change the HTTP redirect listener from 80 to 9999-http (or remove the redirect block entirely)
# Change the HTTPS listener from 443 to 9999

sed -i 's/listen 80;/listen 9999;/g'   /etc/nginx/nginx.conf
sed -i 's/listen 443 ssl;/listen 9999 ssl;/g' /etc/nginx/nginx.conf
```

> If you want **HTTP-only** on port 9999 (acceptable for an internal-only LXD container behind a trusted network), you can simplify the config substantially. See the alternative HTTP-only config below.

### 8.3 Alternative: HTTP-only nginx config on port 9999

For an internal deployment where HTTPS is terminated upstream (or where you simply want to avoid certificate management), replace the generated config entirely with this minimal HTTP-only version. It still proxies to gunicorn and serves static files.

First, locate where skyline-console static files are installed:

```bash
STATIC_PATH=$(python3 -c \
  "import skyline_console; import os; print(os.path.dirname(skyline_console.__file__))")/static
echo $STATIC_PATH
# Typically: /opt/skyline-venv/lib/python3.12/site-packages/skyline_console/static
```

Then write the nginx config:

```bash
cat > /etc/nginx/nginx.conf <<EOF
user www-data;
worker_processes auto;
pid /run/nginx.pid;

events {
    worker_connections 1024;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    sendfile        on;
    keepalive_timeout 65;

    upstream skyline {
        server 127.0.0.1:28000 fail_timeout=0;
    }

    server {
        listen 0.0.0.0:9999;
        server_name _;

        access_log /var/log/nginx/skyline_access.log;
        error_log  /var/log/nginx/skyline_error.log;

        # Proxy API requests to gunicorn
        location /api {
            proxy_pass         http://skyline;
            proxy_set_header   Host              \$host;
            proxy_set_header   X-Real-IP         \$remote_addr;
            proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
            proxy_set_header   X-Forwarded-Proto \$scheme;
            proxy_read_timeout 600;
            client_max_body_size 10g;
        }

        # Serve the compiled React frontend
        location / {
            root  ${STATIC_PATH};
            index index.html;
            try_files \$uri \$uri/ /index.html;
        }
    }
}
EOF
```

### 8.4 Test and apply the nginx configuration

```bash
nginx -t
# Expected: nginx: configuration file /etc/nginx/nginx.conf test is successful

systemctl enable nginx
systemctl restart nginx
```

Verify nginx is listening on 9999:

```bash
ss -tlnp | grep 9999
# Expected: LISTEN 0 ... 0.0.0.0:9999 ...
```

---

## 9. Service Management & Verification

### 9.1 Summary of all services

| Service | Unit file | Port |
|---|---|---|
| MariaDB | `mariadb.service` | 3306 (localhost only) |
| Skyline APIServer | `skyline-apiserver.service` | 127.0.0.1:28000 |
| Nginx | `nginx.service` | 0.0.0.0:9999 |

### 9.2 Start and enable all services

```bash
systemctl enable --now mariadb
systemctl enable --now skyline-apiserver
systemctl enable --now nginx
```

### 9.3 Check all service statuses

```bash
systemctl status mariadb skyline-apiserver nginx
```

### 9.4 View logs

```bash
# Gunicorn / APIServer logs
journalctl -u skyline-apiserver -f

# Skyline application logs (if log_dir is set in skyline.yaml)
tail -f /var/log/skyline/*.log

# Nginx access/error logs
tail -f /var/log/nginx/skyline_access.log
tail -f /var/log/nginx/skyline_error.log
```

### 9.5 Access the dashboard

Open a browser and navigate to:

```
http://<CONTAINER_IP>:9999
```

Log in with any valid OpenStack user credentials. The `skyline` service user itself is not meant for browser login.

---

## 10. Login Page Customisation

Skyline Console is a compiled React application. The compiled static assets — including images — live inside the installed Python package. Post-install customisation falls into two categories:

### Category A — Replacing images (no rebuild needed)

Images in the compiled bundle can be replaced in-place. The bundle references them by filename, so replacing a file with a same-named file of the correct dimensions is sufficient. No rebuild or service restart is needed — only nginx needs to reload if you change files it serves directly.

### Category B — Changing text (requires rebuild)

Text strings (login page title, subtitle, product name, etc.) are compiled into the JavaScript bundle. To change them you must edit the source, rebuild the wheel, and reinstall it.

---

### 10.1 Locating the installed static files

```bash
# Activate the venv first
source /opt/skyline-venv/bin/activate

# Find the static directory
python3 -c \
  "import skyline_console, os; print(os.path.join(os.path.dirname(skyline_console.__file__), 'static'))"
```

This will print something like:
```
/opt/skyline-venv/lib/python3.12/site-packages/skyline_console/static
```

Save this path to a variable for convenience:

```bash
SKYLINE_STATIC=$(python3 -c \
  "import skyline_console, os; print(os.path.join(os.path.dirname(skyline_console.__file__), 'static'))")
echo $SKYLINE_STATIC
```

### 10.2 Finding login page images

```bash
# List everything in the static root
ls $SKYLINE_STATIC/

# Search for image files that relate to login / background
find $SKYLINE_STATIC -type f \( -iname "*.png" -o -iname "*.jpg" -o -iname "*.svg" \) \
  | grep -i -E "login|bg|background|logo|banner" \
  | sort
```

The typical files you will find (filenames may include a content hash suffix):

| Purpose | Likely filename pattern |
|---|---|
| Login page background image | `bg_login*.png` or `login-bg*.png` or `background*.png` |
| Login page logo / product logo | `logo*.svg` or `logo*.png` |
| Browser favicon | `favicon.ico` or `favicon*.png` |
| Header logo (post-login) | `logo*.svg` |

> Because the filenames include a webpack content hash (e.g. `logo.a3b4c5d6.svg`), use `find` and `grep` as shown above rather than guessing exact names. The hash changes on each build.

### 10.3 Replacing the login background image

1. Identify the exact filename from the output of step 10.2.
2. Prepare your replacement image at the **same dimensions** as the original (check with `file <original>` or an image editor).
3. Back up the original:

```bash
cp $SKYLINE_STATIC/bg_login.a3b4c5d6.png \
   $SKYLINE_STATIC/bg_login.a3b4c5d6.png.bak
```

4. Copy your image over the original, keeping the exact filename:

```bash
cp /path/to/your-custom-background.png \
   $SKYLINE_STATIC/bg_login.a3b4c5d6.png
```

5. Reload nginx (no service restart needed):

```bash
systemctl reload nginx
```

6. Hard-refresh the browser (Ctrl+Shift+R) to bypass browser cache.

### 10.4 Replacing the logo

Follow the same procedure as 10.3. SVG logos can be any vector content but must match the original filename exactly.

---

### 10.5 Changing login page text (requires rebuild)

To change text such as the product name, login title, or subtitle displayed on the login page, you must edit the source code before building.

**Step 1 — Locate the login page source component**

```bash
find /root/skyline-console/src -type f -name "*.jsx" \
  | xargs grep -l -i "login\|sign in\|openstack" 2>/dev/null \
  | grep -i login
```

The primary login page component is typically at:

```
/root/skyline-console/src/pages/base/Login/index.jsx
```

**Step 2 — Inspect and edit the component**

```bash
grep -n "OpenStack\|Skyline\|Login\|Welcome\|Sign" \
  /root/skyline-console/src/pages/base/Login/index.jsx
```

Open the file in your editor and change the text strings you want to customise. Common locations:

- Product name / title: look for a string like `'Skyline'` or `t('Skyline')` (where `t()` is the i18n helper)
- Login subtitle or description: look for strings containing `'OpenStack'` or similar descriptive text

For example, to change the login title from `Skyline` to `My Cloud Portal`:

```jsx
// Before
<h1>{t('Skyline')}</h1>

// After
<h1>{t('My Cloud Portal')}</h1>
```

> **i18n note:** If the project uses the `t()` translation function, and you want the change to apply regardless of locale, either hard-code the string directly (bypassing `t()`) or add the translated string to all locale files under `src/locales/`.

**Step 3 — Rebuild the wheel**

```bash
# Make sure nvm and the correct node version are loaded
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
nvm use lts/gallium

cd /root/skyline-console
make package
```

**Step 4 — Reinstall the wheel into the venv**

```bash
source /opt/skyline-venv/bin/activate
pip install --force-reinstall /root/skyline-console/dist/skyline_console-*.whl
```

**Step 5 — Regenerate nginx config and reload**

After reinstalling, the static files are in a new path (because content hashes change with every build). Regenerate the nginx config:

```bash
/opt/skyline-venv/bin/skyline-nginx-generator -o /etc/nginx/nginx.conf
```

Then re-apply your port customisation (section 8) since the generator overwrites your edits:

```bash
sed -i \
  "s|server .* fail_timeout=0;|server 127.0.0.1:28000 fail_timeout=0;|g" \
  /etc/nginx/nginx.conf

# Re-apply your port change (if using the sed approach)
sed -i 's/listen 80;/listen 9999;/g'       /etc/nginx/nginx.conf
sed -i 's/listen 443 ssl;/listen 9999 ssl;/g' /etc/nginx/nginx.conf
```

Or, if you are using the manual HTTP-only nginx.conf from section 8.3, update `STATIC_PATH` and rewrite the config again.

Reload nginx:

```bash
nginx -t && systemctl reload nginx
```

> **Charm tip:** Store your nginx.conf template separately from the generated one. In the charm, generate the file, apply patches, then write the final config in one idempotent step.

---

## 11. Juju Charm Integration Notes

This section summarises considerations for implementing the above as a Juju charm.

### 11.1 Python dependency management

Use the `venv` charm library pattern. The charm should:

1. Create `/opt/skyline-venv` with `python3 -m venv`
2. Install packages using `/opt/skyline-venv/bin/pip`
3. Reference all binaries by full path: `/opt/skyline-venv/bin/gunicorn`, `/opt/skyline-venv/bin/skyline-nginx-generator`

Never pass `--break-system-packages` to pip in a charm.

### 11.2 systemd service ExecStart

```ini
ExecStart=/opt/skyline-venv/bin/gunicorn \
  -c /etc/skyline/gunicorn.py \
  skyline_apiserver.main:app
```

### 11.3 Configuration templating

Manage `/etc/skyline/skyline.yaml` as a charm-rendered Jinja2 template. Expose these as charm config options:

- `database-url`
- `keystone-url`
- `default-region`
- `system-user-password`
- `listen-port` (default: `9999`)
- `debug` (default: `false`)

### 11.4 Node.js version

Do not hard-code `lts/gallium`. Read the `engines.node` field from `package.json` at charm install time and install the appropriate LTS. This makes the charm forward-compatible.

### 11.5 nginx config management

Keep your nginx.conf as a Jinja2 template in the charm. Do **not** rely on `skyline-nginx-generator` in production — use it once to understand the structure, then maintain a charm-owned template. This avoids the generator overwriting charm-managed config on upgrades.

### 11.6 Idempotency

The `make db_sync` step is safe to run multiple times (Alembic checks existing schema). Run it on every charm upgrade to ensure the schema is current.

---

## 12. Troubleshooting Reference

### `systemctl status skyline-apiserver` shows failed / inactive

```bash
journalctl -u skyline-apiserver --no-pager -n 50
```

Common causes:
- `skyline.yaml` has wrong `database_url` — check MariaDB is running and credentials match
- `OS_CONFIG_DIR` environment variable not set — ensure the systemd unit has `Environment="OS_CONFIG_DIR=/etc/skyline"`
- gunicorn binary path wrong — verify `/opt/skyline-venv/bin/gunicorn` exists

### nginx fails to start: `[emerg] invalid variable name`

This happens when the nginx config contains dollar signs that are not properly escaped. The `skyline-nginx-generator` may produce these. Check `/etc/nginx/nginx.conf` around line 148 (as noted in the upstream bug tracker). Escape any literal `$` that should not be nginx variables, or use the manual HTTP-only config from section 8.3.

### `make db_sync` fails with `ModuleNotFoundError`

The venv is not activated. Run:

```bash
source /opt/skyline-venv/bin/activate
cd /root/skyline-apiserver/
make db_sync
```

### `make package` for skyline-console hangs or runs out of memory

The Node.js build is memory-intensive. Check available memory:

```bash
free -h
```

If RAM is under 2 GB, add a swap file:

```bash
fallocate -l 4G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
```

### Login page shows 502 Bad Gateway

nginx can reach port 9999, but cannot proxy to port 28000. Check:

```bash
ss -tlnp | grep 28000            # gunicorn must be listening
curl http://127.0.0.1:28000/     # should return a JSON response
systemctl status skyline-apiserver
```

### Browser login fails with "401 Unauthorized"

The `skyline` OpenStack user does not have the `admin` role in the `service` project. Re-run:

```bash
openstack role add --project service --user skyline admin
```

Also verify that `keystone_url` in `skyline.yaml` is reachable from the container:

```bash
curl http://KEYSTONE_SERVER:5000/v3/
```

### Port 9999 not reachable from outside the container

Check the LXD container's network profile — ensure port 9999 is not blocked by a container-level firewall or the host's iptables. For a bridged container profile, traffic should flow freely; for a NAT profile you may need a proxy device:

```bash
lxc config device add <container> skyline-http proxy \
  listen=tcp:0.0.0.0:9999 \
  connect=tcp:127.0.0.1:9999
```

---

*Guide prepared for Skyline 2026.1 (skyline-apiserver 8.0.x, skyline-console 8.0.x) on Ubuntu 24.04 LTS. Last reviewed: April 2026.*