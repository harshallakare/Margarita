"""Purpose: PURE mapping from EC2 descriptions (DescribeInstances / DescribeVolumes /
DescribeInstanceTypes dicts, as boto3 returns them) to the IR, the two-API disk cross-check,
device ordering, and EBS block-index -> byte-range helpers. No I/O.

Usage:
    spec = to_vmspec(instance, volumes, itype)        # raises AwsMappingError (fail loud)
    crosscheck(instance, volumes)                     # {"ec2.BlockDeviceMappings": n, "ec2.DescribeVolumes": m}
    blocks_to_ranges([0, 1, 7], block_size, size)     # merged ByteRanges

Rules:
  * every EBS volume in BlockDeviceMappings; no cap; cross-checked against DescribeVolumes
    filtered by attachment.instance-id
  * instance-store (ephemeral NVMe/SSD) data is never in EBS snapshots: instance types with local
    storage are REFUSED unless ignore_instance_store=true (you confirm it only holds scratch data)
  * controller: Nitro instances present EBS as NVMe (controller "nvme", NIC "ena"); Xen ones as
    xen-blkfront. Neither is virtio, so OSMorph injects virtio for KVM targets.
"""

from __future__ import annotations

import re
from typing import Any

from ...core.ranges import ByteRange, normalize
from ...ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec

GIB = 1 << 30
_DEV = re.compile(r"^/dev/(?:sd|xvd)([a-z]+)(\d*)$")


class AwsMappingError(ValueError):
    pass


def device_key(dev: str, root: str | None = None) -> tuple[int, int, str]:
    if root and dev == root:
        return (0, 0, "")
    m = _DEV.match(dev or "")
    if not m:
        return (2, 0, dev or "")
    return (1, len(m.group(1)), m.group(1))


def tag(obj: dict[str, Any], key: str) -> str | None:
    return next((t["Value"] for t in obj.get("Tags") or [] if t.get("Key") == key), None)


def ebs_mappings(instance: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in instance.get("BlockDeviceMappings") or [] if (m.get("Ebs") or {}).get("VolumeId")]


def crosscheck(instance: dict[str, Any], volumes: list[dict[str, Any]]) -> dict[str, int]:
    iid = instance["InstanceId"]
    attached = [v for v in volumes if any(a.get("InstanceId") == iid and a.get("State") in ("attached", "attaching")
                                          for a in v.get("Attachments") or [])]
    return {"ec2.BlockDeviceMappings": len(ebs_mappings(instance)), "ec2.DescribeVolumes": len(attached)}


def guest_os(instance: dict[str, Any]) -> GuestOS:
    if (instance.get("Platform") or "").lower() == "windows":
        return GuestOS(OsFamily.WINDOWS, distro="windows")
    details = (instance.get("PlatformDetails") or "").lower()
    for key, distro in (("red hat", "rhel"), ("suse", "sles"), ("ubuntu", "ubuntu"), ("linux", None)):
        if key in details:
            return GuestOS(OsFamily.LINUX, distro=distro)
    return GuestOS(OsFamily.UNKNOWN)


def to_vmspec(instance: dict[str, Any], volumes: list[dict[str, Any]], itype: dict[str, Any],
              *, ignore_instance_store: bool = False) -> VmSpec:
    iid = instance["InstanceId"]
    name = tag(instance, "Name") or iid
    if itype.get("InstanceStorageSupported") and not ignore_instance_store:
        gb = (itype.get("InstanceStorageInfo") or {}).get("TotalSizeInGB")
        raise AwsMappingError(f"{name}: instance type {instance.get('InstanceType')} has {gb} GB of instance store; "
                              "EBS snapshots never include it. Move that data to EBS, or set ignore_instance_store: "
                              "true if it only holds scratch data.")
    by_id = {v["VolumeId"]: v for v in volumes}
    root = instance.get("RootDeviceName")
    maps = sorted(ebs_mappings(instance), key=lambda m: device_key(m["DeviceName"], root))
    if not maps:
        raise AwsMappingError(f"{name}: no EBS volumes")
    nitro = (itype.get("Hypervisor") or "nitro") == "nitro"
    bus, ctrl = (DiskBus.NVME, "nvme") if nitro else (DiskBus.SCSI, "xen-blkfront")
    disks = []
    for i, m in enumerate(maps):
        vid = m["Ebs"]["VolumeId"]
        v = by_id.get(vid)
        if v is None:
            raise AwsMappingError(f"{name}: volume {vid} ({m['DeviceName']}) not returned by DescribeVolumes")
        disks.append(DiskSpec(id=vid, bus=bus, size_bytes=int(v["Size"]) * GIB, order=i,
                              is_boot=m["DeviceName"] == root or (root is None and i == 0), bus_index=i,
                              label=m["DeviceName"].removeprefix("/dev/"), controller=ctrl,
                              source_handle={"volume_id": vid, "device": m["DeviceName"],
                                             "encrypted": bool(v.get("Encrypted")), "type": v.get("VolumeType")},
                              shared_id=vid if v.get("MultiAttachEnabled") else None))
    if not any(d.is_boot for d in disks):
        disks[0].is_boot = True
    cpu = instance.get("CpuOptions") or {}
    vcpu = (itype.get("VCpuInfo") or {})
    cores = int(cpu.get("CoreCount") or vcpu.get("DefaultCores") or 1) * int(cpu.get("ThreadsPerCore")
                                                                          or vcpu.get("DefaultThreadsPerCore") or 1)
    nic_model = "ena" if nitro else "xen-netfront"
    nics = [NicSpec(id=n["NetworkInterfaceId"], network=f"{n.get('VpcId')}/{n.get('SubnetId')}",
                    mac=n.get("MacAddress"), ipv4=n.get("PrivateIpAddress"), model=nic_model)
            for n in sorted(instance.get("NetworkInterfaces") or [], key=lambda n: (n.get("Attachment") or {}).get("DeviceIndex", 0))]
    boot_mode = (instance.get("CurrentInstanceBootMode") or instance.get("BootMode") or "legacy-bios").lower()
    spec = VmSpec(
        name=name, source_id=iid, cpu=CpuSpec(sockets=1, cores=max(1, cores)),
        memory_bytes=int((itype.get("MemoryInfo") or {}).get("SizeInMiB") or 1024) << 20, disks=disks, nics=nics,
        firmware=Firmware.UEFI if boot_mode == "uefi" else Firmware.BIOS, guest_os=guest_os(instance),
        extra={"aws": {"instance_type": instance.get("InstanceType"), "nitro": nitro,
                       "az": (instance.get("Placement") or {}).get("AvailabilityZone"), "source_platform": "aws"}})
    spec.validate()
    return spec


def blocks_to_ranges(indices: list[int], block_size: int, size: int) -> list[ByteRange]:
    out = []
    for i in indices:
        start = i * block_size
        if start < size:
            out.append(ByteRange(start, min(block_size, size - start)))
    return normalize(out)
