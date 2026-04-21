# Prometheus Monitoring Setup for OpenStack (Bobcat) with Skyline

## Overview

This guide sets up a Prometheus-based monitoring stack for an OpenStack Bobcat cloud deployed
with Juju + MAAS. Both the monitoring VM and Skyline dashboard are MAAS-provisioned machines on
the same physical network as the OpenStack nodes — no OpenStack security groups are involved
anywhere in this setup.

**What you will deploy:**

- **Monitoring VM** (new MAAS machine): runs Prometheus server + openstack-exporter + node_exporter
- **OpenStack nodes** (existing): each gets node_exporter installed on the physical host
- **Skyline** (existing MAAS machine): configured to point at the Prometheus server

**Architecture summary:**

```
Physical OpenStack Nodes (bare metal)
  └── node_exporter :9100  ─────────────────────────────┐
                                                          │ scrape
Juju LXD Containers                                      │
  ├── mysqld_exporter :9104 ─────────────────────────────┤
  ├── rabbitmq_prometheus :15692 ────────────────────────┤
  └── memcached_exporter :9150 ──────────────────────────┤
                                                          │
openstack-exporter :9180 ──► OpenStack APIs               │
  │ (on monitoring VM)                                    │
  │                                                 Prometheus :9090
  └──────────────────────────────────────────────────────┘
                                                          │
                              Recording Rules (mirror layer)
                                                          │
                                                   Skyline queries
                                                prometheus_endpoint
```

**What each Skyline monitoring tab needs and what provides it:**

| Skyline Tab | Data source | Status |
|---|---|---|
| Overview — CPU / RAM bars | Recording rules (node_exporter mirror) | ✅ Working |
| Overview — Storage bar | Recording rules (node_exporter → fake Ceph) | ✅ Working |
| Physical Nodes | node_exporter on physical hosts | ✅ Working |
| OpenStack Services | openstack-exporter (`agent_state` metrics) + dedicated exporters | ✅ Nova + Neutron + RabbitMQ/MySQL/Memcached |
| Storage Clusters | Real Ceph exporter | ⚠️ Empty — no Ceph in this setup |
| Other Services (RabbitMQ/MySQL/Memcached) | Dedicated exporters (RabbitMQ/MySQL/Memcached) | ✅ Working |

> **Why the Overview bars need a special approach:** Skyline's Overview page queries for specific
> metric names (`openstack_nova_vcpus_available`, `openstack_nova_memory_used_bytes`,
> `ceph_cluster_total_bytes`) that the openstack-exporter snap cannot reliably produce on Bobcat.
> Two compatibility issues block it: the snap only has access to per-tenant quota data (not
> hypervisor-level hardware data), and a Nova microversion incompatibility crashes the hypervisor
> collection loop in v1.4.0. The Storage bar is hardcoded to Ceph and shows nothing without it.
> The solution — documented in Part 7 — is a set of Prometheus Recording Rules that translate
> accurate physical hardware data from node_exporter into the exact metric names Skyline expects.

---

## Part 1 — Hardware specs for the monitoring VM

Request a new machine from MAAS. Disk is the main variable — Prometheus stores 30 days of
time-series data at 15-second scrape intervals.

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| vCPUs    | 2       | 4           |
| RAM      | 4 GB    | 8 GB        |
| Disk     | 100 GB  | 200 GB      |
| OS       | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| Network  | Same subnet as OpenStack management network | Same subnet |

Once MAAS has provisioned and deployed the machine, SSH in and proceed.

---

## Part 2 — Prepare the monitoring VM

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl wget tar snapd

# Create installation directories
sudo mkdir -p /opt/prometheus
sudo mkdir -p /opt/node_exporter

# Create dedicated system users (security best practice)
sudo useradd --no-create-home --shell /bin/false prometheus
sudo useradd --no-create-home --shell /bin/false node_exporter
```

---

## Part 3 — Install Prometheus server

Check https://prometheus.io/download/ for the current latest version. Replace `2.51.0` if a
newer version is available.

```bash
cd /tmp
PROM_VERSION="2.51.0"
wget https://github.com/prometheus/prometheus/releases/download/v${PROM_VERSION}/prometheus-${PROM_VERSION}.linux-amd64.tar.gz
tar xzf prometheus-${PROM_VERSION}.linux-amd64.tar.gz

