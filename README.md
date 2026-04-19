# Repository containing guides, configuration files and notes adressing the issues discovered while deploying skyline dashboard on a private openstack cloud managed via MaaS and Juju.
## Deployment of skyline dashboard was tested on a small 2-node openstack cloud deployed via MaaS+Juju, version bobcat.
## Skyline also supports monitoring via prometheus, guide on installation and integration with skyline is in the files.
### WIP - just a rough base
* not going through skyline deployment, skyline.yaml shows current configuration
* error - unavailable console type novnc
* error - login to dashbaord viable only through skyline service user 
* error - admin dashboards are read-only (system scoped token/policies)
