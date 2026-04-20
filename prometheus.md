# Prometheus Monitoring Setup for OpenStack (Bobcat) with Skyline

## Overview

This guide sets up a Prometheus-based monitoring stack for an OpenStack Bobcat cloud deployed with Juju + MAAS. Both the monitoring VM and Skyline dashboard are MAAS-provisioned machines on the same physical network as the OpenStack nodes — no OpenStack security groups are involved anywhere in this setup.

**What you will deploy:**

- **Monitoring VM** (new MAAS machine): runs Prometheus server + openstack-exporter + node_exporter
- **OpenStack nodes** (existing): each gets node_exporter installed so Skyline can show physical infrastructure data
- **Skyline** (existing MAAS machine): configured to point at the Prometheus server

**Architecture summary:**

```
OpenStack Nodes (controller, compute)
  └── node_exporter :9100  ──────────────────────────┐
                                                      │ scrape
openstack-exporter :9180 ──► OpenStack APIs           │
  │ (on monitoring VM)                                │
  │                                              Prometheus :9090
  └──────────────────────────────────────────────────┘
                                                      │
                                               Skyline queries
                                             prometheus_endpoint
```

---

## Part 1 — Hardware specs for the monitoring VM

Request a new machine from MAAS with the following specs. Disk is the main variable — Prometheus stores 30 days of time-series data by default at 15-second scrape intervals.

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

Check https://prometheus.io/download/ for the current latest version before running. Replace `2.51.0` below if a newer version is available.

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

This lets Prometheus monitor the monitoring VM itself (CPU, RAM, disk).

```bash
cd /tmp
NODE_VERSION="1.11.1"
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

This is the component that scrapes all OpenStack APIs (Nova, Neutron, Cinder, Glance, Keystone, Heat) and makes the data available to Prometheus. It also feeds Skyline's OpenStack-specific monitoring panels.

### 5.1 Install via snap

```bash
sudo snap install --channel stable golang-openstack-exporter
```

### 5.2 Create the clouds.yaml authentication file

The exporter uses OpenStack's standard `clouds.yaml` format. Place it in `/var/` to avoid AppArmor blocking access (a known issue with snap confinement):

```bash
sudo mkdir -p /var/prometheus-openstack
sudo nano /var/prometheus-openstack/clouds.yaml
```

File contents — fill in your actual values:

```yaml
clouds:
  mycloud:
    auth:
      auth_url: http://KEYSTONE_IP:5000/v3
      username: admin
      password: YOUR_ADMIN_PASSWORD
      project_name: admin
      user_domain_name: Default
      project_domain_name: Default
    region_name: RegionOne
    interface: internal
    identity_api_version: 3
    verify: false
```

**Finding your Keystone IP** — on the machine running Juju:

```bash
juju status keystone | grep public-address
# or
juju run --unit keystone/0 'unit-get private-address'
```

Use `interface: internal` if Prometheus is on the management/internal network (which it is in a MAAS deployment). Change to `public` only if you need to reach external endpoints.

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
# --os-client-config /var/prometheus-openstack/clouds.yaml \ - snap nevidi
  --web.listen-address 0.0.0.0:9180 \
  mycloud
Restart=on-failure
RestartSec=15s

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable openstack-exporter
sudo systemctl start openstack-exporter
```

Wait 30–60 seconds for the first scrape to complete, then verify:

```bash
curl http://localhost:9180/metrics | grep "^openstack_" | head -10
```

You should see metrics like `openstack_nova_instances_total`, `openstack_neutron_networks_total`, etc. If you get an auth error, recheck `clouds.yaml` credentials and the `auth_url`.

---

## Part 6 — Install node_exporter on every OpenStack node

**This step is required for Skyline's "Physical Nodes" monitoring dashboards to show data.** Skyline queries Prometheus for hardware metrics (CPU, RAM, disk) of your OpenStack compute and controller nodes. Those metrics only exist if node_exporter is running on each node.

Since your OpenStack nodes are MAAS machines running Ubuntu, this process is the same on each one. Run these commands on every compute and controller node.

### 6.1 Find your OpenStack node IPs

