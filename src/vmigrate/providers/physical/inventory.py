"""Purpose: inventory of a physical Linux server (from sysfs/procfs, gathered by one remote Python
run) -> VmSpec, plus the pure planning for the two read modes.

Usage:
    inv = json.loads(shell.run(["python3", "-c", INVENTORY]))
    spec = to_vmspec(inv)
    problems = cold_problems(inv, disk_names)                       # cold mode: nothing mounted rw
    plan = lvm_plan(inv, disk_names, pvsegs, lvs, run_tag=..., cow_percent=10)   # lvm-snapshot mode

Disk enumeration is cross-checked two ways: /sys/block entries with a backing device (sysfs) and
`lsblk` whole disks (udev view); the engine fails loud when they disagree.
"""

from __future__ import annotations

import re
from typing import Any

from ...core.errors import SafetyGateError
from ...ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec

INVENTORY = r'''
import os, json, socket, re, subprocess
def rd(p, d=""):
    try:
        return open(p).read().strip()
    except Exception:
        return d
def ls(p):
    try:
        return sorted(os.listdir(p))
    except Exception:
        return []
out = {"hostname": socket.gethostname(), "efi": os.path.isdir("/sys/firmware/efi"), "cpus": os.cpu_count(),
       "mem_kb": int((re.search(r"MemTotal:\s+(\d+)", rd("/proc/meminfo")) or [0, 0])[1]),
       "os_release": rd("/etc/os-release"), "redhat_release": rd("/etc/redhat-release"), "disks": [], "dm": [],
       "mounts": [], "nics": []}
for n in ls("/sys/block"):
    b = "/sys/block/" + n
    if not os.path.exists(b + "/device") or n.startswith(("sr", "fd", "loop", "ram", "zram", "nbd")):
        continue
    real = os.path.realpath(b + "/device")
    tr = "nvme" if "/nvme" in real else "virtio" if "virtio" in real else "usb" if "/usb" in real else \
         "ata" if "/ata" in real else "scsi"
    d = {"name": n, "dev": rd(b + "/dev"), "size": int(rd(b + "/size", "0")) * 512, "removable": rd(b + "/removable") == "1",
         "model": rd(b + "/device/model"), "serial": rd(b + "/device/serial") or rd(b + "/serial"),
         "wwid": rd(b + "/device/wwid") or rd(b + "/wwid"), "transport": tr, "holders": ls(b + "/holders"), "parts": []}
    for p in ls(b):
        if os.path.exists(b + "/" + p + "/partition"):
            d["parts"].append({"name": p, "dev": rd(b + "/" + p + "/dev"), "start": int(rd(b + "/" + p + "/start", "0")) * 512,
                               "size": int(rd(b + "/" + p + "/size", "0")) * 512, "holders": ls(b + "/" + p + "/holders")})
    out["disks"].append(d)
for n in ls("/sys/block"):
    if n.startswith(("dm-", "md")):
        b = "/sys/block/" + n
        out["dm"].append({"name": n, "dev": rd(b + "/dev"), "dmname": rd(b + "/dm/name"), "uuid": rd(b + "/dm/uuid"),
                          "slaves": ls(b + "/slaves")})
for line in rd("/proc/self/mountinfo").splitlines():
    f = line.split()
    if " - " not in line:
        continue
    pre, post = line.split(" - ", 1)
    pf, qf = pre.split(), post.split()
    out["mounts"].append({"dev": pf[2], "mnt": pf[4], "opts": pf[5], "fstype": qf[0], "source": qf[1]})
for i in ls("/sys/class/net"):
    b = "/sys/class/net/" + i
    if not os.path.exists(b + "/device"):
        continue
    out["nics"].append({"name": i, "mac": rd(b + "/address"),
                        "driver": os.path.basename(os.path.realpath(b + "/device/driver"))})
try:
    ipo = subprocess.run(["ip", "-o", "-4", "addr", "show"], stdout=subprocess.PIPE, universal_newlines=True).stdout
    out["ipv4"] = [(l.split()[1], l.split()[3]) for l in ipo.splitlines() if len(l.split()) > 3]
except Exception:
    out["ipv4"] = []
try:
    lb = subprocess.run(["lsblk", "-d", "-n", "-r", "-o", "NAME,TYPE,RM"], stdout=subprocess.PIPE,
                        universal_newlines=True).stdout
    out["lsblk_disks"] = [l.split()[0] for l in lb.splitlines()
                          if len(l.split()) >= 3 and l.split()[1] == "disk" and l.split()[2] == "0"
                          and not l.split()[0].startswith(("loop", "ram", "zram", "sr", "nbd"))]
except Exception:
    out["lsblk_disks"] = None
print(json.dumps(out))
'''

