# OpenStack Admin Role Migration Guide
## Renaming `Admin` → `admin` on Juju+MAAS Bobcat (Production)

> **Context:** This guide documents the complete procedure required to rename the Keystone admin role from `Admin` (capital A, Juju default) to `admin` (lowercase) in a Juju+MAAS OpenStack Bobcat deployment, required for correct Skyline dashboard admin panel operation.

---

## Background

Juju's Keystone charm defaults to creating the admin role as `Admin` (capital A). OpenStack Bobcat's oslo.policy rules across all services are written for lowercase `role:admin`. This mismatch causes:

- Skyline admin panel buttons greyed out or missing
- `400 Networking client is experiencing an unauthorized exception` on instance/port listings
- System-scoped token operations silently denied across Nova, Neutron, Cinder, Glance

The root cause: `service_token_roles_required = True` in `[keystone_authtoken]` means each service validates that calling service tokens carry the configured admin role name. With `Admin` ≠ `admin`, all service-to-service calls fail with 401, which Nova surfaces as 400 to the client.

---

## Pre-flight Checks

Run these before starting. Save the outputs.

```bash
# Confirm current role name and ID
openstack role show Admin
ROLE_ID=$(openstack role show Admin -f value -c id)
echo "Role ID: $ROLE_ID"

# Confirm role assignments are intact
openstack role assignment list --role Admin --names | head -20

# Confirm Juju is healthy
juju status --relations | grep -E "error|blocked|waiting"
# Should return nothing

# Confirm current keystone charm config
juju config keystone keystone-admin-role
# Expected output: Admin
```

---

## Phase 1 — Manual OpenStack Role Rename

> These commands cannot be automated as they require deliberate operator action on the OpenStack role itself.

```bash
# Step 1: Get the role ID (if not already done above)
ROLE_ID=$(openstack role show Admin -f value -c id)

# Step 2: Remove the immutable flag (Bobcat sets this by default)
openstack role set --no-immutable $ROLE_ID

# Step 3: Rename the role
openstack role set --name admin $ROLE_ID

# Step 4: Verify
openstack role show admin
# Expected: shows role with name: admin

# Step 5: Confirm existing assignments still intact
openstack role assignment list --role admin --names | head -20
```

---

## Phase 2 — Juju Config Propagation

> A single command that automatically re-templates `[keystone_authtoken]` (inside x/x.conf) with `service_token_roles = admin` on every service connected via the `identity-service` relation.

**Covered automatically:** nova-cloud-controller, neutron-api, glance, placement, cinder, ceph-radosgw, gnocchi, manila, heat, openstack-dashboard

```bash
# Trigger propagation
juju config keystone keystone-admin-role=admin
#juju config keystone admin-role=admin - shouldn't be necessary
# Wait for ALL units to reach active/idle before proceeding to Phase 3
# Do not continue until this is clean
watch -n5 juju status

```

**Expected final state:** Every unit shows `active` workload status and `idle` agent status.

---

## Phase 3 — Keystone Policy Overrides (Permanent)

> Uses Juju's native `use-policyd-override` mechanism. Survives every future charm hook run and upgrade. Replaces any manual edits to `/etc/keystone/policy.json`.
changed policies:
### Fixes Admin → admin role rename. Redundant system_scope clause removed.
admin_required: "role:admin"

### Removes hardcoded domain/project UUIDs from old rule, works with system-scoped tokens.
cloud_admin: "rule:admin_required"

### Removes domain_id constraint that broke system-scoped token admin operations.
admin_or_owner: "rule:admin_required or rule:owner"

### Required for Skyline login — allows admin to list projects for any user.
### Redundant rule:cloud_admin clause removed (equals admin_required anyway).
identity:list_user_projects: "rule:owner or rule:admin_required"

### Security fix — was empty string in both old and new (anyone could delete trusts).
identity:delete_trust: "rule:admin_required or user_id:%(target.trust.trustor_user_id)s"

