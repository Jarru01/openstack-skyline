# skyline Juju Charm

Deploys **OpenStack Skyline Dashboard** (stable/2024.2) including:

| Component | Detail |
|---|---|
| skyline-apiserver | Python ASGI app, gunicorn on `127.0.0.1:28000` |
| skyline-console | React SPA compiled to a Python wheel, served by nginx |
| MariaDB | Local instance (optional — skipped if `database-url` is set) |
| nginx | Public listener, default port `9999` |

---

## Directory Layout

```
skyline-charm/
├── charmcraft.yaml               # Build config: base Ubuntu 22.04, Python deps
├── metadata.yaml                 # Charm name, relations, series
├── config.yaml                   # All user-facing config options with defaults
├── actions.yaml                  # Juju actions (db-sync, restart-services, …)
├── requirements.txt              # Charm Python deps: ops, jinja2
├── src/
│   └── charm.py                  # Main ops-framework charm (all event handlers)
└── templates/
    ├── skyline.yaml.j2           # /etc/skyline/skyline.yaml
    ├── gunicorn.py.j2            # /etc/skyline/gunicorn.py
    ├── skyline-apiserver.service.j2   # systemd unit
    └── nginx.conf.j2             # /etc/nginx/nginx.conf
```

---

## Step-by-Step: Creating and Building the Charm (for First-Timers)

### 1. Install charmcraft

`charmcraft` is the official tool for building Juju charms.

```bash
sudo snap install charmcraft --classic
```

Verify:
```bash
charmcraft version
```

### 2. Understand the structure you have

```
skyline-charm/
├── charmcraft.yaml    ← tells charmcraft how to pack the charm
├── metadata.yaml      ← charm identity, relations, supported series
├── config.yaml        ← every `juju config` key lives here
├── actions.yaml       ← every `juju run-action` command lives here
├── requirements.txt   ← Python packages the charm itself needs (not the app!)
├── src/charm.py       ← the actual charm logic
└── templates/         ← Jinja2 templates rendered into /etc/skyline/ at deploy time
```

The key insight: **`src/charm.py` is the ops event loop**. Juju fires events
(`install`, `config-changed`, `start`, etc.) and this file responds to them.
Templates are rendered at runtime — they are not installed until Juju fires
`config-changed`.

### 3. Build the charm

From the `skyline-charm/` directory:

```bash
cd skyline-charm/
charmcraft pack
```

This produces a file like `skyline_ubuntu-22.04-amd64.charm` in the current
directory.  The `.charm` file is just a zip archive — you can inspect it with
`unzip -l skyline_ubuntu-22.04-amd64.charm`.

> **Note:** `charmcraft pack` runs in a temporary VM/container to ensure a
> clean build environment. If you are on a machine without LXD, run
> `charmcraft pack --destructive-mode` to build directly on the host
> (requires Ubuntu 22.04).

### 4. Bootstrap Juju (skip if already done)

```bash
# For an existing MAAS/MaaS or LXD provider already known to Juju:
juju bootstrap lxd lxd-controller
```

If you are adding the charm to an existing Juju model on your OpenStack
deployment, skip bootstrapping and just switch to the correct model:

```bash
juju switch <your-model>
```

### 5. Pre-deploy: Create the OpenStack skyline service user

Run this on any node that has the OpenStack client and admin credentials:

```bash
source /etc/kolla/admin-openrc.sh      # adjust to your openrc path

openstack user create \
  --domain admin_domain \
  --password-prompt \
  skyline

openstack role add \
  --project admin \
  --user skyline \
  admin
```

Record the password — you will need it as `system-user-password` below.

### 6. Deploy the charm

```bash
juju deploy ./skyline_ubuntu-22.04-amd64.charm \
  --config keystone-url="http://KEYSTONE_IP:5000/v3/" \
  --config system-user-password="THE_PASSWORD_YOU_SET_ABOVE" \
  --config default-region="RegionOne" \
  --to lxd:0                  # deploy into an LXD container on machine 0
```

Replace `KEYSTONE_IP` with the actual IP/hostname of your Keystone endpoint.

To find your Keystone public endpoint:
```bash
openstack endpoint list --service keystone --interface public
```

### 7. Watch the deployment

```bash
juju status --watch 5s
```

The unit will progress through:
```
maintenance: Installing system packages
maintenance: Installing MariaDB
maintenance: Creating Python virtualenv
maintenance: Installing skyline-apiserver
maintenance: Cloning skyline-console
maintenance: Building skyline-console wheel (takes several minutes)   ← slowest step
maintenance: Installing skyline-console wheel
maintenance: Rendering configuration
maintenance: Running database migration (db_sync)
active:      Skyline ready on :9999
```

The console build (React → Python wheel) takes **5–15 minutes** depending on
RAM. The container needs at least 2 GB RAM; 4 GB is comfortable.

### 8. Access the dashboard

```bash
juju status skyline    # note the unit IP address
```

Open `http://<UNIT_IP>:9999` in your browser and log in with any valid
OpenStack user credentials.

---

## Configuration Reference

