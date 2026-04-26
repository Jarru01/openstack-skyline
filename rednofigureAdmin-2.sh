. /home/test/scripts/admin-openrc.sh


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
juju ssh keystone/0 "sudo ls /etc/keystone/policy.d/"
# Expected: keystone-overrides.yaml listed

juju ssh keystone/0 "sudo cat /etc/keystone/policy.d/keystone-overrides.yaml"
# Expected: contents of your overrides file


juju run --unit neutron-api/0 "sudo systemctl restart neutron-server.service"

# Confirm healthy
juju ssh neutron-api/0 "sudo systemctl status neutron-server.service"
# Expected: active (running)