sudo cp prometheus-${PROM_VERSION}.linux-amd64/prometheus /opt/prometheus/prometheus
sudo cp prometheus-${PROM_VERSION}.linux-amd64/promtool   /opt/prometheus/promtool
sudo mkdir -p /opt/prometheus/data
sudo mkdir -p /etc/prometheus
sudo cp prometheus-${PROM_VERSION}.linux-amd64/prometheus.yml /etc/prometheus/prometheus.yml

sudo chown -R prometheus:prometheus /opt/prometheus /etc/prometheus
```

Create the systemd service at `/etc/systemd/system/prometheus.service`:

```ini
[Unit]
Description=Prometheus Monitoring Server
After=network.target

[Service]
User=prometheus
Group=prometheus
Type=simple
ExecStart=/opt/prometheus/prometheus \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/opt/prometheus/data \
  --storage.tsdb.retention.time=30d \
  --web.listen-address=0.0.0.0:9090
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable prometheus
sudo systemctl start prometheus

# Verify
systemctl status prometheus
curl http://localhost:9090/-/healthy
```

---

## Part 4 — Install node_exporter on the monitoring VM

```bash
cd /tmp
NODE_VERSION="1.7.0"
wget https://github.com/prometheus/node_exporter/releases/download/v${NODE_VERSION}/node_exporter-${NODE_VERSION}.linux-amd64.tar.gz
tar xzf node_exporter-${NODE_VERSION}.linux-amd64.tar.gz

sudo cp node_exporter-${NODE_VERSION}.linux-amd64/node_exporter /opt/node_exporter/node_exporter
sudo chown -R node_exporter:node_exporter /opt/node_exporter
```

Create `/etc/systemd/system/node_exporter.service`:

```ini
[Unit]
Description=Node Exporter
After=network.target

[Service]
User=node_exporter
Group=node_exporter
Type=simple
ExecStart=/opt/node_exporter/node_exporter --web.listen-address=0.0.0.0:9100
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable node_exporter
sudo systemctl start node_exporter

# Verify
curl http://localhost:9100/metrics | head -5
```

---

## Part 5 — Install openstack-exporter

This component scrapes the OpenStack APIs and feeds Skyline's service status and per-instance
monitoring panels. It does **not** provide the data for the Overview bars — that comes from the
recording rules in Part 7.

### 5.1 Install via snap

```bash
sudo snap install --channel stable golang-openstack-exporter
```

Verify the exact binary name exposed by the snap — it differs from the snap package name:

```bash
sudo snap info golang-openstack-exporter | grep commands
# Output: commands: - golang-openstack-exporter.openstack-exporter
```

### 5.2 Create the clouds.yaml authentication file

> **Critical — AppArmor snap confinement:** The snap process runs under AppArmor and can only
> read files from within its own snap directory tree. Placing `clouds.yaml` anywhere else (such
> as `/var/prometheus-openstack/`) results in a silent permission denied and the exporter will
> fail to authenticate. The only path that reliably works is inside the snap's own data directory.

```bash
sudo mkdir -p /var/snap/golang-openstack-exporter/current/etc
sudo nano /var/snap/golang-openstack-exporter/current/etc/clouds.yaml
```

File contents — fill in your actual values:

```yaml
clouds:
  mycloud:
    auth:
      auth_url: https://KEYSTONE_IP:5000/v3
      username: admin
      password: YOUR_ADMIN_PASSWORD
      project_name: admin
      user_domain_name: admin_domain
      project_domain_name: admin_domain
    region_name: RegionOne
    interface: internal
    identity_api_version: 3
    verify: false
