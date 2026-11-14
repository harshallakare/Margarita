"""Purpose: generic OpenStack TargetProvider (any Keystone + Cinder + Nova cloud that is not VHI).

It is the VHI target without the vStorage shortcut: the new Cinder volumes are always written through
a temporary helper VM (helper_vm mode: volumes attached to the helper, data sent over SSH to a small
NBD agent, OSMorph over the same NBD path), then Nova boots the VM from the volumes. Only API
credentials and SSH reachability from the worker to the helper are needed (no storage-backend access).

Usage (endpoint config; password in the encrypted credentials). The same endpoint can also be a source:
    type: openstack
    auth_url: https://keystone.example.com:5000/v3
    username / password / user_domain_name / project_id (or project_name + project_domain_name)
    region_name: RegionOne
    volume_type: ssd                         # optional (Cinder default type otherwise)
    helper_image: ubuntu-22.04               # cloud image with python3 + cloud-init
    helper_network: private                  # reachable from the worker over SSH
    helper_floating_network: public          # optional: floating IP for the helper
    helper_flavor / helper_security_groups   # optional
    network_map / default_flavor / preserve_mac / availability_zone   # as for VHI

Boot volume: hw_disk_bus=scsi + hw_scsi_model=virtio-scsi (+ UEFI image properties), the same image
metadata Nova honours on stock OpenStack (libvirt driver). Clouds whose compute driver is not
libvirt/KVM are out of scope.

Replicas: volumes are rewritten in place through the helper; deploy clones a volume (Cinder clone).
Restore points stay off, as on VHI, until Cinder snapshots of rewritten volumes are verified.
Tested against fakes only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...core.context import RunContext
from ..vhi.target import VhiTarget


class OpenStackTarget(VhiTarget):
    type_name = "openstack"
    platform_family = "kvm"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, api: Any = None, qemu: Any = None):
        cfg = {**config, "write_mode": "helper_vm"}          # no vStorage outside VHI
        super().__init__(name, cfg, ctx, api=api, qemu=qemu)

    @property
    def write_mode(self) -> str:
        return "helper_vm"