```bash
# Step 1: Create the overrides file
cat > keystone-overrides.yaml << 'EOF'
admin_required: "role:admin"
cloud_admin: "rule:admin_required"
admin_or_owner: "rule:admin_required or rule:owner"
identity:list_user_projects: "rule:owner or rule:admin_required"
identity:delete_trust: "rule:admin_required or user_id:%(target.trust.trustor_user_id)s"
EOF

# Step 2: Zip it (charm requires zip format)
zip keystone-policyd-override.zip keystone-overrides.yaml

# Step 3: Attach as Juju resource
juju attach-resource keystone policyd-override=keystone-policyd-override.zip

# Step 4: Enable the override
juju config keystone use-policyd-override=true

# Step 5: Verify charm applied it
juju ssh keystone/0 "sudo ls /etc/keystone/policy.d/"
# Expected: keystone-overrides.yaml listed

juju ssh keystone/0 "sudo cat /etc/keystone/policy.d/keystone-overrides.yaml"
# Expected: contents of your overrides file
```

---

## Phase 4 — Neutron Service Restart

> Required based on observed behaviour in test environment. The charm relation propagation updates config files but Neutron does not always reload automatically.

```bash
juju run --unit neutron-api/0 "sudo systemctl restart neutron-server"

# Confirm healthy
juju ssh neutron-api/0 "sudo systemctl status neutron-server.service"
# Expected: active (running)
```

---

## Phase 5 — Verification

### 5a — Check service_token_roles on all identity-service units

```bash
echo "=== Checking service_token_roles on identity-service units ==="

for unit in \
  nova-cloud-controller/0 \
  neutron-api/0 \
  glance/0 \
  placement/0 \
  cinder/0 \
  gnocchi/0 \
  heat/0; do
  SERVICE=$(echo $unit | cut -d/ -f1)
  echo -n "$unit: "
  juju ssh $unit \
    "sudo grep -r 'service_token_roles' /etc/${SERVICE}/ 2>/dev/null | grep -v Binary | head -2" \
    2>/dev/null || echo "SSH failed"
done
```

**Expected for each:** `service_token_roles = admin` (lowercase)

### 5b — Check identity-credentials units (production-only risk)

> `manila-ganesha` and `ceilometer` use the `identity-credentials` relation, which is a **different relation type** from `identity-service`. The `keystone-admin-role` propagation in Phase 2 may **not** have reached these services. Check manually.

```bash
echo "=== Checking identity-credentials units ==="

for unit in manila-ganesha/0 ceilometer/0; do
  echo "--- $unit ---"
  juju ssh $unit \
    "sudo grep -r 'service_token_roles' /etc/ 2>/dev/null | grep -v Binary" \
    2>/dev/null || echo "SSH failed or not found"
done
```

**If output shows `Admin` (capital A) or field is missing, apply manual fix:**

```bash
# Fix manila-ganesha if needed
juju ssh manila-ganesha/0 "
  sudo sed -i 's/service_token_roles = Admin/service_token_roles = admin/g' \
    /etc/manila/*.conf 2>/dev/null
  sudo systemctl restart manila-api 2>/dev/null || true
  echo Done
"

# Fix ceilometer if needed
juju ssh ceilometer/0 "
  sudo sed -i 's/service_token_roles = Admin/service_token_roles = admin/g' \
    /etc/ceilometer/*.conf 2>/dev/null
  sudo systemctl restart ceilometer-api 2>/dev/null || true
  echo Done
"
```

### 5c — End-to-end API verification