```

`verify: false` disables TLS certificate verification — required in Juju+MAAS because Keystone
uses a self-signed certificate. Find your Keystone IP with:

```bash
juju status keystone | grep public-address
# or
juju run --unit keystone/0 'unit-get private-address'
```

### 5.3 Create the systemd service

Create `/etc/systemd/system/openstack-exporter.service`:

```ini
[Unit]
Description=OpenStack Prometheus Exporter
After=network.target

[Service]
Type=simple
ExecStart=/snap/bin/golang-openstack-exporter.openstack-exporter \
  --os-client-config /var/snap/golang-openstack-exporter/current/etc/clouds.yaml \
  --web.listen-address 0.0.0.0:9180 \
  mycloud
Restart=on-failure
RestartSec=15s

[Install]
WantedBy=multi-user.target
```

> The binary is `/snap/bin/golang-openstack-exporter.openstack-exporter` — the snap name and the
> command name are different. Using just `/snap/bin/golang-openstack-exporter` will fail with
> command not found.

```bash
sudo systemctl daemon-reload
sudo systemctl enable openstack-exporter
sudo systemctl start openstack-exporter
```

Wait 30–60 seconds, then verify:

```bash
curl http://localhost:9180/metrics | grep "^openstack_" | head -10
```

You should see metrics like `openstack_nova_limits_instances_total`,
`openstack_neutron_networks_total`, `openstack_nova_agent_state`, etc.

**Known errors on Bobcat that are non-fatal:**

- `failed to collect metric: security_groups` — Nova's `/os-security-groups` endpoint was removed
  in newer OpenStack. This error is harmless; all other collection continues normally.
- `CPUInfo has unexpected type: <nil>` — A microversion incompatibility in snap v1.4.0 vs
  Bobcat's Nova API. This prevents hypervisor-level metrics from being collected, which is why
  the recording rules in Part 7 are necessary.

---

## Part 6 — Install node_exporter on every OpenStack physical node

**This step is required for both the Physical Nodes tab and the Overview bar recording rules.**

> **Juju+MAAS architecture note:** All OpenStack services run as LXD containers managed by Juju
> on top of physical MAAS nodes. Install node_exporter directly on the **physical MAAS host
> machines**, not inside Juju units or LXD containers. Installing inside a container gives
> container-level metrics, not real hardware metrics. The physical host IPs are the MAAS-assigned
> addresses — find them in the MAAS web UI, not from `juju status` which shows container IPs.

### 6.1 Install on each physical node

SSH directly to each physical host, then run:

```bash
cd /tmp
NODE_VERSION="1.7.0"
wget https://github.com/prometheus/node_exporter/releases/download/v${NODE_VERSION}/node_exporter-${NODE_VERSION}.linux-amd64.tar.gz
tar xzf node_exporter-${NODE_VERSION}.linux-amd64.tar.gz

sudo mkdir -p /opt/node_exporter
sudo cp node_exporter-${NODE_VERSION}.linux-amd64/node_exporter /opt/node_exporter/node_exporter
sudo useradd --no-create-home --shell /bin/false node_exporter 2>/dev/null || true
sudo chown -R node_exporter:node_exporter /opt/node_exporter
```

Create `/etc/systemd/system/node_exporter.service` on each node:

```ini
[Unit]
Description=Node Exporter
After=network.target

