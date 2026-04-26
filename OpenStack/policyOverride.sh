. /home/test/scripts/admin-openrc.sh

juju ssh neutron-api/0 "sudo systemctl restart neutron-server.service"
sleep 5
# Confirm healthy
juju ssh neutron-api/0 "sudo systemctl status neutron-server.service"
# Expected: active (running)

# Step 1: Create the overrides file
cat > keystone-overrides.yaml << 'EOF'
admin_required: "role:admin"
cloud_admin: "rule:admin_required"
admin_or_owner: "rule:admin_required or rule:owner"
identity:list_user_projects: "rule:owner or rule:admin_required"
identity:delete_trust: "rule:admin_required or user_id:%(target.trust.trustor_user_id)s"
EOF

# Step 2: Zip it (charm requires zip format)
sudo apt install zip -y
zip keystone-policyd-override.zip keystone-overrides.yaml

# Step 3: Attach as Juju resource
juju attach-resource keystone policyd-override=keystone-policyd-override.zip

# Step 4: Enable the override
juju config keystone use-policyd-override=true

# Step 5: Verify charm applied it
#juju ssh keystone/0 "sudo ls /etc/keystone/policy.d/"
# Expected: keystone-overrides.yaml listed

#juju ssh keystone/0 "sudo cat /etc/keystone/policy.d/keystone-overrides.yaml"
# Expected: contents of your overrides file

# Wait for ALL units to reach active/idle before proceeding to Phase 3
# Do not continue until this is clean (takes a lot of time to propagate)
juju status
echo "*****Wait for ALL units to reach active/idle before proceeding to Phase 3 - consoleProtocol.sh*****"
echo "*****If errors related to HEAT occur, refer the the documentation at docs.cc.uniza.sk for fixing them (heat internal error)*****"