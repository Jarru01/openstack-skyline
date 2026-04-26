. /home/test/scripts/admin-openrc.sh

# Step 1: Get the role ID (if not already done above)
ROLE_ID=$(openstack role show Admin -f value -c id)
echo "Role ID: $ROLE_ID"
# Step 2: Remove the immutable flag (Bobcat sets this by default)
openstack role set --no-immutable $ROLE_ID

# Step 3: Rename the role
openstack role set --name admin $ROLE_ID

openstack role set --immutable $ROLE_ID

# Step 4: Verify
openstack role show admin
# Expected: shows role with name: admin

# Step 5: Confirm existing assignments still intact
openstack role assignment list --role admin --names | head -20


# Trigger propagation
juju config keystone admin-role=admin
juju config keystone keystone-admin-role=admin

# Optional: Nudge other services to pick up the change (if they cache role info)
#TARGET_APPS="nova-cloud-controller neutron-api glance placement heat barbican designate octavia openstack-dashboard"
#for app in $TARGET_APPS; do
#    echo "--- Nudging $app ---"
#    juju config $app debug=true
#    # Give Juju a moment to trigger the hook
#    juju config $app debug=false
#done

# Wait for ALL units to reach active/idle before proceeding to Phase 2
# Do not continue until this is clean (takes a lot of time to propagate)
juju status
echo "*****Wait for ALL units to reach active/idle before proceeding to Phase 2 - policyOverride.sh*****"
echo "*****If errors related to HEAT occur, refer the the documentation at docs.cc.uniza.sk for fixing them (heat internal error)*****"