[Service]
User=node_exporter
Group=node_exporter
Type=simple
ExecStart=/opt/node_exporter/node_exporter --web.listen-address=0.0.0.0:9100
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable node_exporter
sudo systemctl start node_exporter
systemctl status node_exporter
```

### 6.2 Open port 9100 if UFW is active

```bash
# On each physical node — if UFW is active:
sudo ufw allow from MONITORING_VM_IP to any port 9100 proto tcp
sudo ufw reload
```

---

## Part 6.5 — Install Infrastructure Exporters (for "Other Services")

Skyline's "Other Services" tab requires specific metric names. We will install these inside your Juju LXD containers.

### 6.5.1 Enable RabbitMQ Monitoring

RabbitMQ 3.9 (your version) has built-in Prometheus support. You just need to enable the plugin on both units.

```bash
# Enable the plugin on both units
juju ssh rabbitmq-server/0 -- sudo rabbitmq-plugins enable rabbitmq_prometheus
juju ssh rabbitmq-server/1 -- sudo rabbitmq-plugins enable rabbitmq_prometheus
```

Note: RabbitMQ metrics will now be available on port 15692 (the default for the Prometheus plugin).

### 6.5.2 Install MySQL Exporter

You have a 3-node InnoDB cluster. You need to run the exporter on each node.

Create the monitoring user in MySQL:

Get the password (on your MAAS host):

```bash
export MYSQL_ROOT_PW=$(juju exec --unit mysql-innodb-cluster/2 leader-get mysql.passwd)
```

Create the user on the Cluster Primary (Unit 2):

```bash
juju exec --unit mysql-innodb-cluster/2 "mysql -u root -p$MYSQL_ROOT_PW -e \"CREATE USER 'exporter'@'localhost' IDENTIFIED BY 'SecretPass123'; GRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO 'exporter'@'localhost';\""
```

Install the exporter binary on all 3 units (0, 1, and 2):

Install the binary:

```bash
cd /tmp
wget https://github.com/prometheus/mysqld_exporter/releases/download/v0.18.0/mysqld_exporter-0.18.0.linux-amd64.tar.gz
tar xzf mysqld_exporter-0.18.0.linux-amd64.tar.gz
sudo cp mysqld_exporter-0.18.0.linux-amd64/mysqld_exporter /usr/local/bin/
```

Create the service:

```bash
sudo nano /etc/systemd/system/mysqld_exporter.service
```

Paste this:

```ini
[Unit]
Description=MySQL Exporter
After=network.target

[Service]
User=root
ExecStart=/usr/local/bin/mysqld_exporter --config.my-cnf=/etc/.mysqld_exporter.cnf
Restart=always