```bash
# On the Juju client machine:
juju status nova-compute
juju status nova-cloud-controller
# Note the IP address of each unit
```

### 6.2 Install node_exporter on each node

SSH into each node (via `juju ssh` or directly), then run:

```bash
# On each OpenStack node:
cd /tmp
NODE_VERSION="1.11.1"
wget https://github.com/prometheus/node_exporter/releases/download/v${NODE_VERSION}/node_exporter-${NODE_VERSION}.linux-amd64.tar.gz
tar xzf node_exporter-${NODE_VERSION}.linux-amd64.tar.gz

sudo mkdir -p /opt/node_exporter
sudo cp node_exporter-${NODE_VERSION}.linux-amd64/node_exporter /opt/node_exporter/node_exporter
sudo useradd --no-create-home --shell /bin/false node_exporter 2>/dev/null || true
sudo chown -R node_exporter:node_exporter /opt/node_exporter
```

Create `/etc/systemd/system/node_exporter.service` on each node (same content as in Part 4):

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

Using Juju to run on all compute nodes at once:

```bash
# On the Juju client:
juju exec --application nova-compute \
  'cd /tmp && \
   wget -q https://github.com/prometheus/node_exporter/releases/download/v1.7.0/node_exporter-1.7.0.linux-amd64.tar.gz && \
   tar xzf node_exporter-1.7.0.linux-amd64.tar.gz && \
   sudo mkdir -p /opt/node_exporter && \
   sudo cp node_exporter-1.7.0.linux-amd64/node_exporter /opt/node_exporter/ && \
   sudo useradd --no-create-home --shell /bin/false node_exporter 2>/dev/null; \
   sudo chown -R node_exporter:node_exporter /opt/node_exporter'
```

Then create the service file and enable it on each unit individually.

### 6.3 Open port 9100 on OpenStack nodes (if UFW is active)

Your OpenStack nodes are MAAS bare-metal machines — no OpenStack security groups apply. Check whether UFW is running:

```bash
# On each OpenStack node:
sudo ufw status
```

If active, allow Prometheus to scrape from the monitoring VM:

```bash
# Replace with your monitoring VM's actual IP:
sudo ufw allow from MONITORING_VM_IP to any port 9100 proto tcp
sudo ufw reload
```

---

## Part 7 — Configure Prometheus scrape targets

Now configure Prometheus to collect from all the exporters. Edit `/etc/prometheus/prometheus.yml` on the monitoring VM, replacing the entire file:

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

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
          role: 'monitoring'

  # OpenStack API metrics
  # Longer interval — API calls can take 20-60 seconds
  - job_name: 'openstack'
    scrape_interval: 60s
    scrape_timeout: 55s
    static_configs:
      - targets: ['localhost:9180']

  # OpenStack physical nodes — controller(s)
  - job_name: 'openstack_controllers'
    static_configs:
      - targets:
          - '192.168.1.10:9100'    # nova-cloud-controller/0 — replace with real IP
        labels:
          role: 'controller'

  # OpenStack physical nodes — compute nodes
  - job_name: 'openstack_compute'
    static_configs:
      - targets:
          - '192.168.1.11:9100'    # nova-compute/0 — replace with real IP
          - '192.168.1.12:9100'    # nova-compute/1 — add more as needed
        labels:
          role: 'compute'
```

**Find the correct IPs** from `juju status` and replace the example addresses above. Add or remove target lines to match your actual node count.

Apply the new configuration:

```bash
sudo systemctl restart prometheus
```

### 7.1 Verify all targets are healthy

Wait about 60 seconds, then open the Prometheus web UI:

```
http://MONITORING_VM_IP:9090/targets
```

Every job should show status **UP**. If any target shows **DOWN**, the error message next to it will indicate whether it is a connection issue (wrong IP/port or firewall) or an authentication issue (openstack-exporter credentials).

---

## Part 8 — Open firewall on monitoring VM (if UFW is active)

Skyline needs to reach Prometheus on port 9090. Since both are MAAS machines on the same network, check whether UFW is running on the monitoring VM:

```bash
sudo ufw status
```

If active, allow Skyline to reach Prometheus:

```bash
# Replace with your Skyline machine's actual IP:
sudo ufw allow from SKYLINE_IP to any port 9090 proto tcp
sudo ufw reload
```

Verify connectivity from the Skyline machine:

```bash
curl http://MONITORING_VM_IP:9090/-/healthy
# Expected response: Prometheus Server is Healthy.
```

---

## Part 9 — Configure Skyline

### 9.1 Locate and edit skyline.yaml

Skyline deployed via Juju runs inside an LXC container on its MAAS node. SSH in and find the config file:

```bash
juju ssh skyline/0