| Key | Default | Description |
|---|---|---|
| `keystone-url` | *(required)* | Full Keystone v3 URL |
| `system-user-password` | *(required)* | Password of the `skyline` OS user |
| `database-url` | `""` | External DB URL; leave empty for local MariaDB |
| `database-password` | `""` | Local MariaDB password (auto-generated if empty) |
| `default-region` | `RegionOne` | OpenStack region |
| `system-user-name` | `skyline` | Name of the OS service user |
| `system-user-domain` | `admin_domain` | Domain of the service user |
| `system-project` | `admin` | Admin project name |
| `system-project-domain` | `admin_domain` | Domain of the admin project |
| `interface-type` | `public` | Endpoint interface (public/internal/admin) |
| `listen-port` | `9999` | nginx listener port |
| `debug` | `false` | Enable debug logging |
| `ssl-enabled` | `false` | Enable SSL flag in skyline.yaml |
| `secret-key` | `""` | Session key (auto-generated if empty) |
| `prometheus-endpoint` | `""` | Prometheus URL |
| `sso-enabled` | `false` | Enable SSO |
| `enforce-new-defaults` | `false` | New RBAC defaults |
| `reclaim-instance-interval` | `604800` | Deleted instance reclaim (seconds) |
| `apiserver-branch` | `stable/2024.2` | skyline-apiserver git branch |
| `console-branch` | `stable/2024.2` | skyline-console git branch |
| `gunicorn-workers` | `0` | Workers (0 = auto from cpu_count) |
| `gunicorn-timeout` | `300` | gunicorn worker timeout |

### Changing a config value after deploy

```bash
juju config skyline listen-port=8080
```

Juju fires `config-changed`, which re-renders all templates, runs `db_sync`,
and restarts/reloads services automatically.

---

## Actions

```bash
# Re-run Alembic migration manually
juju run-action skyline/0 db-sync --wait

# Show where nginx serves static files from
juju run-action skyline/0 get-static-path --wait

# Restart skyline-apiserver and reload nginx
juju run-action skyline/0 restart-services --wait

# Dump the rendered /etc/skyline/skyline.yaml (includes password — use carefully)
juju run-action skyline/0 show-config --wait
```

---

## Using an External Database

If you already have a managed MySQL/MariaDB (e.g. Percona XtraDB or
mysql-innodb-cluster), set `database-url` instead of letting the charm install
a local MariaDB:

```bash
juju config skyline database-url="mysql://skyline:PASS@10.0.0.5:3306/skyline"
```

When `database-url` is non-empty:
- MariaDB is **not** installed on the unit
- The charm skips the local DB creation step
- You are responsible for creating the `skyline` database and user externally

SQL to run on your external database server:
```sql
CREATE DATABASE IF NOT EXISTS skyline
  DEFAULT CHARACTER SET utf8 DEFAULT COLLATE utf8_general_ci;
GRANT ALL PRIVILEGES ON skyline.* TO 'skyline'@'%'
  IDENTIFIED BY 'YOUR_PASS';
FLUSH PRIVILEGES;
```

---

## Troubleshooting

### View charm logs

```bash
juju debug-log --include unit-skyline/0
```

### SSH into the unit

```bash
juju ssh skyline/0
```

### Check service status inside the unit

```bash
systemctl status skyline-apiserver nginx mariadb
journalctl -u skyline-apiserver -f
```

### gunicorn not listening on 28000

```bash
ss -tlnp | grep 28000
journalctl -u skyline-apiserver --no-pager -n 50
```

Common causes:
- `skyline.yaml` has wrong `database_url` (check MariaDB password)
- `OS_CONFIG_DIR` not set — verify the systemd unit was rendered correctly

### Browser shows 502 Bad Gateway

nginx is up but cannot reach gunicorn:
```bash
curl -s http://127.0.0.1:28000/api/openstack/skyline/version
systemctl status skyline-apiserver
```

### Login returns 401 Unauthorized

The `skyline` OpenStack user lacks the `admin` role:
```bash
openstack role add --project admin --user skyline admin
# optionally also:
openstack role add --user skyline --user-domain admin_domain --system all Admin
```

Also verify Keystone is reachable from inside the unit:
```bash
curl http://KEYSTONE_IP:5000/v3/
```

### Build runs out of memory

The Node.js build is memory-intensive. Add swap inside the unit:
```bash
fallocate -l 4G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
```

---

## How the Charm Operates Internally

### Event flow on first deploy

```
install
  ├─ apt-get: baseline + console build deps + mariadb
  ├─ python3 -m venv /opt/skyline-venv
  ├─ pip install skyline-apiserver (from git clone)
  ├─ nvm install + yarn + make package (skyline-console)
  ├─ pip install skyline_console-*.whl
  └─ discover + store static_path

config-changed  (fired automatically after install)
  ├─ validate keystone-url and system-user-password
  ├─ create local MariaDB db/user (if database-url is empty)
  ├─ render skyline.yaml, gunicorn.py, skyline-apiserver.service, nginx.conf
  ├─ systemctl daemon-reload
  ├─ make db_sync  (Alembic — idempotent)
  └─ enable + restart skyline-apiserver; test + reload nginx

start
  └─ confirm skyline-apiserver is active → set ActiveStatus
```

### Event flow on config-changed (subsequent)

Same as the `config-changed` step above — all operations are idempotent.

### Template rendering

All four config files are Jinja2 templates in `templates/`. The charm renders
them from `_template_context()` which maps every config option to a template
variable. After rendering, `systemctl daemon-reload` ensures systemd sees any
unit file changes, and `nginx -t` validates the nginx config before reload.

### Secret key persistence

The session `secret_key` is generated once with `secrets.token_urlsafe(32)` and
stored in `ops.StoredState`. It survives `config-changed` and `upgrade-charm`
events. To rotate the key, set a new value via `juju config skyline secret-key=...`
(this will invalidate all active sessions).