[Install]
WantedBy=multi-user.target
```

Inside your MySQL LXD container (e.g., juju-3e312a-1-lxd-2), create a hidden configuration file:

```bash
sudo nano /etc/.mysqld_exporter.cnf
```

Paste the following into that file (replace SecretPass123 with the actual password you created for the exporter user):

```ini
[client]
user=exporter
password=SecretPass123
socket=/var/run/mysqld/mysqld.sock
```

Start: ```bash
sudo systemctl daemon-reload && sudo systemctl enable --now mysqld_exporter
```

Verify it is working

```bash
sudo systemctl status mysqld_exporter
```

### 6.5.3 Install Memcached Exporter

On the Memcached unit (memcached/0):

SSH into the unit: ```bash
juju ssh memcached/0
```

Install binary:

```bash
cd /tmp
wget https://github.com/prometheus/memcached_exporter/releases/download/v0.16.0/memcached_exporter-0.16.0.linux-amd64.tar.gz
tar xzf memcached_exporter-0.16.0.linux-amd64.tar.gz
sudo cp memcached_exporter-0.16.0.linux-amd64/memcached_exporter /usr/local/bin/
```

Create the service: ```bash
sudo nano /etc/systemd/system/memcached_exporter.service
```

Paste this:

```ini
[Unit]
Description=Memcached Exporter
[Service]
ExecStart=/usr/local/bin/memcached_exporter
Restart=always
[Install]
WantedBy=multi-user.target
```

Start: ```bash
sudo systemctl daemon-reload && sudo systemctl enable --now memcached_exporter
```

---

## Part 7 — Create the Skyline Mirror Rules

This is the translation layer that makes Skyline's Overview bars work when using local disk
instead of Ceph, and when the openstack-exporter cannot access hypervisor-level Nova metrics.

**How it works:** Prometheus Recording Rules pre-compute new time series from existing ones.
Here, accurate physical hardware data from node_exporter is published under the exact metric
names Skyline's Overview page is hardcoded to query.

The three mappings:
- **CPU** → `openstack_nova_vcpus_available` / `openstack_nova_vcpus_used`
- **RAM** → `openstack_nova_memory_available_bytes` / `openstack_nova_memory_used_bytes`
- **Storage** → `ceph_cluster_total_bytes` / `ceph_cluster_total_used_bytes`
  (Skyline's storage bar is hardcoded to Ceph; mapping local disk into those names bypasses that)

### 7.1 Create the rules file

```bash
sudo nano /etc/prometheus/skyline_mapping.rules.yml
```

```yaml
groups:
  - name: skyline_hardware_mirror
    rules:

      # --- CPU MIRROR ---
      # Total physical cores across all compute nodes.
      # cluster="openstack" selects only OpenStack physical nodes, not the monitoring VM.
      - record: openstack_nova_vcpus_available
        expr: count(node_cpu_seconds_total{mode="idle", cluster="openstack"})

      # Used physical cores — total minus idle fraction over 5 minutes.
      # Result is fractional (e.g. 3.2 of 16 cores in active use).
      - record: openstack_nova_vcpus_used
        expr: >
          count(node_cpu_seconds_total{mode="idle", cluster="openstack"})
          - sum(rate(node_cpu_seconds_total{mode="idle", cluster="openstack"}[5m]))

      # --- RAM MIRROR ---
      # Total physical RAM in bytes across all compute nodes.
      - record: openstack_nova_memory_available_bytes
        expr: sum(node_memory_MemTotal_bytes{cluster="openstack"})

      # Used physical RAM in bytes.
      - record: openstack_nova_memory_used_bytes
        expr: >
          sum(node_memory_MemTotal_bytes{cluster="openstack"}
          - node_memory_MemAvailable_bytes{cluster="openstack"})

      # --- STORAGE MIRROR (Ceph spoof) ---
      # Skyline's storage overview bar queries for ceph_cluster_total_bytes.
      # We map root filesystem disk space from compute nodes into those metric names.
      # mountpoint="/" selects the root filesystem from all matching nodes.
      - record: ceph_cluster_total_bytes
        expr: sum(node_filesystem_size_bytes{mountpoint="/", cluster="openstack"})

      - record: ceph_cluster_total_used_bytes
        expr: >
          sum(node_filesystem_size_bytes{mountpoint="/", cluster="openstack"}
          - node_filesystem_free_bytes{mountpoint="/", cluster="openstack"})

      # Ceph health: 0 = HEALTH_OK. Required by some Skyline storage widgets.
      # Hardcoded to 0 since this is local disk, not a real Ceph cluster.
      - record: ceph_health_status
        expr: vector(0)
```

Set correct ownership and validate:

```bash
sudo chown prometheus:prometheus /etc/prometheus/skyline_mapping.rules.yml

/opt/prometheus/promtool check rules /etc/prometheus/skyline_mapping.rules.yml
# Expected: Checking ... SUCCESS
```

---

## Part 8 — Configure Prometheus scrape targets

Edit `/etc/prometheus/prometheus.yml`, replacing the entire file:

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

# Load the Skyline mirror rules created in Part 7
rule_files:
  - "skyline_mapping.rules.yml"

scrape_configs:

  # Prometheus monitoring itself
  - job_name: 'prometheus'
    static_configs:
      - targets: ['localhost:9090']

  # Monitoring VM system metrics
  - job_name: 'monitoring_vm'
    static_configs:
      - targets: ['localhost:9100']
        labels:
          instance: 'monitoring-server'
          role: 'monitoring'

  # OpenStack API metrics
  - job_name: 'openstack'
    scrape_interval: 60s
    scrape_timeout: 55s
    static_configs:
      - targets: ['localhost:9180']
        labels:
          instance: 'openstack-exporter'

  # OpenStack physical nodes (Compute Nodes)
  # Two labels here are critical:
  #   cluster="openstack"  — the recording rules in skyline_mapping.rules.yml
  #                          filter on this to select only these nodes
  #   node=""              — Skyline's frontend queries {node=""}. In PromQL this
  #                          matches series where the label is absent, which is
  #                          what node_exporter produces by default. Adding it
  #                          explicitly ensures Skyline's queries match.
  - job_name: 'openstack_compute'
    static_configs:
      - targets: ['10.11.0.21:9100']
        labels:
          instance: 'node1'
          role: 'compute'
          node_type: 'compute'
          cluster: 'openstack'
          region: 'RegionOne'
          node: ""
      - targets: ['10.11.0.22:9100']
        labels:
          instance: 'node2'
          role: 'compute'
          node_type: 'compute'
          cluster: 'openstack'
          region: 'RegionOne'
          node: ""

  # MySQL Cluster Nodes
  - job_name: 'mysql'
    static_configs:
      - targets: ['10.11.1.36:9104']
        labels:
          instance: 'mysql1'
      - targets: ['10.11.1.42:9104']
        labels:
          instance: 'mysql2'
      - targets: ['10.11.1.49:9104']
        labels:
          instance: 'mysql3'

  # RabbitMQ Message Queue Service
  - job_name: 'rabbitmq'
    metrics_path: /metrics
    static_configs:
      - targets: ['10.11.1.35:15692']
        labels:
          instance: 'rabbit1'
      - targets: ['10.11.1.38:15692']
        labels:
          instance: 'rabbit2'

  # Memcached Cache Service
  - job_name: 'memcached'
    static_configs:
      - targets: ['10.11.1.46:9150']
        labels:
          instance: 'memcache1'
```

