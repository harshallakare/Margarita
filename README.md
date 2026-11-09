<!--
Purpose: operator + developer guide for vmigrate — architecture, adding a provider, config
reference, operational pitfalls, and the platform / OSMorph support matrix.
Usage: read before running a migration or writing an adapter.
-->

# vmigrate — any-to-any VM migration + DR platform

vmigrate is a self-hosted, pluggable alternative to Coriolis. Each platform is one isolated adapter
behind stable interfaces, all built around one normalized VM model. Supporting N sources and M
targets takes N+M adapters, not N×M.

**Reference vertical: Nutanix AHV → Virtuozzo VHI.** This path was transcribed from a migration
done by hand: the 13-disk RIS-Sainath VM, of which Coriolis could only see 10.

> **Safety model.** Everything runs in dry-run mode unless you pass `--execute`. Source VMs and disks are never
> modified or deleted. Disks are only ever read from a frozen snapshot copy. Two independent
> enumerations must agree on the disk count, and the run fails loudly if they don't.

## Platform services

```
 Web UI (web/, React + Vite, at /ui) ──REST + SSE──►  API (FastAPI, vmigrate.api)
                                                        │  auth/RBAC · secret store (Fernet, key outside DB)
                                                        ▼
                     Postgres / SQLite: endpoints · migrations(+state) · executions(= queue) · tasks · logs · events
                         ▲                                   ▲
 Conductor (vmigrate.conductor): PENDING → build task DAG → QUEUED; cancels; expired lease → re-queue (resume)
 Worker(s) (vmigrate.worker):    claim + lease → build providers (decrypt creds) → run DAG → stream progress/logs
```

The three services are separate processes (`vmigrate-platform api|conductor|worker`). The API never does
heavy work. Each worker runs one execution at a time; you scale out by adding workers. The queue is the
`executions` table, claimed with an atomic compare-and-set and held by a lease the worker renews. If a
worker dies, its lease expires, the conductor re-queues the execution, and the next worker resumes it:
completed tasks are kept and disk copies continue from their durable watermark. One migration can have
only one active execution at a time.

**A migration execution is a task DAG** (`engine/flows.py`, run by `engine/dag.py`):

```
validate_endpoints → inspect_instance (DISK-COUNT GATE) → create_snapshot → deploy_disks
   ══expands══► replicate_disk:<disk> × N (parallel, largest first, retries on transient errors)
→ osmorphing → deploy_instance (create VM + attach + attach-count gate) → verify_instance → finalize
dry-run:  validate_endpoints → inspect_instance → plan          (no mutating task exists in the graph)
```

Every task records its status, attempts, progress %, message, result and logs. You can cancel any
execution, and you can retry a failed or cancelled one; a retry resumes it. Safety-gate errors are never retried.

### Replica mode (incremental DR)

A **replica** keeps a set of replica disks on the target and brings them up to date on every **sync**.
**Deploy** (failover or test failover) clones those disks into a new VM, and the replica keeps syncing
afterwards. Deploy never contacts the source, so it still works when the source site is down.

```
sync:   validate → inspect (DISK-COUNT GATE; disk set/sizes unchanged) → snapshot → replica disks
        ══► replicate_disk × N → finalize (restore point, retention, source snapshot rotation)
deploy: validate target → check replica → clone disks → OSMorph → create VM + attach → verify → finalize
delete: validate target → delete replica disks + restore points + source base snapshot (deployed VMs kept)
```

| Per-disk sync mode | When | Source read | Target write |
|---|---|---|---|
| `full` | first sync of the disk | everything | everything (zero-skip) |
| `cbt` | source has change tracking and a previous sync exists | changed regions only | changed regions only |
| `compare` | no change tracking | everything | only 1 MiB blocks whose checksum differs (the replica is not read) |

- **Block checksums:** every sync records a SHA-256 per 1 MiB block of each replica disk (8 MB per
  250 GiB, in the `replica_hashes` table). A `compare` sync checks the source against them instead of
  reading the replica back over the network, the approach Coriolis uses. They are cleared when a sync
  starts and saved only when it completes, so an interrupted sync never leaves checksums to trust; the
  next `compare` then reads the replica once ("compare (reading replica)") and rebuilds them. `cbt` syncs
  keep them current by re-reading whole blocks around each change, unless that would read more than 1.5x
  what changed (scattered small writes); then they are marked unknown instead.
- **Pipelined copy:** a reader thread prefetches the next chunks (`prefetch_chunks`, default 2) while the
  current one is written, and the helper-VM writer keeps up to 16 NBD writes in flight. Progress shows the
  current speed over the last 10 seconds next to the average.

- **Consistency:** replica disks are being changed while a sync runs. A failed sync leaves the replica
  marked *inconsistent*, and the next sync **resumes it with the same source snapshot**. Until then only a
  restore point can be deployed.
- **Restore points and retention:** after each sync, targets that support restore points snapshot the replica
  disks, keeping `options.retention` of them (default 3). The fake target supports them. **VHI does not yet**:
  a Cinder snapshot of a VMDS volume whose qcow2 backing we rewrite in place has not been verified on a
  cluster. On VHI, deploys clone the latest consistent sync with a Cinder clone (`source_volume_id`).
- **VHI incremental writes:** the local raw staging image is rebuilt from the VMDS backing
  (`qemu-img convert -f qcow2 -O raw`, a local vStorage read), the deltas are applied, and the image is converted
  back with the tuned options (pitfall 4). Source NFS reads are limited to what changed (with CBT).
