# Purpose: one image for every platform service (api / conductor / worker); the command picks the role.
# Usage:   docker compose up --build        (see docker-compose.yml); UI at http://localhost:8080/ui/
# Note: a worker that drives the Nutanix -> VHI fast path must run ON a VHI node (vStorage + the
#       Nutanix NFS export mounted); run that worker on the host or bind-mount those paths.
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
# qemu-utils: VHI qcow2 writes. nbdkit: VMware VDDK reads (mount VDDK from Broadcom at runtime and set
# vddk_libdir). cifs-utils: Hyper-V storage mounts (share_map). openssh-client: Proxmox node access. libguestfs-tools: offline OSMorph (virtio injection, fstab); uses /dev/kvm if passed in.
RUN apt-get update && apt-get install -y --no-install-recommends qemu-utils nbdkit libguestfs-tools cifs-utils openssh-client \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=web /web/dist ./web/dist
RUN pip install --no-cache-dir '.[platform,postgres,nutanix,vhi,vmware,hyperv,openstack,aws]'
RUN useradd -r -u 10001 vmigrate && mkdir -p /data && chown vmigrate /data
USER vmigrate
ENV VMIGRATE_DATA_DIR=/data VMIGRATE_WEB_DIR=/app/web/dist
ENTRYPOINT ["vmigrate-platform"]
