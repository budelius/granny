# TODO

## Datacenter / On-Prem Infrastructure Assessment Support

Goal: add a Granny workflow for TouristMobile datacenter architecture discovery in **strict read-only mode**, including supporting BunnyCDN SPA delivery, Hetzner servers without customer data, and cloud backup evidence.

### Requirements

- Add an `onprem` or `inventory` command group for non-cloud infrastructure discovery.
- Default to read-only behavior; no command may modify infrastructure state.
- Provide an explicit `--strict-read-only` mode for all datacenter/on-prem discovery commands.
- Commands must fail closed if a requested operation is not known to be read-only.
- Never collect or print secrets, private keys, API tokens, kubeconfig credentials, password hashes, or Kubernetes Secret values.
- Output should be suitable for compliance evidence and architecture assessment.
- Prefer JSON/Markdown output for easy inclusion in compliance repositories.

### Candidate Commands

```shell
granny onprem datacenter checklist
granny onprem datacenter k8s-inventory --strict-read-only --output ./evidence/kubernetes.json
granny onprem datacenter vmware-inventory --strict-read-only --output ./evidence/vmware.json
granny onprem datacenter kvm-inventory --strict-read-only --output ./evidence/kvm.json
granny onprem datacenter openvz-inventory --strict-read-only --output ./evidence/openvz.json
granny onprem datacenter linux-host-inventory --strict-read-only --output ./evidence/hosts.json
granny onprem datacenter nas-inventory --strict-read-only --output ./evidence/storage.json
granny onprem datacenter bunnycdn-spa-inventory --strict-read-only --output ./evidence/bunnycdn.json
granny onprem datacenter hetzner-server-inventory --strict-read-only --no-customer-data --output ./evidence/hetzner.json
granny onprem datacenter cloud-backup-inventory --strict-read-only --output ./evidence/cloud-backups.json
granny onprem datacenter report --input ./evidence --format markdown
```

### Read-Only Data Sources

- Kubernetes: `kubectl get`, `kubectl describe`, `kubectl top`; metadata only for Secrets.
- VMware: vCenter/govc inventory and reporting calls only.
- KVM: `virsh list`, `virsh dominfo`, `virsh net-list`, `virsh pool-list` only.
- OpenVZ: `vzlist` and host information commands only.
- Linux hosts: OS/hardware/network/listening-port/status commands only.
- NAS/storage: vendor read-only status, volume, snapshot and backup reports.
- Firewall/network: exported rules and diagrams only; no commit/apply operations.
- BunnyCDN: list zones/domains/origins/cache/TLS metadata only; no purge or configuration changes.
- Hetzner: list server metadata for systems that do not store customer data; no lifecycle or firewall changes.
- Cloud backups: list backup targets, retention, encryption and restore-test metadata only; no restore/delete/retention changes.

### Explicitly Forbidden Operations

- Kubernetes: apply/delete/edit/patch/scale/restart/exec into application containers.
- VMware: power changes, migrations, snapshot create/delete, VM reconfiguration.
- KVM: start/shutdown/destroy/undefine/migrate/snapshot/change XML.
- OpenVZ: start/stop/set/migrate containers.
- NAS: delete/modify shares, snapshots, volumes, replication or backup jobs.
- Firewall: add/remove/change rules, NAT, VPN users, routes or commit configuration.
- BunnyCDN: purge cache, change edge rules, change TLS, change zones or alter origins.
- Hetzner: create/delete/reboot/rebuild servers, change firewall/network/storage settings.
- Cloud backups: delete/restore/overwrite backups, change retention, change encryption, change replication.

### Compliance Output

Generated reports should support:

- asset inventory
- network exposure inventory
- Kubernetes workload inventory
- virtualization inventory
- storage/backup inventory
- access-control summary
- ISO 27001 evidence map
- risk register draft
- BunnyCDN static SPA inventory
- Hetzner no-customer-data server inventory
- cloud backup inventory and restore-test evidence map

Related compliance docs:

- `T:/projects/TouristMobile/touristmobile-compliance/docs/infrastructure/DATACENTER_ARCHITECTURE_ASSESSMENT.md`
- `T:/projects/TouristMobile/touristmobile-compliance/docs/infrastructure/DATACENTER_READ_ONLY_DISCOVERY_RUNBOOK.md`
