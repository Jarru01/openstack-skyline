. /home/test/scripts/admin-openrc.sh

juju ssh neutron-api/0 "sudo systemctl restart neutron-server.service"
sleep 5
# Confirm healthy
juju ssh neutron-api/0 "sudo systemctl status neutron-server.service"
# Expected: active (running)

juju config nova-cloud-controller console-access-protocol="novnc" 

juju status
echo "*****Wait for ALL units to reach active/idle and you're done.*****"
echo "*****If heat errors occur, refer the the documentation at docs.cc.uniza.sk for fixing them*****"