Replace the IPs with your actual physical node addresses from MAAS. Apply:

```bash
sudo systemctl restart prometheus
```

### 8.1 Verify targets and recording rules

Wait 60 seconds, then check `http://MONITORING_VM_IP:9090/targets` — all jobs should show **UP**.

In the Prometheus expression browser (`/graph`), run these verification queries:

```promql
# Should return total physical cores (e.g. 16 for two 8-core nodes)
openstack_nova_vcpus_available

# Should return total RAM in bytes (~34GB for 2x 16GB nodes)
openstack_nova_memory_available_bytes

# Should return total disk size in bytes
ceph_cluster_total_bytes

# Should show your compute nodes with correct labels
node_memory_MemTotal_bytes{cluster="openstack"}
```

If the recording rule metrics return empty but `node_memory_MemTotal_bytes` returns data without
the `cluster` label, the label is missing from the `openstack_compute` job in prometheus.yml.

---

## Part 9 — Open firewall on monitoring VM (if UFW is active)

```bash
sudo ufw status
# If active:
sudo ufw allow from SKYLINE_IP to any port 9090 proto tcp
sudo ufw reload

# Verify from the Skyline machine:
curl http://MONITORING_VM_IP:9090/-/healthy
# Expected: Prometheus Server is Healthy.
```

---

## Part 10 — Configure Skyline

### 10.1 Locate and edit skyline.yaml

```bash
juju ssh skyline/0

sudo find / -name skyline.yaml 2>/dev/null
# Most likely: /etc/skyline/skyline.yaml

sudo nano /etc/skyline/skyline.yaml
```

### 10.2 Set the Prometheus endpoint

Find these lines in the `default:` section:

```yaml
default:
  prometheus_endpoint: http://MONITORING_VM_IP:9090   # port MUST be 9090, not 9091
  prometheus_enable_basic_auth: false
  prometheus_basic_auth_user: ''
  prometheus_basic_auth_password: ''
```

The sample file default is `http://localhost:9091` — `9091` is a placeholder. Your Prometheus
listens on **9090**.

Also verify the `secret_key` is not the upstream sample default:

```yaml
# Generate a new one if needed:
#   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
secret_key: YOUR_UNIQUE_SECRET_KEY_HERE
```

### 10.3 Restart Skyline

```bash
sudo systemctl list-units | grep skyline
# Restart whichever are present:
sudo systemctl restart skyline
sudo systemctl restart skyline-nginx
sudo systemctl restart skyline-gunicorn
sudo systemctl restart skyline-apiserver
```

Or via Juju charm config if the option is exposed:

```bash
juju config skyline | grep -i prometheus
juju config skyline prometheus-endpoint="http://MONITORING_VM_IP:9090"
```