- **Nutanix change tracking (CRT):** on the iSCSI transport, replica syncs read only the regions Prism reports
  as changed since the previous sync. After each sync, the volume group and iSCSI session are removed. The
  recovery point is kept as the next sync's reference, with its expiry extended to `crt_base_keep_hours`
  (default 14 days). Set that longer than your sync interval. The next sync calls Prism Central's v4
  `discover-cluster` and then the cluster's `compute-changed-regions`, reads those ranges, and deletes the old
  reference. Deleting the replica deletes the last one.
  - If CRT fails for a disk (the reference expired, or the AOS version doesn't support it), that disk falls
    back to `compare`. The sync still completes correctly.
  - The NFS transport has no CRT and always uses `compare`.
  - API field names come from Nutanix's own v4 dataprotection SDK. They are untested against a live Prism
    Central (needs PC 7.3+ / AOS 7.3+, or pc.2024.3+ per Nutanix's blog). `crt: false` turns CRT off.
- **Schedules:** 5-field cron in **UTC** (`*/15 * * * *`, `0 2 * * 1-5`, `@daily`), run by the conductor.
  A slot that comes due while the replica has an active execution is skipped and recorded, never stacked.
  `GET /schedules/preview?cron=` returns the next run times.
- Disks added, removed or resized on the source make the sync fail loudly. Create a new replica in that case
  (resizing replicas is not supported yet).

### Deploy on a VM (production-style)

One command on a fresh Linux VM or host (Ubuntu/Debian with `apt`, RHEL/Rocky/VHI with `dnf`/`yum`):

```bash
curl -fsSL https://raw.githubusercontent.com/harshallakare/Margarita/main/scripts/deploy.sh | sudo bash
```

Already root (minimal images often have no `sudo`)? Pipe to `bash` instead of `sudo bash`.

`scripts/deploy.sh` installs the OS packages it needs (only those missing). It finds or installs Python ≥ 3.11,
clones to `/opt/margarita/app`, builds the UI (with a pinned, checksum-verified Node if the system has none),
creates `/etc/margarita/{secret.key,margarita.env}` and `/var/lib/margarita`, and creates an `admin` user with a
random password (printed once, saved to `/root/margarita-admin-password`). It then starts three systemd services
and waits for `/health`. The API and conductor run as user `margarita`; the worker runs as root because it writes
VHI vStorage backings and uses mounts, qemu-img and ssh. Options: `--port`, `--bind`, `--branch`, `--db-url`
(Postgres), `--full` (nbdkit, guestfish, virt-v2v, virtio-win on RHEL, cifs-utils), `--no-web`, `--no-systemd`.
Re-running is safe: the key, config and users are kept.

Multi-site and worker options:

| Option | Effect |
|---|---|
| `--role all\|server\|worker` | `server` = API + conductor; `worker` = worker only, joining a central server (needs `--db-url postgresql+psycopg://…` and `--secret-key-file`) |
| `--region NAME` | the worker only takes jobs for endpoints in that region |
| `--worker-tags a,b` | extra capability tags (`vstorage`, `site-a`, …) that endpoints can require |
| `--pool NAME` | join worker pool NAME (tag `pool:NAME`) |
| `--enroll URL --enroll-token T` | join as a pool worker with a single-use token: fetches DB URL, secret key, region and pool from `URL/enroll` over HTTPS |
| `--export-agent PORT` | also run the export agent (HTTPS, self-signed cert pinned by peers) that serves this site's source disks to other sites; needs `--region` |

**Back up `/etc/margarita/secret.key`.** Without it, stored endpoint credentials cannot be decrypted.

Update to the latest commit of the deployed branch:

```bash
sudo bash /opt/margarita/app/scripts/update.sh            # --check to preview, --ref <tag|commit> to pin
```

`scripts/update.sh` works as follows:

- It refuses to run if the VM's checkout has local edits, and only fast-forwards.
- It waits for running migrations and syncs to finish (`--wait-timeout`, default 30 min).
- It reinstalls, rebuilds the UI only if `web/` changed, upgrades the DB schema (additive only), and restarts.
- It health-checks the API, conductor and worker.
- If the install or health check fails, it **rolls back to the previous commit** automatically.

Both scripts were tested in containers:

- **Ubuntu 24.04 with systemd:** fresh deploy, a migration, a normal update, rollback after a crash on start,
  rollback after a failed install, refusal on local edits, the wait for running work, and an idempotent re-deploy.
- **Rocky Linux 9:** deploy, including the automatic Python 3.12 install, an update and a migration.

They have not yet been run on a real VHI node.

### Demo (fake providers, no infrastructure)

```bash
scripts/demo.sh
```

This seeds a fake AHV source (with the 13-disk RIS-Sainath VM), a source that reproduces Coriolis's
10-disk cap, and a fake VHI target. The fake source has change tracking; raise its `generation` (Edit endpoint)
to simulate guest writes between replica syncs. Then open http://127.0.0.1:8080/ui/ and sign in as `admin` /
`demo-admin-password` (a local demo login). The demo state lives in `./vmigrate-demo`; delete that folder to start over.

### Web UI

`web/` is a React + TypeScript single-page app built with Vite. The API serves the built app at `/ui`
(`VMIGRATE_WEB_DIR`, default `web/dist`), so production needs no separate Node server.

| Screen | What it does |
|---|---|
| Endpoints | List, add, edit, test and delete endpoints. Each form is generated from the adapter's own field schema (`FIELDS` in `providers/__init__.py`). Secret fields are write-only and stored encrypted. |
| New migration (wizard) | source → VMs from inventory, each one inspected (**true disk count**, per-disk bus and size, and the disk-count gate; a mismatch blocks the wizard) → target → network/storage/flavor mapping → options → review. Creating a migration starts a dry-run. |
| Migration | phase, disk-count cross-check, run history, and controls: **Run migration** is enabled only after a successful dry-run and asks you to type the name. Also cancel, delete (the record only), and the per-disk verification report with .md/.json download |
| Execution | live task graph (one bar per disk, MB/s), streamed per-task logs, task details, the dry-run plan, cancel and retry (resume) |
| Replicas | list with consistency, last sync and next scheduled run. Detail page: sync now (typed confirm), deploy from the latest sync or a restore point, sync history with bytes read/written/unchanged per mode, restore points, deployments, a schedule editor with live cron preview, and delete-disks |
| Settings | workers, secret-store status, users (admin), and the platform support matrix |

Develop the UI with hot reload against a running API:

```bash
cd web && npm install && npm run dev
```

Then open http://localhost:5173/ui/. Vite proxies API calls to `VMIGRATE_API` (default `http://127.0.0.1:8080`).
Build the UI with `npm run build`.

### Quick start (platform)

```bash
.venv/bin/pip install -e '.[dev]'
```
```bash
export VMIGRATE_SECRET_KEY=$(.venv/bin/vmigrate-platform gen-key)
```
```bash
.venv/bin/vmigrate-platform create-user admin --role admin
```
```bash
.venv/bin/vmigrate-platform all --port 8080
```

`all` runs the API, conductor and worker in one process, for development only. Open
http://localhost:8080/docs for the OpenAPI UI. For the production-shaped topology (Postgres plus separate
api, conductor and worker containers), use `docker compose up --build`; see the header of `docker-compose.yml`.

Typical flow: `POST /auth/token` → `POST /endpoints` (non-secret `config` + write-only `credentials`) →
`GET /endpoints/{id}/instances/{vm}` (the true disk count, plus whether the gate passed) → `POST /migrations` →
`POST /migrations/{id}/start` (this is a **dry-run unless the body is `{"dry_run": false}`**) → watch `GET /events` (SSE) or
`GET /executions/{id}/tasks` → `GET /migrations/{id}/report`.

| Env var | Default | |
|---|---|---|
| `VMIGRATE_SECRET_KEY` / `_FILE` | — (required) | Fernet key(s). Pass `new,old` to rotate. |
| `VMIGRATE_DATABASE_URL` | `sqlite:///./vmigrate-platform.db` | `postgresql+psycopg://…` in production |
| `VMIGRATE_ENGINE_CONFIG` | — | YAML with an `engine:` block (defaults for every migration) |
| `VMIGRATE_DATA_DIR` | `./vmigrate-data` | reports |
| `VMIGRATE_LEASE_SECONDS` / `VMIGRATE_POLL_SECONDS` | 60 / 2 | worker lease, loop interval |
| `VMIGRATE_TOKEN_TTL_HOURS` | 12 | |
| `VMIGRATE_ALLOWED_PROVIDERS` / `VMIGRATE_CORS_ORIGINS` | — | comma lists |

**Endpoints are validated before they are saved.** Creating an endpoint, or editing its settings or
credentials, first connects to the platform with the submitted values and proves the credentials work. This
also checks named objects such as the project or protection domain. Only then is the endpoint stored. On
failure, the API answers `422 {"detail": {"message", "problems": [...]}}` and nothing is written, not even
the encrypted secret. This save-time check deliberately ignores worker-host prerequisites (NFS mounts,
nbdkit, the vStorage path), because the API may run on a different machine than the workers; those are checked
by each migration's dry-run/preflight on the worker. The endpoint page's **Test** button runs both.
`VMIGRATE_VALIDATE_TIMEOUT` (default 60 s) bounds the check.

Roles: `viewer` can read. `operator` can also manage endpoints and migrations and start, cancel or retry executions.
`admin` can also manage users and sees every project. Credentials are encrypted at rest and never returned; the API
rejects secret-looking keys in `config`.

## Coriolis-parity features (cross-site, pools, clusters, cutover, OSMorphing)

Everything below is tested against fakes and local files only. None of it has run on real infrastructure yet.

**Worker routing.** Each worker reports its region and capabilities: `provider:<type>` for importable SDKs,
`tool:<name>` for qemu-img, iscsiadm, ssh, virt-v2v-in-place and similar tools, plus `vstorage`, `root` and
operator tags. An endpoint's `region` and `required_tags` decide which workers may run its jobs. A queued job
that no worker can run shows why on the execution page.

**Cross-site transfers.** When the source and target endpoints are in different regions, the job runs at the
target site. Every source call is forwarded to an **export agent** at the source site
(`vmigrate-platform export-agent`, or `deploy.sh --export-agent 8443`). The agent runs the real source
provider, so snapshots, iSCSI sessions and safety checks stay at the source. It streams snapshot disks over
HTTPS, skipping zero blocks and compressing with zlib.
- Requests are HMAC-signed with a key derived from the shared secret key, and replays are refused after 5 minutes.
- The agent's self-signed certificate is pinned by its SHA-256 fingerprint, published through the workers table.
- Plain HTTP is refused.
- Replica deploys never contact the source site, so a failover works while the source is down.

**Worker pools.** These are named groups of workers (tag `pool:<name>`). A migration with `options.pool`
runs only on that pool, in the pool's region. Settings → Worker pools creates single-use **enrollment tokens**:
- A new host runs `deploy.sh --enroll https://<api> --enroll-token …` and receives the DB URL and secret key over HTTPS.
- Enrollment needs Postgres. It refuses plain HTTP unless `VMIGRATE_ALLOW_HTTP_ENROLL=1` is set (lab only).
- With `provision` set (VHI/OpenStack endpoint, image, flavor, network, `api_url`), the conductor boots minion
  VMs up to `min_size`, never more than `max_size`. Their cloud-init carries only an enrollment token.
- Minions are never deleted automatically. Use `DELETE /pools/{id}/minions/{server}`.

**Replica cutover.** Use `POST /replicas/{id}/cutover` (or the replica page's Cutover… button). It optionally
shuts the source VM down, then runs a final delta sync and deploys from it in one execution.
- Shutting down the source happens **only** when `shutdown_source` is true. It is the only source-state change
  Margarita makes. `force_power_off` does a hard power-off instead of a guest shutdown.
- Implemented for Nutanix, VMware, Hyper-V, Proxmox, OpenStack, AWS and physical servers.

**Full checksum verification.** The `engine.verify_full` option (wizard → Options) adds a SHA-256 of every byte
on the source snapshot and the committed target disk, on top of the sampled checks. A mismatch fails the disk.

**Linux OSMorphing** (KVM targets, offline with guestfish):
- OS detection.
- virtio initramfs.
- Network config: MACs are rewritten when known (`preserve_mac`, `osmorph.mac_map`), otherwise unpinned in
  ifcfg, NetworkManager and netplan. udev persistent-net rules are dropped.
- VMware, Hyper-V and Azure tools are removed; qemu-guest-agent is installed at first boot.
- fstab `/dev/sdX` entries become `UUID=`, plus nofail. Periodic fsck is disabled.
- SELinux `/.autorelabel` is set.
- Operator scripts:
  - offline scripts run inside the image;
  - first-boot scripts run once through a self-disabling systemd unit (log: `/var/log/vmigrate-firstboot.log`).

**Windows OSMorphing** calls `virt-v2v-in-place` (or `virt-v2v --in-place`) with virtio-win drivers
(`osmorph.virtio_win`, `$VIRTIO_WIN` or `/usr/share/virtio-win`).
- Static IPs are re-applied through `osmorph.windows_ip`.
- An optional first-boot script is installed with `virt-customize --firstboot`.
- Without these tools the step is manual and the run stops before VM creation.

**Physical Linux servers (P2V).** Endpoint type `physical`, over SSH with a pinned host key; needs python3 on
the server.
- `mode: cold`: the server runs a rescue ISO, and raw disks are read only when nothing on them is mounted read-write.
- `mode: lvm-snapshot`: the server stays live.
  - Each mounted LV gets an LVM snapshot, and plain partitions such as `/boot` are remounted read-only.
  - Each disk image is composed on the fly: LV extents come from the snapshots, and the PV metadata from a copy
    taken before the snapshots.
  - Thin, RAID or cache LVs, md, LUKS, and a root filesystem on a plain partition are refused.

**Shared-disk clusters.** Give every member the same `options.cluster`. Each shared disk (VMware multi-writer,
OpenStack or AWS multi-attach) is copied **once** by the first member to claim it, into a multi-attach target
volume (VHI: `multiattach_volume_type`). Every member then attaches that volume. Members wait for shared disks
only after their own disks are done, so two members cannot deadlock. A VM with shared disks and no cluster is
refused.
- vSphere cannot snapshot multi-writer disks; such VMs get a clear refusal.
- Hyper-V shared VHDX stays refused, because checkpoints exclude it.

**Extra transports.**
- VMware `transport: openvix` uses Cloudbase's [OpenVixDiskLib](https://github.com/cloudbase/OpenVixDiskLib)
  over NFC. It needs vSphere 8 and no VDDK. It is an external library installed separately; its license file is
  ambiguous, so check it before redistributing.
- Hyper-V `rct_service_url` uses Cloudbase's [rct-service](https://github.com/cloudbase/rct-service)
  (Apache-2.0) on the host. It provides disk reads and RCT changed ranges over HTTPS, so no SMB/CSV share
  mounts are needed.
- Both APIs were taken from their published source and are used as documented, not copied.

## VHI target: where the worker can run (write modes)

The VHI target can write the migrated disks in two ways. Choose it with **Disk write mode** on the VHI
endpoint (`write_mode`).

| Mode | Where the worker runs | How disks are written |
|---|---|---|
| `vstorage` | On a VHI node, as root, with vStorage mounted | Straight into each Cinder volume's qcow2 backing under `/mnt/vstorage`. Fastest. |
| `helper_vm` | Anywhere that can reach VHI's API and SSH to the helper | A temporary helper VM in VHI gets the new volumes attached; the worker writes them over SSH. This is how Coriolis works. |
| `auto` (default) | | `vstorage` when the vStorage datastore is visible on the worker, otherwise `helper_vm`. |

**Helper VM settings** (`helper_vm` mode):

| Field | Required | Meaning |
|---|---|---|
| `helper_image` | yes | A Linux cloud image in VHI with python3 and cloud-init (Ubuntu 22.04/24.04, Rocky/Alma 9, Debian 12). Nothing is installed on it. |
| `helper_network` | yes | Network the helper is attached to. The worker must reach the helper's address on it over SSH (port 22). |
| `helper_floating_network` | no | External network to allocate a floating IP from, when the helper's network isn't routable from the worker. |
| `helper_security_groups` | no | Existing security group(s) for the helper, comma-separated (default: the project's `default`, as Coriolis does). They must allow SSH (TCP 22) from the worker. Nothing is created. |
| `helper_create_security_group`, `helper_ssh_cidr` | no | Advanced: create a temporary group allowing TCP 22 from `helper_ssh_cidr` instead, deleted after the run. |
| `helper_flavor` | no | Default: the smallest flavor with 2 vCPU and 2 GiB RAM. |
| `helper_root_gib`, `helper_boot_timeout` | no | Helper root volume size (10) and SSH wait (600 s). |

**What happens in a run:**
1. On the first disk, the worker creates an SSH client key and an SSH host key and boots the helper from
   `helper_image` on a 10 GiB volume, in the project's `default` security group (or the ones you chose). cloud-init adds user `vmigrate`
   with the client key and installs the host key, which the worker pins, so the connection is verified from
   the first packet.
2. Each new volume is attached to the helper and found by its virtio serial (the volume ID).
3. The worker starts a small NBD block server on the helper over SSH (`python3`, standard library only,
   sent inline) and writes the disk through it. Zero chunks are skipped on new volumes; replica syncs write
   changed zeros explicitly. Sample and full SHA-256 verification read the volume back the same way.
4. OSMorph runs on the worker as usual: guestfish (and virt-v2v for Windows) open each volume through a local
   Unix socket that is bridged to the helper. The worker needs libguestfs (`deploy.sh --full`).
5. Before the real VM is created, every volume is detached from the helper. When the run ends, or fails, the
   helper VM, its root volume, the floating IP and the keys are deleted (and a temporary security group, if you asked for one). Cleanup
   keeps going if an API call fails and names anything left behind.

There is one helper per run, shared by all disks; disks are still copied in parallel. Everything the helper
path creates is tagged `vmigrate:managed`/`vmigrate:helper`, and volumes that aren't Margarita's are never
attached. A dry-run never creates a helper.

Not yet verified on a real VHI cluster: the virtio serial lookup inside the helper, cloud-init host key
injection on your image, and throughput over SSH (expect lower speeds than `vstorage` mode).

## Logs, diagnostics and audit trail

**Investigating a failure:**
- **Logs page:** searches every run's log lines by level, text, migration and time. Failures carry the full
  Python traceback (click the row) and the logger name. The **Platform events** tab lists run lifecycle,
  worker, schedule and requeue events.
- **Download diagnostics** (on any run's page, also `GET /executions/{id}/diagnostics`): one zip with the run,
  its tasks, every log line, events, migration options and state, the report, the endpoints (config redacted,
  credentials never included), workers and versions. Send this file when asking for help.
- **More detail:** set `VMIGRATE_EXEC_LOG_LEVEL=DEBUG` in `/etc/margarita/margarita.env` and restart the
  worker. Run logs then also record every Nutanix API call with status and timing. Calls that fail are always
  logged as warnings, with the response text.
- **Request ids:** every API response has an `X-Request-Id` header, and the matching audit entry stores it.
- **Service logs:** `journalctl -u margarita-api -u margarita-conductor -u margarita-worker`.

**Audit trail** (admins only: the Audit trail page and `GET /audit`):
- **What's recorded:** every API call that changes something, on every record type (users, endpoints,
  migrations, replicas, runs, schedules, pools, minions, enrollment). That includes refused attempts, logins
  and failed logins. Each entry says who did it, role, IP, user agent, action, record and outcome. The
  conductor's own actions (scheduled syncs, retention purges) appear as `system:conductor`. Downloading
  diagnostics and exporting the audit log are recorded too.
- **Secrets:** request bodies are stored with every password, key, token and credential replaced by `***`.
- **Tamper evidence:** there's no API to edit or delete entries. Each entry holds a SHA-256 chained to the
  previous one, and **Verify integrity** (`GET /audit/verify`) reports the first entry that was changed or
  removed directly in the database.
- **Retention:** entries older than `VMIGRATE_AUDIT_RETENTION_DAYS` (default 365; 0 keeps them forever) are
  purged daily, and the purge itself is recorded. **Export CSV** (`GET /audit/export`) saves a copy first.
- **Deleted migrations keep their run history:** deleting a migration or replica hides it everywhere, but its
  runs, tasks and logs stay searchable on the Logs page (listed as "name (deleted <id>)"; `GET
  /migrations?deleted=true`). They are removed for good after `VMIGRATE_DELETED_RETENTION_DAYS` (default 90;
  0 keeps them forever), or when an endpoint they used is deleted. Deleting never touches source or target VMs.

## Repository layout

| Path | Contents | Status |
|---|---|---|
| `src/vmigrate/ir/` | normalized `VmSpec` (IR) | done |
| `src/vmigrate/core/` | provider/reader/writer contracts, dry-run guard, errors, ranges, retry | done |
| `src/vmigrate/engine/` | `dag.py` (task engine), `flows.py` (migration DAG), `pipeline.py` (phases + gates), copier, verify, state, report | done |
| `src/vmigrate/providers/<platform>/` | adapters: `fake`, `nutanix` (source), `vhi` (target) | others planned |
| `src/vmigrate/osmorph/` | strategy registry (guest OS × target family) | Linux→KVM done |
| `src/vmigrate/db/` | schema (SQLAlchemy Core), repo, SQL task/state stores, queue | done |
| `src/vmigrate/security/` | secret store, password/token auth, RBAC | done (local auth) |
| `src/vmigrate/platform/` | settings + factory (rows → providers/runners) | done |
| `src/vmigrate/api/` · `conductor/` · `worker/` | services | done |
| `src/vmigrate/cli/` | `vmigrate` (single-host CLI), `vmigrate-platform` (services/admin) | done |
| `src/vmigrate/minions/` | worker-VM pools for pairs without direct storage access | planned |
| `web/` | React + Vite UI (served at `/ui`) | done |
| `scripts/` | `deploy.sh` / `update.sh` (VM install + safe updates), `demo.sh` + `demo_seed.py` (local demo) | done |

## Engine architecture

```
            ┌─────────────── engine (provider-agnostic) ───────────────┐
 Source ──► │ inspect → DISK-COUNT GATE → preflight → snapshot          │ ──► Target
 adapter    │ → per disk ∥: copy → verify → commit → verify → free      │     adapter
 (IR in)    │ → OSMorph(guest × target) → boot disk prep → create VM    │     (IR out)
            │ → attach (+ attach-count gate) → boot check → cleanup     │
            └──── state/resume (JSON) · reports (JSON+MD) · dry-run ────┘
```

| Layer | Module | Role |
|---|---|---|
| IR | `src/vmigrate/ir/model.py` | `VmSpec` / `DiskSpec` / `NicSpec`. Validation means exactly one boot disk and disk orders `0..n-1` with no gaps. |
| Contracts | `src/vmigrate/core/interfaces.py` | `SourceProvider`, `TargetProvider`, `DiskReader`, `DiskWriter`, refs |
| Dry-run guard | `src/vmigrate/core/context.py` | Every mutating provider method calls `ctx.require_execute()` first |
| Engine | `src/vmigrate/engine/` | `pipeline.py` (state machine), `copier.py`, `safety.py`, `verify.py`, `state.py`, `report.py` |
| OSMorph | `src/vmigrate/osmorph/` | Strategy registry keyed by `(guest family, target platform family)` |
| Config | `src/vmigrate/config/loader.py` | YAML, `${ENV}` secrets, strict validation |
| Providers | `src/vmigrate/providers/<platform>/` | One package per platform. SDK imports stay inside it. |
| CLI | `src/vmigrate/cli/main.py` | `validate`, `matrix`, `list-vms`, `inspect`, `migrate`, `status`, `cleanup` |

Dry-run is enforced in two places. First, the engine never calls a mutating method in dry-run;
it only builds a plan. Second, every mutating provider method raises `DryRunViolation` when
dry-run is on.

## Quick start (single-host CLI)

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,nutanix,vhi]'
```
```bash
.venv/bin/pytest
```
```bash
.venv/bin/vmigrate migrate -c examples/demo.fake.yaml --execute
```

To run the reference vertical in `vstorage` mode, run on a VHI node as root, with vStorage mounted and the Nutanix
container mounted over NFS:

```bash
vmigrate validate -c migrate.yaml
```
```bash
vmigrate inspect -c migrate.yaml --source ahv1 --vm RIS-Sainath
```
```bash
vmigrate migrate -c migrate.yaml
```
```bash
vmigrate migrate -c migrate.yaml --execute
```

`inspect` prints the IR and runs the disk-count gate. The first `migrate` (no flag) prints the
dry-run plan and writes a report. `--execute` performs the migration. To resume after a failure,
run the same command again.

Exit codes: `0` ok, `1` migration or provider error, `2` config error, `3` safety gate refused.

## Adding a provider (adapter checklist)

1. Create `src/vmigrate/providers/<platform>/` and import its SDK only there.
2. Subclass `SourceProvider` and/or `TargetProvider` and implement every abstract method.
3. **Source requirements:**
   - `inspect_vm` follows pagination and never caps the disk count.
   - `disk_count_crosscheck` returns at least 2 *independent* counts.
   - `open_disk` accepts only a `SnapshotRef` and refuses live disks.
   - `cleanup` deletes only what `create_snapshot` recorded.
4. **Target requirements:**
   - `find_disk` and `find_vm` return only resources this tool created (tag them), for idempotency.
   - `commit_disk` finalizes a written disk.
   - `cleanup_on_failure` deletes only the ids in `created`.
   - Set `platform_family` (`kvm` / `vmware` / `aws` / `azure`) so OSMorph resolves.
5. Call `self.ctx.require_execute("what", **details)` at the top of every method that changes state.
6. Keep `SnapshotRef.data` / `TargetDiskRef.data` JSON-safe, because they are persisted for resume.
7. Put the platform JSON → IR mapping in a pure module and unit-test it with canned payloads
   (see `src/vmigrate/providers/nutanix/mapping.py`).
8. Flip the adapter's row in `src/vmigrate/providers/__init__.py` from `planned` to `implemented`, and add
   its endpoint form fields to `FIELDS` (mark secrets with `secret: true`). The UI picks them up automatically.
9. Add tests with a fake client (see `tests/fakes_platform.py`), including a run through `MigrationRunner`.

## Config reference

See `examples/config.example.yaml` (fully commented). For provider keys, see the docstrings in
`src/vmigrate/providers/nutanix/source.py` and `src/vmigrate/providers/vhi/target.py`.

| `engine.*` | default | |
|---|---|---|
| `state_dir` / `report_dir` | `./vmigrate-state` / `<state_dir>/reports` | per-migration `*.state.json`, `*.report.{json,md}` |
| `max_parallel_disks` | 4 | Disks copied concurrently. Staging must hold this many of the largest disks. |
| `chunk_size` | 64MiB | linear read size |
| `checkpoint_every` | 1GiB | resume watermark granularity |
| `prefetch_chunks` | 2 | Chunks read ahead from the source while the current one is written (0 turns pipelining off). |
| `verify_samples` / `verify_sample_bytes` | 32 / 1MiB | Sampled checksums. Samples always include the first and last MiB. |
| `app_consistent` | true | snapshot mode (per-migration override allowed) |
| `cleanup_source_snapshot` | true | remove the snapshot/clone after success |
| `on_failure` | `keep` | `keep` lets a re-run resume. `cleanup` deletes what this run created on the target. |
| `boot_check` / `boot_timeout` | true / 900 | Watches the serial console for a login prompt vs emergency mode or a panic. |

`migrations[]`: `name`, `source`, `target`, `vm`, `target_name?`, `guest_os?` (overrides detection),
`target_options?` (`flavor`, `network_map`, `availability_zone`, `boot_bus`, …), and `osmorph?`
(`fstab_harden`, `disable_periodic_fsck`, `fstab_uuid_map`, `allow_manual`).

Secrets: use `${VAR}` / `${VAR:-default}`. `validate` warns about literal secrets in the file.

## Operational notes (pitfalls proven on the reference path)

1. **The disk count is never capped.** v3 `disk_list` and v2 `vm_disk_info` must agree on the
   exact address set and on sizes. The materialized snapshot clone must expose the same count, and
   so must the target attachment list. Volume-group disks fail loudly instead of being dropped.
2. **Live disks are never read.** A PD out-of-band snapshot is restored as a powered-off clone,
   `vmigrate-<run>-<name>`, and its vmdisks are what gets read. Reading a source vmdisk uuid raises `LiveDiskReadRefused`.
3. **Nutanix disk data travels over iSCSI by default (no NFS, no protection domain).** With Prism Central
   ≥ 7.3 (v4 API) and AOS ≥ 6.8, `transport: auto` uses the method Coriolis documents:
   - a v4 **recovery point** (application- or crash-consistent);
   - a temporary **volume group** `vmigrate-<run>`, with one disk per VM disk whose data source is the disk
     recovery point (instant, no copy);
   - **this worker's iSCSI initiator is attached through the API**, so nothing is whitelisted by hand;
   - `iscsiadm` logs in to the cluster's **Data Services IP**;
   - each LUN is mapped to its disk (both Nutanix target layouts) and size-checked;
   - the recovery point's, volume group's and visible LUNs' disk counts all join the disk gate;
   - cleanup logs out, detaches the initiator, then deletes the volume group and the recovery point
     (only `vmigrate-*` objects).

   Worker needs: open-iscsi (`deploy.sh` installs it and enables `iscsid`) and TCP 3260 to the Data Services
   IP. The v4 request/response shapes live in one module (`providers/nutanix/v4.py`). A missing field fails
   loudly with the raw payload, so the first live run will confirm them against your Prism Central version.
   Older clusters, or no Prism Central, use the **NFS transport** below automatically (`transport: nfs`
   forces it; `transport: iscsi` forbids the fallback).
   **NFS transport: disk data is read over NFS, mounted automatically.** Prism has no API that streams a
   vmdisk's bytes, so the worker reads the snapshot clone's files from the storage container. You don't
   configure mounts: the worker mounts each container it needs **read-only** from the Prism Element address
   (`nfs_host` overrides it), and unmounts it afterwards. The one-time cluster step is to add the worker's IP
   to the cluster's or container's NFS whitelist. A refused mount names the exact IP to whitelist. Pre-mounted
   paths (`nfs_root`, `nfs_mounts`) are still honored. Mounting needs root, as the worker runs in `deploy.sh` installs.
   **No qemu-img against Nutanix NFS.** Hole probing on those vdisks hangs. The reader uses plain
   `pread` in 64 MiB blocks. qemu-img only ever touches local staging and the vStorage backing.
4. **Tuned qcow2 writes:** `qemu-img convert -p -W -m 16 -f raw -O qcow2 -o compat=1.1,cluster_size=1048576`.
5. **VMDS backing stays Cinder-consistent.** The volume must be detached and available, and the backing must
   already be qcow2. The tool writes in place to `vols/datastores/cinder/volume-<ID>/volume-<ID>`,
   leaves the sidecars alone, then runs `chown root:vstorage-users` and `chmod 660`, and checks
   that the header's virtual size equals the volume size. The staged raw image is sized to the GiB-rounded volume.
6. **Bus match instead of driver injection.** The boot volume gets `hw_disk_bus=scsi` and
   `hw_scsi_model=virtio-scsi`. All volumes go into the BDM at create time, in source SCSI order, so `/dev/sdX` is preserved.
7. **Keystone scoping.** Scope by `project_id` only. `OS_PROJECT_NAME`, `OS_PROJECT_DOMAIN_*` and
   `OS_TENANT_*` are scrubbed, and env and clouds.yaml loading are disabled. Preflight fails if the
   token's project differs. A missing role assignment must be granted by an admin; the tool does not do it.
8. **No Glance.** Volumes are written directly.
9. **Boot-time fsck:** OSMorph adds `nofail` and pass `0` to data mounts and runs `tune2fs -c 0 -i 0`
   (through guestfish on the committed images). Run `fsck -n` once offline. Extent-tree "IGNORED" lines are not errors.
10. **Throughput:** single NFS streams top out around 150 MB/s, so the tool parallelizes across disks, largest first.
    Each disk is committed and its staging freed as soon as it is done. Preflight checks staging headroom for
    `max_parallel_disks` disks in flight.

## VMware vSphere source

Endpoint config (`type: vmware`): `host` (vCenter recommended), `username`, `password` (encrypted),
`verify_tls`, `transport` (`auto` | `vddk` | `http`), `vddk_libdir`, `vddk_transports` (default
`nbdssl:nbd`; add `hotadd` only when the worker is a VM on the same vSphere), and an optional `thumbprint` pin.

| Step | How |
|---|---|
| Inspect | `vm.config` devices map to the IR. Disks are ordered by controller, bus and unit (`scsi0:12` etc.), and the **controller model** is recorded (pvscsi, lsilogic, lsilogic-sas, buslogic, ahci, ide, nvme). |
| Disk-count gate | device-list disks vs `layoutEx.disk`, plus the disk count in the snapshot's config |
| Refused (fail loud) | **independent** disks (snapshots skip them, so reading one would be a live read), **physical-mode RDMs**, unknown controllers, VM names that are not unique (use the moref) |
| Snapshot | `CreateSnapshot_Task` named `vmigrate-<run>`, memory off, `quiesce = app_consistent` (needs VMware Tools) |
| Read: `vddk` | `nbdkit vddk` (the virt-v2v path) opens the snapshot's base disk and exports it read-only on a private socket, which vmigrate's NBD client reads. The password is passed as `password=+FILE` (a 0600 temp file, never on argv). VDDK must be downloaded from Broadcom and unpacked at `vddk_libdir`. |
| Read: `http` | Range GETs of the base `-flat.vmdk` through `/folder`. Only allowed when **at the snapshot** the disk had no parent (our snapshot is the only one) and a flat backing. Otherwise it is refused; use `vddk`. |
| Thin disks | when CBT is on, a first full copy asks `QueryChangedDiskAreas(changeId="*")` and skips never-written regions |
| Replicas | with `changeTrackingEnabled`, syncs read only `QueryChangedDiskAreas` ranges. Checkpoints are per-disk changeIds, so **no snapshot is left on the VM** between syncs. If CBT is reset, that disk falls back to `compare` (correct, slower). vmigrate never enables CBT itself, because that would reconfigure the source VM; enable it yourself if you want incremental reads. |
| Cleanup | `RemoveSnapshot_Task(consolidate=True)`, and only for snapshots named `vmigrate-*` that the run recorded |

**OSMorph for VMware guests.** A VMware Linux guest boots from pvscsi or LSI, but on VHI/KVM the same
"scsi" bus is virtio-scsi. Matching the bus alone is **not** enough; RHEL-family initramfs images only contain
the original host's drivers. The Linux→KVM strategy therefore requires virtio in every kernel's
initramfs and automates it offline with guestfish. For each kernel it skips the rebuild if the drivers are
built in or already present; otherwise it runs dracut (RHEL/SUSE) or update-initramfs (Debian/Ubuntu) and verifies the
result. If guestfish is not installed on the worker, the run stops **before creating the VM** (all disks are already
copied and committed), so you can inject manually and rerun with `osmorph.allow_manual: true`. NIC models
change too (vmxnet3 becomes virtio-net), so check interface naming and DHCP. This automation has only been
tested with mocks; validate it on a test VM per distro before production use. Windows guests remain manual.

Minimum vSphere role: inventory read, `VirtualMachine.State.CreateSnapshot` / `RemoveSnapshot`,
`VirtualMachine.Provisioning.DiskRandomRead` (VDDK), and `Datastore.Browse` (http transport).

## Hyper-V source

Endpoint config (`type: hyperv`): `host` (the node that owns the VM), `username` (Hyper-V Administrators),
`password` (encrypted), `winrm_transport` (`ntlm` | `kerberos` | `credssp`), `port` (5986), `use_ssl`,
`verify_tls`, **`share_map`** and `rct`.

The worker reads disk files directly, so it must mount the VM storage (SMB share, or the CSV exposed as a
share). `share_map` maps host paths to those mounts, longest prefix first and case-insensitive:

```yaml
share_map:
  "C:\\ClusterStorage\\Volume1": /mnt/hv/csv1     # mount -t cifs //hv01/Volume1 /mnt/hv/csv1 -o ro,...
  "\\\\fs01\\vmstore": /mnt/hv/vmstore
```

| Step | How |
|---|---|
| Control | PowerShell over WinRM (pywinrm). One script per operation, all in `providers/hyperv/client.py`. Parameters travel as base64 JSON and are never spliced into the script. |
| Inspect | Get-VM, Get-VMHardDiskDrive, Get-VHD, Get-VMNetworkAdapter, plus KVP for the guest OS name. Gen1 is BIOS with IDE first; Gen2 is UEFI, boot disk taken from the firmware boot order. Controller models are `hyperv-ide` / `hyperv-scsi`, so OSMorph injects virtio. |
| Disk-count gate | Get-VMHardDiskDrive vs WMI `Msvm_StorageAllocationSettingData` (logical disks), plus the snapshot's disk count |
| Refused | pass-through disks, shared VHDX / VHD Sets (checkpoints exclude them), unknown controllers, VM names that are not unique (use the VM id) |
| Snapshot | WMI **recovery snapshot** (type 32768, the backup-application type). `ConsistencyLevel` is 1 (VSS) when `app_consistent`, else 2. The VM's own checkpoint settings are not changed. |
| Read | the disk file active at the snapshot is frozen. It is read through `share_map` with the built-in pure-Python **VHDX/VHD readers** (checksums validated; a dirty log is refused; unallocated blocks are skipped). If that file is differencing (the VM already had checkpoints), the read is refused; merge those checkpoints first. |
| Cleanup | `DestroySnapshot`, only for the recovery snapshot this run recorded |
| Replicas | `compare` mode by default (full read, only changed blocks written). **`rct: true` (experimental):** each sync ends with `ConvertToReferencePoint`, and the next sync reads only `GetVirtualDiskChanges` ranges. The previous reference point is destroyed, and deleting the replica releases the last one. Any RCT error makes that disk fall back to compare. These WMI calls are untested on a real host, which is why RCT is off by default. |

Worker host requirements: the share mounts (cifs-utils), WinRM reachable (HTTPS 5986 recommended), and
guestfish for the virtio injection. Windows guests remain manual for OSMorph.

## Proxmox VE source

Endpoint config (`type: proxmox`): `url`, `token_id` (`user@realm!name`), `token_secret` (encrypted),
`verify_tls`, `ssh_user` (root, or a user with passwordless sudo), `ssh_private_key` (encrypted),
`ssh_known_hosts` (**pinned** host keys for every node, required), `ssh_port`, and `node_addresses`.

| Step | How |
|---|---|
| Inspect | `/cluster/resources` + `/nodes/{node}/qemu/{vmid}/config`. Disks are `scsiN`/`virtioN`/`sataN`/`ideN`, ordered by bus then index. The controller model comes from `scsihw` (virtio-scsi, lsilogic, megasas, pvscsi), virtio-blk, ahci or ide. Exact sizes come from the storage content listing. CD-ROMs, cloud-init drives, `unusedN`, `efidisk0` and `tpmstate0` are not data disks (EFI vars and TPM state are noted, not copied). |
| Disk-count gate | config disks vs the storages' content listing for the vmid (minus unused, EFI, TPM and cloud-init). Any **pending** (unapplied) disk change fails loudly. |
| Refused | pass-through disks (`/dev/...`), disks on storage without snapshots (thick LVM, iSCSI, raw files on dir/NFS), LXC containers, VM names that are not unique (use the vmid) |
| Snapshot | `POST .../snapshot snapname=vmigrate-<run>`, without RAM state. App-consistent requires the guest agent (`agent: 1`), which makes PVE freeze the guest filesystems. Without the agent the run is refused, never silently crash-consistent. |
| Read | over SSH on the owning node, read-only. **LVM-thin:** activate the snapshot LV. **ZFS:** read-only clone of `@snap`, destroyed afterwards. **Ceph RBD:** `rbd:pool/image@snap`. **qcow2 on dir/NFS/CIFS:** the internal snapshot via `--load-snapshot` + `--force-share`. `qemu-nbd --read-only` serves it on a node-local socket, OpenSSH forwards the socket to the worker, and vmigrate's NBD client reads it. Temporary objects are named `vmigrate-*` and removed even if setup fails halfway. |
| SSH hygiene | host keys pinned (`StrictHostKeyChecking=yes`, no trust on first use). The key goes to a 0600 temp file only for the ssh process. Commands are argv lists of quoted, validated names (no spaces or shell metacharacters). |
| Cleanup | `DELETE .../snapshot/vmigrate-*`, only snapshots this run created |
| Replicas | **Ceph RBD:** `rbd diff --from-snap <prev> image@cur` reads only changed extents, including discarded ones, which are written as zeros. The previous vmigrate snapshot stays on the VM as the diff base and is rotated each sync. **LVM-thin, ZFS, qcow2:** `compare` mode (full read, only changed 64 KiB blocks written; no snapshot kept). |

OSMorph: Proxmox guests usually boot from virtio-scsi or virtio-blk, so going to VHI/KVM needs **no** driver
injection. Guests on pvscsi, LSI, megasas, SATA or IDE get the virtio step. UEFI VMs: the target creates
fresh NVRAM, so the guest must have the fallback boot loader (`\EFI\BOOT\BOOTX64.EFI`). Most distros install it.

Token privileges: VM.Audit, VM.Snapshot, Datastore.Audit, Sys.Audit. Node tools used: `pvesm`, `qemu-nbd`,
and per storage `lvchange`, `zfs`/`udevadm` or `rbd`.

## OpenStack source

Endpoint config (`type: openstack`): Keystone (`auth_url`, `username`, `password` (encrypted), `project_id`,
`user_domain_name`, `region_name`, `verify_tls`). Auth is scoped by project ID with the environment scrubbed
(pitfall 7). The code is shared with the VHI target in `providers/common/openstack_auth.py`. The **transport** is
`rbd` or `attach`.

| Step | How |
|---|---|
| Inspect | Nova server + flavor, `os-volume_attachments`, Cinder volumes, the image or boot-volume metadata (`hw_disk_bus` / `hw_scsi_model` give the controller, `hw_firmware_type`, `os_type`/`os_distro`), interfaces. Disks are ordered by device (`vda` < `vdz` < `vdaa`). |
| Disk-count gate | Nova `os-volume_attachments` vs Cinder volumes attached to the server (plus the image-backed root), and the snapshot's disk count |
| Refused | flavor **ephemeral** disks (Nova snapshots never include them), attached volumes Cinder can't see, `app_consistent` without `hw_qemu_guest_agent=yes`, `app_consistent` for an image-backed root **plus** volumes (Nova cannot freeze both together), server names that are not unique |
| Snapshot | Nova `createImage` named `vmigrate-<run>`. **Volume-backed:** one Cinder snapshot per volume, quiesced together by the guest agent. **Image-backed root:** a Glance image, plus a forced Cinder snapshot per attached volume (crash-consistent per disk, logged loudly). |
| Read: `rbd` | `qemu-nbd --read-only rbd:<cinder_pool>/volume-<v>@snapshot-<s>` (or the Glance image's own RBD snapshot, raw only). With `ceph_access: local` the worker has ceph.conf + keyring; with `ssh` it goes through `ceph_host` with a pinned host key (shared code in `providers/common/remote.py`). |
| Read: `attach` | works on any Cinder backend. The worker must be an instance in the same cloud. A temporary `vmigrate-*` volume is created from the snapshot/image, marked read-only where policy allows, attached to the worker, and read as a block device (`/dev/disk/by-id/virtio-<id>`). It is then detached and deleted, even if the device never appears. |
| Cleanup | the `vmigrate-*` image, the "snapshot for vmigrate-*" snapshots Nova made, our own forced snapshots, and temporary volumes. Anything else is refused. |
| Replicas | `rbd` transport with volume disks: `rbd diff --from-snap snapshot-<prev>` reads only changed extents, and the previous snapshot set is kept as the diff base. Image-backed roots and the `attach` transport use `compare` mode. |

The Glance upload in `createImage` only happens for image-backed roots on non-Ceph ephemeral storage. Root disks
are usually small, but keep pitfall 8 (multi-TB through Glance) in mind: move large root disks to volumes first.

## AWS EC2 source

Endpoint config (`type: aws`): `region`, plus `access_key_id` / `secret_access_key` (encrypted; leave empty to use
the worker's own IAM role), an optional `role_arn` + `external_id` to assume a role in the source account,
`ignore_instance_store`, and `read_workers` (parallel block reads per disk, default 16).

| Step | How |
|---|---|
| Inspect | DescribeInstances + DescribeInstanceTypes + DescribeVolumes. Disks are ordered root first, then by device. Nitro instances use `nvme` disks and `ena` NICs; Xen instances use `xen-blkfront` / `xen-netfront`. Neither is virtio, so OSMorph injects virtio for KVM targets. UEFI comes from BootMode. |
| Disk-count gate | BlockDeviceMappings vs DescribeVolumes(`attachment.instance-id`). The snapshot set must also cover exactly the inspected volumes; a volume attached or detached in between fails loudly. |
| Refused | **instance store** data (never in EBS snapshots) unless `ignore_instance_store: true`; `app_consistent` (EBS multi-volume snapshots are crash-consistent, with no native Linux quiesce), so set it false or stop the instance; non-unique `Name` tags (use the instance id) |
| Snapshot | `CreateSnapshots` for the whole instance: one crash-consistent point in time across all volumes, tagged `vmigrate:run`, waited until `completed` |
| Read | **EBS direct APIs**, with no helper instance and no volume attach. ListSnapshotBlocks is read as a sliding window of tokens, so multi-TiB volumes don't hold millions of tokens in memory. GetSnapshotBlock fetches blocks in parallel, each SHA-256-verified, with corrupt blocks re-fetched and throttling backed off. Never-written blocks are skipped on a full copy. |
| Cleanup | `DeleteSnapshot`, only for snapshots tagged `vmigrate:run` |
| Replicas | `ListChangedBlocks(previous, current)` per volume, at 512 KiB granularity; blocks zeroed since the last sync are written as zeros. The previous snapshot set is kept as the diff base and rotated each sync. |

IAM: `ec2:Describe{Instances,Volumes,InstanceTypes,Snapshots}`, `ec2:CreateSnapshots`, `ec2:CreateTags`,
`ec2:DeleteSnapshot`, `ebs:ListSnapshotBlocks`, `ebs:GetSnapshotBlock`, `ebs:ListChangedBlocks`, plus `kms:Decrypt`
on the volume keys for encrypted volumes. **Costs:** AWS bills EBS direct API requests (about one request per
512 KiB block read) and snapshot storage. A replica's kept base snapshot is incremental.

## Support matrix

| Side | Platform | Status |
|---|---|---|
| source | Nutanix AHV | **implemented** (reference) |
| source | VMware vSphere (vCenter / ESXi) | **implemented**. Tested against a fake vSphere client only; not yet run against a real vCenter. |
| source | Microsoft Hyper-V (Windows Server 2016+) | **implemented**. Tested against a fake WinRM client only (with real VHDX files); not yet run against a Hyper-V host. RCT is experimental and opt-in. |
| source | Proxmox VE (qemu VMs) | **implemented**. Tested against a fake PVE API and node shell (with a real NBD read path); not yet run against a cluster. |
| source | OpenStack (Nova + Cinder; Ceph or any backend) | **implemented**. Tested against a fake cloud only; not yet run against a real deployment. |
| source | AWS EC2 (EBS-backed instances) | **implemented**. Tested against a fake EC2/EBS only; not yet run against an AWS account. |
| source | Physical Linux server (P2V over SSH: cold or live LVM-snapshot) | **implemented**. Tested against a scripted shell and real local files; not yet run on hardware. |
| source | Any of the above at another site | **implemented** through the export agent (cross-site). |
| source | Azure | planned (interface-ready) |
| target | Virtuozzo VHI | **implemented** (reference) |
| target | OpenStack, Proxmox VE, Nutanix AHV, VMware vSphere, AWS, Azure | planned |

| OSMorph | KVM family | VMware | AWS | Azure |
|---|---|---|---|---|
| Linux | **auto**: no injection when the guest already boots from virtio on the same bus (AHV). Otherwise (VMware pvscsi/LSI, unknown controller, bus change), virtio is injected offline with guestfish (dracut or update-initramfs, then verified). Also: OS detection, network MAC handling, hypervisor-tool swap, fstab UUID/nofail, tune2fs, SELinux relabel, offline + first-boot scripts. Without guestfish this is **manual**. | manual | manual | manual |
| Windows | **auto** with virt-v2v-in-place + virtio-win on the worker (drivers, tool removal, static IPs, first-boot script); otherwise manual | manual | manual | manual |

A "manual" cell stops the run before VM creation unless `osmorph.allow_manual: true` is set, and the
report says so in bold. Run `vmigrate matrix` for the live view.

Incremental sync: `SourceProvider.changed_regions` and range-aware `copy_disk(ranges=...)` are in
place, plus range math in `src/vmigrate/core/ranges.py`. Orchestration of delta passes is not wired yet.

| Mode / feature | Status |
|---|---|
| Migration mode via API → conductor → worker (task DAG, resume, cancel, retry, lease recovery) | **done** |
| Endpoints, encrypted credentials, local users + RBAC, SSE events, per-task logs, reports | **done** |
| Replica mode: incremental sync (full / cbt / compare), deploy from latest or restore point, schedules, retention, delete-disks | **done** (fake + VHI; VHI without restore points) |
| Nutanix change tracking (CRT) for replicas | **done** (iSCSI transport; fakes only, not yet run on a live Prism Central) |
| Web UI: endpoints, wizard, live progress, reports, settings | **done** |
| Worker routing (regions, capabilities), worker pools, enrollment tokens, auto-provisioned minions (VHI/OpenStack) | **done** (fakes only) |
| Cross-site export agent, replica cutover, full checksum verification, shared-disk clusters | **done** (fakes only) |
| OIDC/Keystone identity, metrics | planned |
| RabbitMQ/Redis queue backend | optional; the DB queue sits behind the same claim/renew calls |
