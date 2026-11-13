"""Purpose: PURE mapping from a libvirt domain XML (virsh dumpxml) to the IR, the two-method disk
cross-check, and the XML documents Margarita hands to libvirt (pull-mode backup, checkpoint). No I/O.

Usage:
    disks = domain_disks(xml)                 # [{target, bus, type, format, source, shareable, ...}]
    spec = to_vmspec(xml, capacities)         # capacities: {target: bytes} from virsh domblkinfo
    crosscheck(xml, domblklist_text)          # {"libvirt.dumpxml": n, "libvirt.domblklist": m}
    backup_xml(disks, port, scratch_dir, tag, incremental=None)
    checkpoint_xml(name, disks)

Rules:
  * every <disk device="disk"> counts (cdrom / floppy / lun pass-through are not data disks: a
    device="lun" pass-through is REFUSED, because it is a host LUN that no snapshot covers)
  * controller: bus virtio -> virtio-blk; bus scsi -> the scsi <controller> model (virtio-scsi,
    lsilogic, ...); sata -> ahci; ide -> ide. virtio guests need no driver injection on KVM targets.
  * firmware: <os firmware="efi"> or a pflash <loader> -> UEFI; secure boot from the loader / feature
  * names that reach a shell command (domain, target, file paths) are validated by safe()
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from ...ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec

SAFE = re.compile(r"^[A-Za-z0-9_./:@+=,-]+$")
_BUS = {"virtio": DiskBus.VIRTIO, "scsi": DiskBus.SCSI, "sata": DiskBus.SATA, "ide": DiskBus.IDE,
        "nvme": DiskBus.NVME, "usb": DiskBus.SCSI}
_OS = (("windows", OsFamily.WINDOWS, "windows"), ("microsoft.com/win", OsFamily.WINDOWS, "windows"),
       ("ubuntu", OsFamily.LINUX, "ubuntu"), ("debian", OsFamily.LINUX, "debian"), ("rhel", OsFamily.LINUX, "rhel"),
       ("redhat", OsFamily.LINUX, "rhel"), ("centos", OsFamily.LINUX, "centos"), ("rocky", OsFamily.LINUX, "rocky"),
       ("alma", OsFamily.LINUX, "almalinux"), ("fedora", OsFamily.LINUX, "fedora"), ("suse", OsFamily.LINUX, "sles"),
       ("oracle", OsFamily.LINUX, "ol"), ("linux", OsFamily.LINUX, None))


class LibvirtMappingError(ValueError):
    pass


def safe(name: str, what: str) -> str:
    if not name or not SAFE.match(name) or ".." in name:
        raise LibvirtMappingError(f"refusing unsafe {what}: {name!r}")
    return name


def _root(xml: str | ET.Element) -> ET.Element:
    return xml if isinstance(xml, ET.Element) else ET.fromstring(xml)


def domain_disks(xml: str | ET.Element) -> list[dict[str, Any]]:
    root = _root(xml)
    out = []
    for d in root.findall("./devices/disk"):
        device = d.get("device", "disk")
        if device in ("cdrom", "floppy"):
            continue
        tgt = d.find("target")
        src = d.find("source")
        drv = d.find("driver")
        boot = d.find("boot")
        src_attrs = dict(src.attrib) if src is not None else {}
        out.append({
            "target": (tgt.get("dev") if tgt is not None else "") or "",
            "bus": (tgt.get("bus") if tgt is not None else "") or "virtio",
            "device": device,
            "type": d.get("type", "file"),
            "format": (drv.get("type") if drv is not None else None) or "raw",
            "source": src_attrs.get("file") or src_attrs.get("dev") or src_attrs.get("name") or "",
            "protocol": src_attrs.get("protocol"),
            "pool": src_attrs.get("pool"), "volume": src_attrs.get("volume"),
            "shareable": d.find("shareable") is not None,
            "readonly": d.find("readonly") is not None,
            "boot_order": int(boot.get("order")) if boot is not None and boot.get("order", "").isdigit() else None,
            "serial": d.findtext("serial"),
            "address": dict(d.find("address").attrib) if d.find("address") is not None else {},
        })
    return out


def crosscheck(xml: str | ET.Element, domblklist: str) -> dict[str, int]:
    """`virsh domblklist --details` columns: Type Device Target Source."""
    n = 0
    for line in domblklist.splitlines():
        cols = line.split()
        if len(cols) >= 3 and cols[1] in ("disk", "lun"):
            n += 1
    return {"libvirt.dumpxml": len(domain_disks(xml)), "libvirt.domblklist": n}


def _controller_model(root: ET.Element, bus: str, addr: dict[str, str]) -> str | None:
    if bus == "virtio":
        return "virtio-blk"
    if bus == "sata":
        return "ahci"
    if bus == "ide":
        return "ide"
    if bus == "nvme":
        return "nvme"
    if bus == "scsi":
        idx = addr.get("controller", "0")
        for c in root.findall("./devices/controller[@type='scsi']"):
            if c.get("index", "0") == idx:
                return c.get("model") or "lsilogic"      # libvirt's default scsi model
        return "lsilogic"
    return None


def guest_os(root: ET.Element) -> GuestOS:
    os_id = ""
    for el in root.iter():
        if el.tag.endswith("}os") and el.get("id"):
            os_id = el.get("id", "").lower()          # libosinfo metadata
    for key, fam, distro in _OS:
        if key in os_id:
            ver = os_id.rsplit("/", 1)[-1] if "/" in os_id else None
            return GuestOS(fam, distro=distro, version=ver if ver and ver[0].isdigit() else None)
    return GuestOS(OsFamily.UNKNOWN)


def _mem_bytes(el: ET.Element | None) -> int:
    if el is None or not (el.text or "").strip():
        return 1 << 30
    unit = (el.get("unit") or "KiB").lower()
    mult = {"b": 1, "bytes": 1, "k": 1 << 10, "kib": 1 << 10, "kb": 1000, "m": 1 << 20, "mib": 1 << 20, "mb": 10 ** 6,
            "g": 1 << 30, "gib": 1 << 30, "gb": 10 ** 9, "t": 1 << 40, "tib": 1 << 40}.get(unit, 1 << 10)
    return int(el.text.strip()) * mult


def to_vmspec(xml: str | ET.Element, capacities: dict[str, int], *, state: str = "unknown") -> VmSpec:
    root = _root(xml)
    name = root.findtext("name") or "?"
    raw = domain_disks(root)
    if not raw:
        raise LibvirtMappingError(f"{name}: no disks")
    for d in raw:
        where = f"{name}: disk {d['target'] or '?'}"
        if d["device"] == "lun":
            raise LibvirtMappingError(f"{where} is a pass-through LUN (device='lun'); no snapshot covers it")
        if not d["target"]:
            raise LibvirtMappingError(f"{where} has no target device")
        safe(d["target"], "disk target")
        if d["target"] not in capacities or capacities[d["target"]] <= 0:
            raise LibvirtMappingError(f"{where}: capacity unknown (virsh domblkinfo)")
    # boot disk: lowest <boot order>, else the first disk in XML order (libvirt's own default)
    boots = [d for d in raw if d["boot_order"] is not None]
    boot_target = min(boots, key=lambda d: d["boot_order"])["target"] if boots else raw[0]["target"]
    os_el = root.find("os")
    loader = os_el.find("loader") if os_el is not None else None
    uefi = (os_el is not None and os_el.get("firmware") == "efi") or (loader is not None and loader.get("type") == "pflash")
    secure = bool(loader is not None and loader.get("secure") == "yes")
    if os_el is not None:
        for f in os_el.findall("./firmware/feature"):
            if f.get("name") == "secure-boot" and f.get("enabled") == "yes":
                secure = True
    disks = []
    for i, d in enumerate(raw):
        disks.append(DiskSpec(
            id=d["target"], bus=_BUS.get(d["bus"], DiskBus.VIRTIO), size_bytes=int(capacities[d["target"]]), order=i,
            is_boot=d["target"] == boot_target, bus_index=i, label=d["target"],
            controller=_controller_model(root, d["bus"], d["address"]),
            source_handle={k: d[k] for k in ("target", "type", "format", "source", "protocol", "pool", "volume",
                                             "readonly")},
            shared_id=f"{d['type']}:{d['source']}" if d["shareable"] else None))
    vcpu = int((root.findtext("vcpu") or "1").strip() or 1)
    topo = root.find("./cpu/topology")
    if topo is not None:
        sockets = int(topo.get("sockets", "1"))
        cpu = CpuSpec(sockets=sockets, cores=max(1, vcpu // max(1, sockets)))
    else:
        cpu = CpuSpec(sockets=1, cores=max(1, vcpu))
    nics = []
    for k, n in enumerate(root.findall("./devices/interface")):
        src = n.find("source")
        net = ""
        if src is not None:
            net = src.get("network") or src.get("bridge") or src.get("dev") or ""
        mac = n.find("mac")
        model = n.find("model")
        nics.append(NicSpec(id=f"net{k}", network=net or n.get("type", ""), mac=mac.get("address") if mac is not None else None,
                            model=model.get("type") if model is not None else None))
    otype = os_el.find("type") if os_el is not None else None
    spec = VmSpec(name=name, source_id=root.findtext("uuid") or name, cpu=cpu, memory_bytes=_mem_bytes(root.find("memory")),
                  disks=disks, nics=nics, firmware=Firmware.UEFI if uefi else Firmware.BIOS, secure_boot=secure,
                  guest_os=guest_os(root), machine_type=otype.get("machine") if otype is not None else None,
                  extra={"libvirt": {"state": state, "source_platform": "kvm"}})
    spec.validate()
    return spec


def backup_xml(disks: list[dict[str, Any]], port: int, scratch_dir: str, tag: str, incremental: str | None = None) -> str:
    """Pull-mode backup: libvirt asks QEMU to serve a point-in-time view of every disk over NBD on
    127.0.0.1:<port> (export name = disk target); writes the guest makes meanwhile copy the old data
    to a scratch file first. With `incremental`, each export also carries the dirty bitmap
    qemu:dirty-bitmap:backup-<target> of changes since that checkpoint."""
    safe(tag, "tag")
    safe(scratch_dir, "scratch directory")
    root = ET.Element("domainbackup", mode="pull")
    if incremental:
        ET.SubElement(root, "incremental").text = safe(incremental, "checkpoint")
    ET.SubElement(root, "server", transport="tcp", name="127.0.0.1", port=str(int(port)))
    el = ET.SubElement(root, "disks")
    for d in disks:
        t = safe(d["target"], "disk target")
        if d.get("readonly"):
            ET.SubElement(el, "disk", name=t, backup="no")
            continue
        de = ET.SubElement(el, "disk", name=t, backup="yes", type="file")
        ET.SubElement(de, "scratch", file=f"{scratch_dir.rstrip('/')}/{tag}-{t}.scratch")
    return ET.tostring(root, encoding="unicode")


def checkpoint_xml(name: str, disks: list[dict[str, Any]]) -> str:
    root = ET.Element("domaincheckpoint")
    ET.SubElement(root, "name").text = safe(name, "checkpoint")
    el = ET.SubElement(root, "disks")
    for d in disks:
        ET.SubElement(el, "disk", name=safe(d["target"], "disk target"),
                      checkpoint="no" if d.get("readonly") else "bitmap")
    return ET.tostring(root, encoding="unicode")


def offline_plan(handle: dict[str, Any]) -> dict[str, Any]:
    """qemu-nbd exposure of a SHUT-OFF domain's disk. No --force-share: qemu-nbd takes the image
    lock, so libvirt cannot start the VM (and write to the disk) while it is being read."""
    if handle["type"] == "file":
        return {"setup": [], "nbd_target": safe(handle["source"], "disk path"), "nbd_format": _fmt(handle["format"]),
                "nbd_extra": [], "teardown": []}
    if handle["type"] == "block":
        return {"setup": [], "nbd_target": safe(handle["source"], "device path"), "nbd_format": "raw",
                "nbd_extra": [], "teardown": []}
    raise LibvirtMappingError(f"disk {handle['target']}: {handle['type']} ({handle.get('protocol') or ''}) disks of a "
                              "shut-off VM cannot be read directly; start the VM (it is then read through a libvirt "
                              "backup) or move the disk to a file or block device")


def _fmt(f: str) -> str:
    if f not in ("raw", "qcow2", "vmdk", "vpc", "vhdx", "qed"):
        raise LibvirtMappingError(f"unsupported disk format {f!r}")
    return f


def domblkinfo_capacity(text: str) -> int:
    for line in text.splitlines():
        if line.strip().lower().startswith("capacity:"):
            return int(line.split(":", 1)[1].strip())
    raise LibvirtMappingError(f"no Capacity in domblkinfo output: {text[:200]!r}")

