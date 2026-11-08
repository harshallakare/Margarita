"""Purpose: in-memory FakeSource and file-staged FakeTarget implementing the full provider contracts.
They exist to prove the engine end to end (tests, demos) and to model the failure modes the real
platforms have (e.g. an enumeration path that caps at 10 disks, like Coriolis's Nutanix provider).

Usage (config):
    sources:
      demo: {type: fake, vms: [{name: vm1, disks: [{size: 64MiB, boot: true}, {size: 32MiB}]}],
             secondary_enumeration_cap: null}      # set to 10 to reproduce the Coriolis bug
    targets:
      sink: {type: fake, staging_dir: /tmp/sink, fail_commit_disk: null, persist: false}
                # persist: true keeps the disk/VM registry in <staging_dir>/registry.json, so the
                # fake behaves like a real platform across worker processes (platform tests, demos)

Disk content is deterministic pseudo-random data with every other 4 MiB block zeroed, so zero-skip,
resume and checksum verification are all exercised.

Replica simulation (source): `generation: N` advances the disks N "change generations"; each
generation rewrites ~1/8 of the 64 KiB blocks. A snapshot captures the generation it was taken at.
`cbt: true` makes changed_regions() report exactly the blocks changed between two snapshots
(like Nutanix CRT / VMware CBT); without it the engine falls back to full read + compare.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...config.loader import parse_size
from ...core.context import RunContext
from ...core.errors import LiveDiskReadRefused, ProviderError
from ...core.fileio import RawFileReader, RawFileWriter
from ...core.interfaces import (
    BootCheck,
    CheckpointRef,
    DiskHints,
    DiskReader,
    DiskWriter,
    SnapshotRef,
    SourceProvider,
    TargetDiskRef,
    TargetProvider,
    VmRef,
)
from ...core.ranges import ByteRange, normalize
from ...ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec

_BLOCK = 64 << 10
_ZERO_STRIDE = 4 << 20


def _changed_in(seed: bytes, i: int, gen: int) -> bool:
    return hashlib.sha256(seed + b"chg" + i.to_bytes(8, "little") + gen.to_bytes(4, "little")).digest()[0] < 32


class PatternDisk(DiskReader):
    def __init__(self, seed: str, size: int, *, fail_at: int | None = None, generation: int = 0):
        self.seed = seed.encode()
        self._size = size
        self.fail_at = fail_at
        self.reads = 0
        self.generation = generation

    def changed_blocks(self, base: int, cur: int) -> list[int]:
        nblocks = -(-self._size // _BLOCK)
        return [i for i in range(nblocks) if any(_changed_in(self.seed, i, g) for g in range(base + 1, cur + 1))]

    @property
    def size(self) -> int:
        return self._size

    def _block(self, i: int) -> bytes:
        last = next((g for g in range(self.generation, 0, -1) if _changed_in(self.seed, i, g)), 0)
        if last:
            h = hashlib.sha256(self.seed + i.to_bytes(8, "little") + b"g" + last.to_bytes(4, "little")).digest()
            return (h * (_BLOCK // len(h)))[:_BLOCK]
        if ((i * _BLOCK) // _ZERO_STRIDE) % 2 == 1:
            return bytes(_BLOCK)
        h = hashlib.sha256(self.seed + i.to_bytes(8, "little")).digest()
        return (h * (_BLOCK // len(h)))[:_BLOCK]

    def read(self, offset: int, length: int) -> bytes:
        if self.fail_at is not None and offset >= self.fail_at:
            raise IOError(f"injected read failure at {offset}")
        self.reads += 1
        length = max(0, min(length, self._size - offset))
        first, last = offset // _BLOCK, (offset + length - 1) // _BLOCK if length else offset // _BLOCK
        buf = b"".join(self._block(i) for i in range(first, last + 1))
        start = offset - first * _BLOCK
        return buf[start:start + length]


def _gen_vm(i: int, v: Mapping[str, Any]) -> VmSpec:
    disks = []
    for n, d in enumerate(v.get("disks", [])):
        disks.append(DiskSpec(
            id=f"{v['name']}-d{n}", bus=DiskBus(d.get("bus", "scsi")), size_bytes=parse_size(d.get("size", "16MiB")),
            order=n, is_boot=bool(d.get("boot", n == 0)), bus_index=d.get("index", n),
            controller=d.get("controller", v.get("controller", "virtio-scsi")),
            source_handle={"seed": f"shared:{d['shared']}" if d.get("shared") else f"{v['name']}:{n}"},
            shared_id=d.get("shared"),
        ))
    return VmSpec(
        name=v["name"], source_id=f"fake-{i}", cpu=CpuSpec(v.get("sockets", 1), v.get("cores", 2)),
        memory_bytes=parse_size(v.get("memory", "2GiB")), disks=disks,
        nics=[NicSpec(id=f"nic{k}", network=n) for k, n in enumerate(v.get("networks", ["net0"]))],
        firmware=Firmware(v.get("firmware", "bios")),
        guest_os=GuestOS(family=OsFamily(v.get("os", "linux"))),
    )


class FakeSource(SourceProvider):
    type_name = "fake"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext):
        super().__init__(name, config, ctx)
        self.vms = {s.name: s for s in (_gen_vm(i, v) for i, v in enumerate(config.get("vms", [])))}
        self.cap = config.get("secondary_enumeration_cap")
        self.fail_read_at = config.get("fail_read_at")
        self.generation = int(config.get("generation", 0))
        self.cbt = bool(config.get("cbt", False))
        self.snapshots: dict[str, SnapshotRef] = {}
        self.cleaned: list[str] = []

    def check_connection(self) -> list[str]:
        return [str(self.config["fail_connection"])] if self.config.get("fail_connection") else []

    def list_vms(self) -> list[VmRef]:
        return [VmRef(s.source_id, s.name, self.name) for s in self.vms.values()]

    def shutdown_vm(self, vm: VmRef, *, force: bool = False, timeout: int = 900) -> str:
        self.ctx.require_execute("fake shutdown", vm=vm.name, force=force)
        if self.config.get("fail_shutdown"):
            raise ProviderError(str(self.config["fail_shutdown"]))
        self.powered_off = getattr(self, "powered_off", [])
        self.powered_off.append((vm.name, force))
        return f"{vm.name} powered off ({'hard' if force else 'guest shutdown'})"

    def find_vm(self, name_or_id: str) -> VmRef:
        for r in self.list_vms():
            if name_or_id in (r.id, r.name):
                return r
        raise ProviderError(f"fake: no VM {name_or_id!r}")

    def inspect_vm(self, vm: VmRef) -> VmSpec:
        return VmSpec.from_dict(self.vms[vm.name].to_dict())

    def disk_count_crosscheck(self, vm: VmRef) -> dict[str, int]:
        n = len(self.vms[vm.name].disks)
        secondary = min(n, self.cap) if self.cap is not None else n
        return {"fake.primary": n, "fake.secondary": secondary}

    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
        self.ctx.require_execute("fake snapshot", vm=vm.name)
        s = SnapshotRef(id=f"snap-{vm.name}-{self.ctx.run_id}", vm=vm, app_consistent=app_consistent,
                        data={"generation": self.generation})
        self.snapshots[s.id] = s
        return s

    def snapshot_disk_count(self, snapshot: SnapshotRef) -> int | None:
        return len(self.vms[snapshot.vm.name].disks)

    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        if not snapshot.id:
            raise LiveDiskReadRefused("fake: snapshot required")
        r = PatternDisk(disk.source_handle["seed"], disk.size_bytes, fail_at=self.fail_read_at,
                        generation=int(snapshot.data.get("generation", 0)))
        delay = float(self.config.get("read_delay") or 0)     # demos: make copies slow enough to watch
        if delay > 0:
            import time as _time
            inner_read = r.read

            def slow_read(offset: int, length: int) -> bytes:
                _time.sleep(delay)
                return inner_read(offset, length)
            r.read = slow_read  # type: ignore[method-assign]
        return r

    def supports_cbt(self) -> bool:
        return self.cbt

    def changed_regions(self, disk: DiskSpec, base: CheckpointRef, cur: CheckpointRef) -> list[ByteRange]:
        if not self.cbt:
            raise NotImplementedError("fake: cbt disabled")
        pd = PatternDisk(disk.source_handle["seed"], disk.size_bytes)
        blocks = pd.changed_blocks(int(base.data.get("generation", 0)), int(cur.data.get("generation", 0)))
        return normalize(ByteRange(i * _BLOCK, min(_BLOCK, disk.size_bytes - i * _BLOCK)) for i in blocks)

    def cleanup(self, snapshot: SnapshotRef) -> None:
        self.ctx.require_execute("fake snapshot delete", id=snapshot.id)
        self.snapshots.pop(snapshot.id, None)
        self.cleaned.append(snapshot.id)


class FakeTarget(TargetProvider):
    type_name = "fake"
    platform_family = "kvm"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext):
        super().__init__(name, config, ctx)
        self.staging = Path(config.get("staging_dir", "./fake-target"))
        self.disks: dict[str, TargetDiskRef] = {}
        self.vms: dict[str, dict[str, Any]] = {}
        self.fail_commit = config.get("fail_commit_disk")
        self.boot_ok = config.get("boot_ok", True)
        self.calls: list[str] = []
        self.persist = bool(config.get("persist", False))
        if self.persist and self._registry().exists():
            reg = json.loads(self._registry().read_text())
            self.disks = {k: TargetDiskRef(**v) for k, v in reg["disks"].items()}
            self.vms = reg["vms"]

    def _registry(self) -> Path:
        return self.staging / "registry.json"

    def _save(self) -> None:
        if not self.persist:
            return
        self.staging.mkdir(parents=True, exist_ok=True)
        disks = {k: {"id": v.id, "name": v.name, "size_bytes": v.size_bytes, "data": dict(v.data)}
                 for k, v in self.disks.items()}
        vms = {k: {kk: vv for kk, vv in v.items() if kk != "spec"} for k, v in self.vms.items()}
        self._registry().write_text(json.dumps({"disks": disks, "vms": vms}, indent=1))

    def check_connection(self) -> list[str]:
        return [str(self.config["fail_connection"])] if self.config.get("fail_connection") else []

    def supports_shared_disks(self) -> tuple[bool, str]:
        return (False, "fake: shared disks disabled") if self.config.get("no_shared") else (True, "")

    def list_networks(self) -> list[dict[str, Any]]:
        return [{"id": f"net-{n}", "name": n} for n in self.config.get("networks", ["prod-net", "backup-net"])]

    def list_storage(self) -> list[dict[str, Any]]:
        return [{"id": s, "name": s} for s in self.config.get("storage", ["vmds-ssd", "vmds-hdd"])]

    def list_flavors(self) -> list[dict[str, Any]]:
        return [{"id": "f-2x4", "name": "small", "vcpus": 2, "ram_mb": 4096},
                {"id": "f-4x16", "name": "large", "vcpus": 4, "ram_mb": 16384}]

    def list_images(self) -> list[dict[str, Any]]:
        if self.config.get("images_need_password") and not self.config.get("password"):
            raise ProviderError("fake: not authenticated (no password)")
        return [{"id": f"img-{n}", "name": n} for n in self.config.get("images", ["ubuntu-24.04", "rocky-9"])]

    def _staged(self, ref: TargetDiskRef) -> Path:
        return self.staging / "staging" / f"{ref.name}.raw"

    def _final(self, ref: TargetDiskRef) -> Path:
        return self.staging / "volumes" / f"{ref.name}.img"

    def create_disk(self, name: str, size_bytes: int, hints: DiskHints) -> TargetDiskRef:
        self.ctx.require_execute("fake create disk", name=name)
        self.calls.append(f"create_disk:{name}")
        ref = TargetDiskRef(id=f"vol-{name}", name=name, size_bytes=size_bytes,
                            data={"bus": hints.bus.value, **({"shared": True} if hints.shared else {})})
        self.disks[name] = ref
        self._save()
        return ref

    def find_disk(self, name: str) -> TargetDiskRef | None:
        return self.disks.get(name)

    def open_disk_writer(self, disk: TargetDiskRef, *, resume: bool = False, incremental: bool = False) -> DiskWriter:
        self.ctx.require_execute("fake open writer", disk=disk.name)
        p = self._staged(disk)
        if incremental:
            # start from the committed replica content; unwritten regions are NOT zero
            if not p.exists() and self._final(disk).exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self._final(disk), p)
            w = RawFileWriter(p, disk.size_bytes)
            w.prezeroed = False
            return w
        if not resume and p.exists():
            p.unlink()
        return RawFileWriter(p, disk.size_bytes)

    # ---- replica support
    def supports_replica(self) -> bool:
        return True

    def supports_restore_points(self) -> tuple[bool, str]:
        return True, "file copies of replica volumes"

    def _rp(self, disk: TargetDiskRef, sid: str) -> Path:
        return self.staging / "restore-points" / f"{disk.name}@{sid}.img"

    def snapshot_disk(self, disk: TargetDiskRef, name: str) -> str:
        self.ctx.require_execute("fake snapshot disk", disk=disk.name, name=name)
        dst = self._rp(disk, name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._final(disk), dst)
        return name

    def delete_disk_snapshot(self, disk: TargetDiskRef, snapshot_id: str) -> None:
        self.ctx.require_execute("fake delete disk snapshot", disk=disk.name, snapshot=snapshot_id)
        self._rp(disk, snapshot_id).unlink(missing_ok=True)

    def clone_disk(self, disk: TargetDiskRef, name: str, restore_point: str | None = None) -> TargetDiskRef:
        self.ctx.require_execute("fake clone disk", disk=disk.name, name=name, restore_point=restore_point)
        src = self._rp(disk, restore_point) if restore_point else self._final(disk)
        if not src.exists():
            raise ProviderError(f"fake: nothing to clone at {src}")
        ref = TargetDiskRef(id=f"vol-{name}", name=name, size_bytes=disk.size_bytes, data=dict(disk.data))
        dst = self._final(ref)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        self.disks[name] = ref
        self._save()
        return ref

    def delete_disk(self, disk: TargetDiskRef) -> None:
        self.ctx.require_execute("fake delete disk", disk=disk.name)
        self._final(disk).unlink(missing_ok=True)
        self._staged(disk).unlink(missing_ok=True)
        for rp in (self.staging / "restore-points").glob(f"{disk.name}@*.img"):
            rp.unlink()
        self.disks.pop(disk.name, None)
        self._save()

    def commit_disk(self, disk: TargetDiskRef, writer_staged: Path | None) -> None:
        self.ctx.require_execute("fake commit", disk=disk.name)
        if self.fail_commit == disk.name:
            raise ProviderError(f"injected commit failure for {disk.name}")
        assert writer_staged is not None
        dst = self._final(disk)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(writer_staged, dst)
        self.calls.append(f"commit:{disk.name}")

    def open_disk_reader(self, disk: TargetDiskRef) -> DiskReader | None:
        p = self._final(disk)
        return RawFileReader(p) if p.exists() else None

    def discard_staging(self, disk: TargetDiskRef) -> None:
        if not self.config.get("keep_staging", False):
            self._staged(disk).unlink(missing_ok=True)

    def morph_image(self, disk: TargetDiskRef) -> tuple[Path, str] | None:
        p = self._final(disk)
        return (p, "raw") if p.exists() else None

    def create_vm(self, spec: VmSpec, boot_disk: TargetDiskRef, data_disks: list[TargetDiskRef]) -> VmRef:
        self.ctx.require_execute("fake create vm", name=spec.name)
        self.calls.append(f"create_vm:{spec.name}")
        disks = [boot_disk.id] + ([d.id for d in data_disks] if self.config.get("attach_at_create") else [])
        self.vms[spec.name] = {"id": f"vm-{spec.name}", "disks": disks, "spec": spec,
                               "network_map": dict((spec.extra.get("target_options") or {}).get("network_map") or {})}
        self._save()
        return VmRef(f"vm-{spec.name}", spec.name, self.name)

    def find_vm(self, name: str) -> VmRef | None:
        v = self.vms.get(name)
        return VmRef(v["id"], name, self.name) if v else None

    def attach_disk(self, vm: VmRef, disk: TargetDiskRef, order: int) -> None:
        self.ctx.require_execute("fake attach", vm=vm.name, disk=disk.name)
        self.calls.append(f"attach:{disk.name}")
        self.vms[vm.name]["disks"].append(disk.id)
        self._save()

    def list_attached_disks(self, vm: VmRef) -> list[str]:
        return list(self.vms[vm.name]["disks"])

    def check_boot(self, vm: VmRef, timeout: float = 900, *, guest_os: str | None = None,
                   progress: Any = None) -> BootCheck:
        return BootCheck(ok=self.boot_ok, detail="fake boot " + ("ok" if self.boot_ok else "dropped to initramfs"))

    def cleanup_on_failure(self, created: Mapping[str, Any]) -> None:
        self.ctx.require_execute("fake cleanup")
        for d in created.get("target_disks", []):
            self.disks.pop(d["name"], None)
        if created.get("target_vm"):
            self.vms.pop(created["target_vm"]["name"], None)
        self.calls.append("cleanup")
        self._save()
