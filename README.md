# Skyline Dashboard Deployment Guides on OpenStack (MaaS + Juju)

Repository containing guides, configuration files, and notes addressing issues discovered while deploying **Skyline Dashboard** on a private OpenStack cloud managed via **MaaS** and **Juju**.
- Tested on a mini 2-node OpenStack cloud deployed via **MaaS + Juju** (Bobcat)
- Skyline supports **Prometheus**-fed monitoring dashboards - installation and integration guide included in files
# WIP - just a rough base
* error - unavailable console type novnc
* error - login to dashbaord viable only through skyline service user 
* error - admin dashboards are read-only (system scoped token/policies)
* guide - prometheus integration
* guide - creation of skyline charm for juju
* guide - load balancing between multiple skyline instances