# Inside the unit, find the config:
sudo find / -name skyline.yaml 2>/dev/null
# Most likely location: /etc/skyline/skyline.yaml
```

Edit the file:

```bash
sudo nano /etc/skyline/skyline.yaml
```

### 9.2 The Prometheus-related settings

Find these four lines in the `default:` section and update them:

```yaml
default:
  # ... other settings above ...

  prometheus_endpoint: http://MONITORING_VM_IP:9090   # ← set your monitoring VM's IP; port MUST be 9090
  prometheus_enable_basic_auth: false
  prometheus_basic_auth_user: ''
  prometheus_basic_auth_password: ''
```

**Important:** The sample file shows the default as `http://localhost:9091` — the port `9091` is just a placeholder from the upstream sample. Your Prometheus runs on **9090**. Make sure the port in this line matches what Prometheus actually listens on.

While you have the file open, also verify the `secret_key` is not the sample default:

```yaml
# This should NOT be the default value from the sample file.
# If it is, generate a new one:
#   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
secret_key: YOUR_UNIQUE_SECRET_KEY_HERE
```

### 9.3 Restart Skyline to apply changes

```bash
# Check which services are running:
sudo systemctl list-units | grep skyline

# Restart whichever ones are present (commonly one or more of):
sudo systemctl restart skyline
sudo systemctl restart skyline-nginx
sudo systemctl restart skyline-gunicorn
sudo systemctl restart skyline-apiserver
```

If none of the above names match, find the actual service name:

```bash
sudo systemctl list-units --type=service | grep -i sky
```

Alternatively, if Skyline exposes a charm config option for the Prometheus endpoint, you can set it from the Juju client without touching the file:

```bash
# Check first whether the charm exposes this option:
juju config skyline | grep -i prometheus

# If it does:
juju config skyline prometheus-endpoint="http://MONITORING_VM_IP:9090"
```

### 9.4 Verify Skyline is using Prometheus

Log in to Skyline and navigate to the monitoring section. The dashboards should begin populating within one to two scrape cycles (up to 2 minutes for the first data). If the monitoring panels remain empty after a few minutes, check Skyline's logs:

```bash
# Inside the skyline unit:
sudo journalctl -u skyline -f
# or:
sudo tail -f /var/log/skyline/skyline.log
```

Common errors at this stage are a wrong IP in `prometheus_endpoint`, or port 9090 not reachable from the Skyline machine (see Part 8).

---

## Part 10 — Troubleshooting reference

Work through this in order if data is missing.

### Prometheus not starting

```bash
sudo journalctl -u prometheus -xe
# Also validate the config file:
/opt/prometheus/promtool check config /etc/prometheus/prometheus.yml
```

### openstack-exporter authentication errors

```bash
sudo journalctl -u openstack-exporter -f
# Test the connection manually:
curl http://localhost:9180/metrics 2>&1 | head -20
```

Check that `auth_url` in `clouds.yaml` is reachable from the monitoring VM:

```bash
curl http://KEYSTONE_IP:5000/v3
# Expected: JSON response with {"version": {...}}
```

### A specific OpenStack node target is DOWN

From the monitoring VM:

```bash
# Test direct connectivity:
curl http://NODE_IP:9100/metrics | head -5
```

If this times out, either node_exporter is not running on that node, or UFW is blocking port 9100. SSH to the node and check:

```bash
systemctl status node_exporter
sudo ufw status
```

### Skyline dashboards empty despite Prometheus being healthy

Confirm Skyline can reach Prometheus:

```bash
# From inside the skyline/0 unit:
curl http://MONITORING_VM_IP:9090/api/v1/query?query=up
# Expected: JSON with status "success"
```

If this fails, the monitoring VM's firewall is blocking port 9090 from the Skyline machine (see Part 8).

---

## Port reference

| Port | Service | Where it runs | Who connects |
|------|---------|---------------|--------------|
| 9090 | Prometheus server | Monitoring VM | Skyline, your browser |
| 9100 | node_exporter | Monitoring VM + each OpenStack node | Prometheus |
| 9180 | openstack-exporter | Monitoring VM | Prometheus |


---

## Part 11 — Overview recovery fix

The original OpenStack exporter and Skyline overview were not reliable as the source of truth for this deployment. CPU and RAM were being inferred from OpenStack-facing data that did not reflect the physical nodes correctly, while Skyline’s storage overview expected Ceph metrics even though the cloud uses local disk.

The fix was to mirror the accurate physical-node metrics into the exact metric names Skyline expects.

### 11.1 Recording rules for Skyline mapping

Create `/etc/prometheus/skyline_mapping.rules.yml` on the monitoring VM:

```yaml
groups:
  - name: skyline_hardware_mirror
    rules:
      # --- CPU MIRROR ---
      # Total Physical Cores
      - record: openstack_nova_vcpus_available
        expr: count(node_cpu_seconds_total{mode="idle", cluster="openstack"})
      # Used Physical Cores (calculated from load)
      - record: openstack_nova_vcpus_used
        expr: count(node_cpu_seconds_total{mode="idle", cluster="openstack"}) - sum(rate(node_cpu_seconds_total{mode="idle", cluster="openstack"}[5m]))
      # --- RAM MIRROR ---
      # Total Physical RAM (converted to bytes)
      - record: openstack_nova_memory_available_bytes
        expr: sum(node_memory_MemTotal_bytes{cluster="openstack"})
      # Used Physical RAM
      - record: openstack_nova_memory_used_bytes
        expr: sum(node_memory_MemTotal_bytes{cluster="openstack"} - node_memory_MemAvailable_bytes{cluster="openstack"})
      # CEPH MIRROR
      # Fake Ceph Total Bytes using Node Exporter local disk data
      - record: ceph_cluster_total_bytes
        expr: sum(node_filesystem_size_bytes{mountpoint="/", cluster="openstack"})
      # Fake Ceph Used Bytes
      - record: ceph_cluster_total_used_bytes
        expr: sum(node_filesystem_size_bytes{mountpoint="/", cluster="openstack"} - node_filesystem_free_bytes{mountpoint="/", cluster="openstack"})
      # Fake Ceph Health (Skyline likes to see this too)
      #- record: ceph_health_status
      #  expr: vector(1)
```

### 11.2 Update Prometheus to load the mapping rules

Edit `/etc/prometheus/prometheus.yml` and ensure the rule file is loaded:

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

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
          role: 'monitoring'

  # OpenStack API metrics
  # Longer interval — API calls can take 20-60 seconds
  - job_name: 'openstack'
    scrape_interval: 60s
    scrape_timeout: 55s
    static_configs:
      - targets: ['localhost:9180']

  # OpenStack physical nodes — compute nodes
  - job_name: 'openstack_compute'
    static_configs:
      - targets:
          - '10.11.0.21:9100'
          - '10.11.0.22:9100'
        labels:
          role: 'compute'
          node_type: 'compute'
          cluster: 'openstack'
          region: 'RegionOne'
          node: ""

rule_files:
  - "skyline_mapping.rules.yml"
```

### 11.3 Why this fixes Skyline

Skyline overview widgets query for a small set of specific metric names. Rather than trying to force the OpenStack API exporter to expose the physical layer correctly, the recording rules translate the already-correct Node Exporter metrics into the metric names Skyline expects. This makes the overview reflect the real hardware: physical CPU cores, physical RAM, and local-disk capacity presented in a Ceph-like form.

### 11.4 Result

After reloading Prometheus, the overview should no longer show 0/0 values. CPU and RAM should match the real compute nodes, and the storage bar should reflect the local disk capacity mapped through the Ceph-style metric names.
