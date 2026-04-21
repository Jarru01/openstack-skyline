# Skyline Dashboard Deployment Guides on OpenStack (MaaS + Juju)

Repository containing guides, configuration files, and notes addressing issues discovered while deploying **Skyline Dashboard** on a private OpenStack cloud managed via **MaaS** and **Juju**.
- Tested on a mini 2-node OpenStack cloud deployed via **MaaS + Juju** (Bobcat)
- Skyline supports **Prometheus**-fed monitoring dashboards - installation and integration guide included in files

## Issue01: Unacessible instance console
After installing skyline dashboard the console of instances was unavailable, when trying to connect to console it threw error: Unavailable console type novnc (400). Skyline apparently doesn't support spice that was configured on the underlying openstack by default.
### Solution: console access protocol in openstack had to be set to novnc.<br> 
```juju config nova-cloud-controller console-access-protocol="novnc" ```

## Issue02: Login to dashboard available only for skyline service user 
When attempting to log in, the system returns a 401 Unauthorized error (specifically HTTP 403) during the `identity:list_user_projects call`. The root cause is the legacy policy.json format, which does not support system_scope. Skyline sends a request with a project-scoped token, which Keystone rejects due to a domain_id mismatch.<br>
### Solution: Updating Access Policies<br>
The cleanest solution is to modify the policy configuration to allow any user with the Admin role to list projects for any user—standard behavior for an administrator.<br>
Open `/etc/keystone/policy.json` and update the rule:<br>
```
Original: "identity:list_user_projects": "rule:owner or rule:admin_and_matching_domain_id",
New: "identity:list_user_projects": "rule:owner or rule:cloud_admin or rule:admin_required",
```
The changes take effect immediately; a restart of the Keystone service is not required. Proceed to test the login in the Skyline interface as admin@admin_domain.

## Issue03: Admin dashboards are read-only
The most complex issue by far was that the admin panels in the skyline dashboards were read only and nothing was editable inside them, no option to create resources/edit them was shown in the UI.<br>

### What Was Tried and Didn't Work (Short Summary)
- Verified system-scoped token generation — worked correctly, token had system: all scope after granting the skyline user admin role on system:all in openstack
- Modified Keystone policy.json to allow identity:list_user_projects with system-scoped Admin role — partially helped login but not admin panel
- Set enforce_new_defaults: true in skyline.yaml — no effect
- Set enforce_new_defaults: false in skyline.yaml — no effect
- Added Admin to system_admin_roles in skyline.yaml — admin panel appeared accessible but all action buttons remained greyed out or missing
- Tried various system_scope: all configurations in skyline.yaml — no effect
- Compared Keystone policies between devstack (working) and Juju+MAAS deployment, patched multiple rules (policy file included in files) — no effect on admin panel functionality

### What fixed the issue<br>
* Setting `immutable` flag of Admin role to `false`
* Renaming role **A**dmin -> **a**dmin
* Change of the keystone admin role name `juju config keystone keystone-admin-role=admin`,`juju config keystone admin-role=admin`
* Changing role name Admin -> admin in `/etc/keystone/policy.json`
* Changing role name Admin -> admin in `nova.conf,neutron.conf`
* Restarting neutron service `sudo systemctl restart neutron-server.service`

### Why renaming role Admin → admin Fixed Everything

**What `system_admin_roles` in `skyline.yaml` actually does:**

This setting controls only one thing: whether Skyline's own frontend shows the **Administrator panel switcher** in the UI at all. It's a display gate — Skyline checks "does this user's token contain any role from this list?" and if yes, it renders the admin panel toggle. That's the full extent of its influence.

So when we had `Admin` (capital A) in `system_admin_roles`, the admin panel appeared and you could switch to it. That part worked. **But every button inside it was dead.**



#### Why the buttons were dead — the real enforcement chain

When you click anything in the Administrator panel, Skyline doesn't make the decision about whether you're allowed. It passes your token to the actual **OpenStack service APIs** — Nova, Neutron, Keystone, Cinder — and those services enforce their own policies to decide if the action is permitted.

Those service policies, both Bobcat's built-in `oslo.policy` defaults and the rules your Juju deployment inherited, are written entirely with lowercase `role:admin`. For example, in Nova:

* `"context_is_admin": "is_admin:1 or role:admin"`
* `"os_compute_api:servers:index:get_all_tenants": "rule:context_is_admin"`

`oslo.policy` role matching is **case-sensitive string comparison**. `role:admin` does not match a token carrying `role:Admin`. So every single policy check across every service evaluated to `False` for your token, regardless of what Skyline's yaml said. Skyline's `system_admin_roles` list had no bearing on this — it was never consulted by Nova, Neutron, or Keystone when they enforced their own rules.



#### Why the 400 Networking error happened after the rename

Once `admin` (lowercase) worked and the admin panel functioned, listing instances still failed because Nova internally calls Neutron to fetch port and network data for each instance. This is a **service-to-service call** — Nova authenticates to Neutron using its own service credentials from `nova.conf`.

The `[neutron]` section in `nova.conf` and neutron's `keystonemiddleware` config both still referenced the old `Admin` role name. Neutron's middleware checks that the Nova service token carries the service role and that the configured admin role matches. With the mismatch, Neutron returned `401` to Nova, and Nova translated that into the generic *"Networking client is experiencing an unauthorized exception."* response back to Skyline. Restarting `neutron-server` after fixing the role reference in both config files cleared it.

#### Why Horizon Worked With Admin (Capital A)
Horizon bypassed the role name entirely because it uses project-scoped tokens, which trigger a legacy is_admin flag that grants access before the role name is ever checked. Skyline uses system-scoped tokens by design, which never set that flag, forcing every policy check to fall through to an explicit case-sensitive role name comparison — where Admin silently failed against every rule written for admin.
# TODO:
* guide - juju charm for skyline
* guide - load balancing between multiple skyline instances (HAProxy)
