. /home/test/scripts/admin-openrc.sh

juju config nova-cloud-controller console-access-protocol="novnc" 

juju status
echo "*****Wait for ALL units to reach active/idle and you're done.*****"
echo "*****If errors related to HEAT occur, refer the the documentation at docs.cc.uniza.sk for fixing them (heat internal error)*****"