```bash
# 1. Confirm role rename
openstack role show admin

# 2. Confirm system-scoped token works
openstack --os-system-scope all token issue

# 3. Confirm Nova→Neutron call works (the exact call that was failing)
TOKEN=$(openstack token issue -f value -c id)
NOVA_URL=$(openstack endpoint list \
  --service compute --interface public -f value -c URL | head -1)

curl -sk \
  -H "X-Auth-Token: $TOKEN" \
  -H "X-OpenStack-Nova-API-Version: 2.79" \
  "${NOVA_URL}/servers/detail?all_tenants=True&limit=1" \
  | python3 -m json.tool | head -10

# Expected: {"servers": [...]} 
# NOT: "Networking client is experiencing an unauthorized exception"

# 4. Confirm keystone policy override active
juju ssh keystone/0 "sudo cat /etc/keystone/policy.d/keystone-overrides.yaml"

# 5. Confirm Keystone admin-role config
juju config keystone keystone-admin-role
# Expected: admin
```

### 5d — Skyline verification

1. Log into Skyline dashboard
2. Confirm login succeeds as `admin` user
3. Switch to **Administrator** panel
4. Navigate to **Compute → Instances** — list must load without 400 error
5. Navigate to **Network → Ports** — list must load
6. Confirm action buttons (Create, Edit, Delete) are present and active

---

## Summary Table

| Phase | What | Command/Action | Automated? |
|-------|------|---------------|------------|
| 1 | Remove immutable flag | `openstack role set --no-immutable` | ❌ Manual |
| 1 | Rename Admin→admin | `openstack role set --name admin` | ❌ Manual |
| 2 | Propagate to nova, neutron, glance, cinder, gnocchi, manila, heat, placement, radosgw, horizon | `juju config keystone keystone-admin-role=admin` | ✅ Automatic |
| 2 | Wait for settlement | `watch juju status` | ❌ Manual check |
| 3 | Keystone policy fixes | `use-policyd-override` | ✅ Permanent |
| 4 | Neutron restart | `juju run --unit neutron-api/0 ...` | ❌ Manual |
| 5a | Verify identity-service units | grep script | ❌ Verify |
| 5b | Verify manila-ganesha, ceilometer | grep + sed if needed | ⚠️ Verify first |
| 5c | API end-to-end test | curl + openstack CLI | ❌ Verify |
| 5d | Skyline UI test | Browser | ❌ Manual |

---

## Service Coverage Reference

### Automatically covered by Phase 2 (`identity-service` relation)

| Service | Charm Channel |
|---------|--------------|
| nova-cloud-controller | 2023.2/stable |
| neutron-api | 2023.2/stable |
| glance | 2023.2/stable |
| placement | 2023.2/stable |
| cinder | 2023.2/stable |
| ceph-radosgw | reef/stable |
| gnocchi | 2023.2/stable |
| manila | 2023.2/stable |
| heat | 2023.2/stable |
| openstack-dashboard | 2023.2/stable |

### Require manual verification (different relation type)

| Service | Relation Type | Risk |
|---------|--------------|------|
| manila-ganesha | `identity-credentials` | ⚠️ May not propagate |
| ceilometer | `identity-credentials` + `identity-notifications` | ⚠️ May not propagate |

### Not affected (no Keystone relation)

`ceph-mon`, `ceph-osd`, `ceph-fs`, `ceph-dashboard`, `rabbitmq-server`, `mysql-innodb-cluster`, `vault`, `ovn-central`, `ovn-chassis`, `neutron-api-plugin-ovn`, `memcached`, `ntp`, `ceilometer-agent`

---

## Rollback

If anything breaks during the process:

```bash
# Re-rename back (no immutable flag needed since we already removed it)
openstack role set --name Admin $(openstack role show admin -f value -c id)

# Revert Juju config
juju config keystone keystone-admin-role=Admin

# Wait for settlement
watch juju status

# Disable policy override
juju config keystone use-policyd-override=false
```

> **Note:** Rollback restores the original broken state for Skyline but restores Horizon functionality. Service-to-service calls will resume working as before since `service_token_roles` will be re-templated to `Admin` by the charm.

---

*Generated from troubleshooting session — Juju+MAAS OpenStack Bobcat (2023.2), Skyline dashboard `99cloud/skyline`*