_BUS = {"nvme": (DiskBus.NVME, "nvme"), "virtio": (DiskBus.VIRTIO, "virtio-blk"), "ata": (DiskBus.SATA, "ahci"),
        "scsi": (DiskBus.SCSI, None), "usb": (DiskBus.SCSI, None)}


def disks_of(inv: dict[str, Any], *, include_removable: bool = False) -> list[dict[str, Any]]:
    return [d for d in inv["disks"] if d["size"] > 0 and (include_removable or not d["removable"])
            and d["transport"] != "usb"]


def _devs_of_disk(d: dict[str, Any]) -> set[str]:
    return {d["dev"], *(p["dev"] for p in d["parts"])}


def _dm_slaves(inv: dict[str, Any]) -> dict[str, set[str]]:
    """dm/md device maj:min -> kernel names it sits on (recursively)."""
    by_name = {x["name"]: x for x in inv.get("dm", [])}

    def walk(n: str, seen: set[str]) -> set[str]:
        if n in seen:
            return set()
        seen.add(n)
        x = by_name.get(n)
        if x is None:
            return {n}
        out: set[str] = set()
        for s in x["slaves"]:
            out |= walk(s, seen)
        return out
    return {x["dev"]: walk(x["name"], set()) for x in inv.get("dm", [])}


def mounts_on(inv: dict[str, Any], d: dict[str, Any]) -> list[dict[str, Any]]:
    """Mounts whose device is this disk, one of its partitions, or a dm/md device built on them."""
    names = {d["name"], *(p["name"] for p in d["parts"])}
    devs = _devs_of_disk(d)
    dm = _dm_slaves(inv)
    return [m for m in inv["mounts"] if m["dev"] in devs or (m["dev"] in dm and dm[m["dev"]] & names)]


def boot_disk(inv: dict[str, Any], disks: list[dict[str, Any]]) -> str | None:
    for want in ("/boot/efi", "/boot", "/"):
        for d in disks:
            if any(m["mnt"] == want for m in mounts_on(inv, d)):
                return d["name"]
    return disks[0]["name"] if disks else None


def disk_id(d: dict[str, Any]) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", d.get("wwid") or d.get("serial") or d["name"])[:120]


def to_vmspec(inv: dict[str, Any]) -> VmSpec:
    from ...osmorph.linux_guest import detect_os
    disks = disks_of(inv)
    boot = boot_disk(inv, disks)
    ordered = sorted(disks, key=lambda d: (d["name"] != boot, d["name"]))
    specs = []
    for i, d in enumerate(ordered):
        bus, ctrl = _BUS.get(d["transport"], (DiskBus.SCSI, None))
        specs.append(DiskSpec(id=disk_id(d), bus=bus, size_bytes=d["size"], order=i, is_boot=d["name"] == boot,
                              bus_index=i, label=f"/dev/{d['name']}", controller=ctrl,
                              source_handle={"kname": d["name"], "dev": d["dev"], "model": d.get("model"),
                                             "serial": d.get("serial")}))
    files = {k: inv[v] for k, v in (("/etc/os-release", "os_release"), ("/etc/redhat-release", "redhat_release"))
             if inv.get(v)}
    info = detect_os(files)
    ips = dict(inv.get("ipv4") or [])
    nics = [NicSpec(id=n["name"], network=n["name"], mac=n["mac"].lower(), ipv4=(ips.get(n["name"]) or "").split("/")[0]
                    or None, model=n.get("driver") or None) for n in inv.get("nics", [])]
    cpus = int(inv.get("cpus") or 1)
    return VmSpec(name=inv["hostname"], source_id=inv["hostname"], cpu=CpuSpec(1, cpus),
                  memory_bytes=int(inv.get("mem_kb") or 0) * 1024, disks=specs, nics=nics,
                  firmware=Firmware.UEFI if inv.get("efi") else Firmware.BIOS,
                  guest_os=GuestOS(family=OsFamily.LINUX, distro=info.distro, version=info.version),
                  extra={"physical": {"transports": sorted({d["transport"] for d in disks})}})