### 10.4 Expected state of Skyline monitoring tabs

After completing this guide:

- **Overview** — CPU%, RAM%, and Storage% bars populated from physical hardware via recording rules
- **Physical Nodes** — Per-node CPU, RAM, disk, and network charts from node_exporter
- **OpenStack Services** — Nova, Neutron, RabbitMQ, MySQL, Memcached service health from exporters
- **Storage Clusters** — Empty. Requires a real Ceph cluster with ceph-exporter; not applicable
  to local-disk deployments
- **Other Services** (RabbitMQ, MySQL, Memcached) — Service metrics from dedicated exporters

---

## Part 11 — Troubleshooting reference

### Prometheus not starting

```bash
sudo journalctl -u prometheus -xe
/opt/prometheus/promtool check config /etc/prometheus/prometheus.yml
/opt/prometheus/promtool check rules /etc/prometheus/skyline_mapping.rules.yml
```

### openstack-exporter fails on startup (AppArmor / clouds.yaml not found)

The snap can only read from its own directory:

```bash
# Correct path:
/var/snap/golang-openstack-exporter/current/etc/clouds.yaml

# Verify the service uses it:
sudo systemctl cat openstack-exporter | grep os-client-config

# Check logs:
sudo journalctl -u openstack-exporter -n 30 --no-pager
```

If Keystone authentication fails:

```bash
# Test Keystone is reachable:
curl -sk https://KEYSTONE_IP:5000/v3 | python3 -m json.tool | head -5
```

Note: Juju+MAAS deployments run Keystone behind Apache with TLS. Use `https://`, not `http://`.

### Overview bars show 0% despite node_exporter working

Verify the recording rules are producing output:

```bash
# Prometheus expression browser:
openstack_nova_vcpus_available
ceph_cluster_total_bytes
```

If empty, the `cluster="openstack"` label is likely missing. Check:

```bash
curl -s 'http://localhost:9090/api/v1/query?query=node_memory_MemTotal_bytes{cluster="openstack"}' \
  | python3 -m json.tool | grep -c '"metric"'
# Should return 2 (one per physical node)
```

If this returns 0, add `cluster: 'openstack'` to the `openstack_compute` job in prometheus.yml.

### Overview storage bar shows 0%

Verify `mountpoint="/"` exists on your nodes:

```bash
curl -s http://NODE_IP:9100/metrics | grep 'node_filesystem_size_bytes{' | grep 'mountpoint="/"'
```

If empty, your root filesystem uses a different mountpoint or device path. Adjust the
`mountpoint="/"` filter in `skyline_mapping.rules.yml` to match.

### Overview numbers are wrong (e.g. double the actual RAM)

Check the monitoring VM is not accidentally included in the compute aggregate. The monitoring VM's
`monitoring_vm` job must not have `cluster: 'openstack'`. Only the `openstack_compute` job should
have that label.

### A specific OpenStack node target is DOWN

```bash
# Test from monitoring VM:
curl http://NODE_IP:9100/metrics | head -5

# On the node itself:
systemctl status node_exporter
sudo ufw status
```

### Skyline dashboards empty despite Prometheus being healthy

```bash
# From inside the skyline unit:
curl "http://MONITORING_VM_IP:9090/api/v1/query?query=up"
# Expected: JSON with status "success"

sudo tail -f /var/log/skyline/skyline.log
```

---

## Port reference

| Port | Service | Where it runs | Who connects |
|------|---------|---------------|--------------|
| 9090 | Prometheus server | Monitoring VM | Skyline, your browser |
| 9100 | node_exporter | Monitoring VM + each physical OpenStack node | Prometheus |
| 9104 | mysqld_exporter | MySQL LXD containers | Prometheus |
| 9150 | memcached_exporter | Memcached LXD container | Prometheus |
| 9180 | openstack-exporter (snap) | Monitoring VM | Prometheus |
| 15692 | rabbitmq_prometheus plugin | RabbitMQ LXD containers | Prometheus |