def crosscheck(inv: dict[str, Any]) -> dict[str, int]:
    out = {"sysfs": len(disks_of(inv))}
    if inv.get("lsblk_disks") is not None:
        rem = {d["name"] for d in inv["disks"] if d["transport"] == "usb"}
        out["lsblk"] = len([n for n in inv["lsblk_disks"] if n not in rem])
    return out


# --------------------------------------------------------------------------- cold mode
def cold_problems(inv: dict[str, Any], names: list[str]) -> list[str]:
    """Cold mode reads raw devices: nothing on them may be mounted read-write (boot a rescue ISO)."""
    by = {d["name"]: d for d in inv["disks"]}
    out = []
    for n in names:
        rw = [m for m in mounts_on(inv, by[n]) if m["opts"].split(",")[0] == "rw"]
        if rw:
            out.append(f"/dev/{n} is in use: {', '.join(m['mnt'] for m in rw)} mounted read-write. Boot the server "
                       "from a rescue/live ISO for a cold copy, or use mode lvm-snapshot")
    return out


# --------------------------------------------------------------------------- lvm-snapshot mode
def lvm_plan(inv: dict[str, Any], names: list[str], pvsegs: list[dict[str, Any]], lvs: list[dict[str, Any]], *,
             run_tag: str, cow_percent: float = 10.0, remount_ro: bool = True) -> dict[str, Any]:
    """Decide what to snapshot and how each disk image is composed.

    pvsegs: `pvs --segments --units b --nosuffix -o pv_name,pe_start,vg_extent_size,pv_mda_count,pvseg_start,
             pvseg_size,lv_name,vg_name,seg_start,vg_free` rows; lvs: `lvs ... -o lv_name,vg_name,lv_size,segtype`.
    Returns {snapshots: [{vg, lv, name, cow_bytes}], remount: [mnt], headers: [{pv, file, bytes}],
             disks: {kname: {base, size, overlays}}}. Raises SafetyGateError when the layout cannot be
    copied consistently this way (then cold mode is the answer)."""
    by = {d["name"]: d for d in inv["disks"]}
    part_of: dict[str, tuple[str, int]] = {}         # /dev/<kname> -> (disk, offset)
    for n in names:
        d = by[n]
        part_of[f"/dev/{n}"] = (n, 0)
        for p in d["parts"]:
            part_of[f"/dev/{p['name']}"] = (n, p["start"])
    lv_info = {(x["vg_name"], x["lv_name"]): x for x in lvs}
    mounted_dm = {m["source"] for m in inv["mounts"]}
    errs: list[str] = []

    def is_mounted_lv(vg: str, lv: str) -> bool:
        cands = {f"/dev/mapper/{vg.replace('-', '--')}-{lv.replace('-', '--')}", f"/dev/{vg}/{lv}"}
        return bool(cands & mounted_dm)

    snaps: dict[tuple[str, str], dict[str, Any]] = {}
    headers: dict[str, dict[str, Any]] = {}
    overlays: dict[str, list[list[Any]]] = {n: [] for n in names}
    vg_free: dict[str, int] = {}
    for s in pvsegs:
        pv = s["pv_name"]
        if pv not in part_of:
            if any(s.get("vg_name") and is_mounted_lv(s["vg_name"], x[1]) for x in lv_info if x[0] == s.get("vg_name")):
                errs.append(f"{pv} (VG {s['vg_name']}) is not a plain partition/disk of the migrated disks "
                            "(md/multipath/other disk): not supported in lvm-snapshot mode")
            continue
        disk, poff = part_of[pv]
        vg_free[s["vg_name"]] = int(s.get("vg_free") or 0)
        if int(s.get("pv_mda_count") or 1) > 1:
            errs.append(f"{pv}: {s['pv_mda_count']} LVM metadata copies; only the first is preserved — not supported")
        if pv not in headers:
            hb = int(s["pe_start"])
            headers[pv] = {"pv": pv, "file": f"/run/vmigrate/{run_tag}-{pv.rsplit('/', 1)[1]}.hdr", "bytes": hb}
            overlays[disk].append([poff, hb, headers[pv]["file"], 0])
        lv = s.get("lv_name") or ""
        if not lv:
            continue                                   # free extents
        vg = s["vg_name"]
        info = lv_info.get((vg, lv), {})
        if not is_mounted_lv(vg, lv):
            continue                                   # not in use (swap, unmounted): read as-is
        if str(info.get("segtype", "linear")) not in ("linear", "striped"):
            errs.append(f"{vg}/{lv}: segment type {info.get('segtype')} (thin/raid/cache) is not supported in "
                        "lvm-snapshot mode")
            continue
        ext = int(s["vg_extent_size"])
        key = (vg, lv)
        if key not in snaps:
            size = int(info.get("lv_size") or 0)
            cow = max(1 << 30, int(size * cow_percent / 100) // ext * ext)
            snaps[key] = {"vg": vg, "lv": lv, "name": f"{run_tag}-{lv}"[:120], "cow_bytes": cow}
        snap_path = f"/dev/{vg}/{snaps[key]['name']}"
        overlays[disk].append([poff + int(s["pe_start"]) + int(s["pvseg_start"]) * ext, int(s["pvseg_size"]) * ext,
                               snap_path, int(s["seg_start"])])
    need: dict[str, int] = {}
    for sn in snaps.values():
        need[sn["vg"]] = need.get(sn["vg"], 0) + sn["cow_bytes"]
    for vg, n in need.items():
        if n > vg_free.get(vg, 0):
            errs.append(f"VG {vg}: snapshots need {n >> 20} MiB free, only {vg_free.get(vg, 0) >> 20} MiB free "
                        "(add space, lower osmorph.cow_percent, or use cold mode)")
    remount: list[str] = []
    dm_by_dev = {x["dev"]: x for x in inv.get("dm", [])}
    for n in names:
        for m in mounts_on(inv, by[n]):
            if m["opts"].split(",")[0] != "rw":
                continue                              # ro mounts cannot change
            x = dm_by_dev.get(m["dev"])
            if x is not None:
                if not str(x.get("uuid", "")).startswith("LVM-"):
                    errs.append(f"{m['mnt']} is on {x['name']} ({x.get('dmname') or 'md/crypt'}), not a plain LVM "
                                "volume: not supported in lvm-snapshot mode; use cold mode")
                continue                              # LVs are snapshotted
            if m["mnt"] == "/":
                errs.append("the root filesystem is on a plain partition (not LVM): it cannot be frozen for the "
                            "copy; use cold mode")
            elif not remount_ro:
                errs.append(f"{m['mnt']} (plain partition) is mounted read-write; allow remount_ro or use cold mode")
            else:
                remount.append(m["mnt"])
    if errs:
        raise SafetyGateError("lvm-snapshot mode cannot copy this server consistently:\n  - " + "\n  - ".join(errs))
    return {"snapshots": list(snaps.values()), "remount": sorted(set(remount)), "headers": list(headers.values()),
            "disks": {n: {"base": f"/dev/{n}", "size": by[n]["size"], "overlays": overlays[n]} for n in names}}
