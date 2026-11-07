"""Purpose: Hyper-V control over WinRM (PowerShell remoting). The ONLY module that imports pywinrm.
Every operation is one PowerShell script below; parameters travel as base64-encoded JSON (never
string-spliced into the script), results come back as JSON, so parsing is unit-testable and the
scripts are auditable in one place.

Usage:
    c = HypervClient("hv01.example.com", "DOMAIN\\\\migrate", "...", transport="ntlm", port=5986, use_ssl=True)
    c.list_vms(); c.vm_info(vm_id)
    snap = c.create_snapshot(vm_id, consistency=1)          # recovery snapshot (backup-style)
    c.destroy_snapshot(vm_id, snap["id"])
    # RCT (experimental, opt-in): convert_to_reference_point / changed / destroy_reference_point

Source-side changes are limited to recovery snapshots and (RCT) reference points this tool made.
The scripts need the Hyper-V PowerShell module and the root\\virtualization\\v2 WMI namespace
(Windows Server 2016+). RCT scripts are UNVERIFIED against a live host — hence opt-in.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from ...core.errors import ProviderError, TransientProviderError

PRELUDE = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$p = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PARAMS__')))
$ns = 'root\virtualization\v2'
function Wait-Job2($r, $what) {
  if ($r.ReturnValue -eq 4096) {
    $job = [wmi]$r.Job
    while ($job.JobState -eq 3 -or $job.JobState -eq 4) { Start-Sleep -Milliseconds 500; $job = [wmi]$r.Job }
    if ($job.JobState -ne 7) { throw "$what failed: $($job.ErrorDescription) (state $($job.JobState))" }
  } elseif ($r.ReturnValue -ne 0) { throw "$what returned $($r.ReturnValue)" }
}
function Get-WmiVm($id) {
  $v = Get-WmiObject -Namespace $ns -Class Msvm_ComputerSystem -Filter "Name='$id'"
  if (-not $v) { throw "no VM with id $id" }
  $v
}
"""

PING = r"""
[pscustomobject]@{ ps = $PSVersionTable.PSVersion.ToString(); hyperv = [bool](Get-Command Get-VM -ErrorAction SilentlyContinue);
  host = $env:COMPUTERNAME } | ConvertTo-Json -Compress
"""

LIST_VMS = r"""
@(Get-VM | ForEach-Object { [pscustomobject]@{ id = $_.Id.ToString(); name = $_.Name; state = $_.State.ToString();
  generation = $_.Generation } }) | ConvertTo-Json -Compress -Depth 3
"""

VM_INFO = r"""
$vm = Get-VM -Id $p.id
$disks = @(Get-VMHardDiskDrive -VM $vm | ForEach-Object {
  $vhd = $null
  if ($_.Path) { $vhd = Get-VHD -Path $_.Path -ErrorAction SilentlyContinue }
  [pscustomobject]@{ id = $_.Id; controller_type = $_.ControllerType.ToString(); controller_number = $_.ControllerNumber;
    controller_location = $_.ControllerLocation; path = $_.Path; disk_number = $_.DiskNumber;
    shared = [bool]$_.SupportPersistentReservations;
    format = $(if ($vhd) { $vhd.VhdFormat.ToString() }); vhd_type = $(if ($vhd) { $vhd.VhdType.ToString() });
    size = $(if ($vhd) { $vhd.Size }); parent = $(if ($vhd) { $vhd.ParentPath }) } })
$nics = @(Get-VMNetworkAdapter -VM $vm | ForEach-Object { [pscustomobject]@{ name = $_.Name; switch = $_.SwitchName;
  mac = $_.MacAddress; legacy = [bool]$_.IsLegacy } })
$boot = $null; $secure = $false
if ($vm.Generation -eq 2) {
  $fw = Get-VMFirmware -VM $vm
  $secure = ($fw.SecureBoot.ToString() -eq 'On')
  $b = $fw.BootOrder | Where-Object { $_.Device -is [Microsoft.HyperV.PowerShell.HardDiskDrive] } | Select-Object -First 1
  if ($b) { $boot = $b.Device.Id }
}
$cs = Get-WmiVm $vm.Id
$vssd = $cs.GetRelated('Msvm_VirtualSystemSettingData') | Where-Object { $_.VirtualSystemType -eq 'Microsoft:Hyper-V:System:Realized' }
$sasd = @($vssd.GetRelated('Msvm_StorageAllocationSettingData') | Where-Object { $_.ResourceType -eq 31 })
$os = $null
$kvp = $cs.GetRelated('Msvm_KvpExchangeComponent') | Select-Object -First 1
if ($kvp -and $kvp.GuestIntrinsicExchangeItems) {
  foreach ($x in $kvp.GuestIntrinsicExchangeItems) {
    $xml = [xml]$x
    $n = ($xml.INSTANCE.PROPERTY | Where-Object { $_.NAME -eq 'Name' }).VALUE
    if ($n -eq 'OSName') { $os = ($xml.INSTANCE.PROPERTY | Where-Object { $_.NAME -eq 'Data' }).VALUE }
  }
}
[pscustomobject]@{ id = $vm.Id.ToString(); name = $vm.Name; generation = $vm.Generation; cpu = $vm.ProcessorCount;
  memory_bytes = $vm.MemoryStartup; secure_boot = $secure; boot_disk_id = $boot; os_name = $os;
  version = $vm.Version; disks = $disks; nics = $nics; wmi_disk_count = $sasd.Count } | ConvertTo-Json -Compress -Depth 5
"""

# ConsistencyLevel: 1 = application-consistent (VSS in the guest), 2 = crash-consistent.
# SnapshotType 32768 = recovery snapshot: the backup-application snapshot type; hidden from the
# checkpoint tree UI and independent of the VM's own CheckpointType setting (nothing reconfigured).
CREATE_SNAPSHOT = r"""
$cs = Get-WmiVm $p.id
$svc = Get-WmiObject -Namespace $ns -Class Msvm_VirtualSystemSnapshotService
$s = ([wmiclass]"\\.\$($ns):Msvm_VirtualSystemSnapshotSettingData").CreateInstance()
$s.ConsistencyLevel = [uint16]$p.consistency
$s.IgnoreNonSnapshottableDisks = $false
$r = $svc.CreateSnapshot($cs, $s.GetText(1), [uint16]32768)
Wait-Job2 $r 'CreateSnapshot'
$snap = $null
if ($r.ResultingSnapshot) { $snap = [wmi]$r.ResultingSnapshot }
if (-not $snap) {
  $snap = $cs.GetRelated('Msvm_VirtualSystemSettingData') | Where-Object {
    $_.VirtualSystemType -eq 'Microsoft:Hyper-V:Snapshot:Recovery' } | Sort-Object CreationTime | Select-Object -Last 1
}
$disks = @($snap.GetRelated('Msvm_StorageAllocationSettingData') | Where-Object { $_.ResourceType -eq 31 } | ForEach-Object {
  $path = $_.HostResource[0]
  $vhd = Get-VHD -Path $path -ErrorAction SilentlyContinue
  [pscustomobject]@{ path = $path; vhd_type = $(if ($vhd) { $vhd.VhdType.ToString() }); parent = $(if ($vhd) { $vhd.ParentPath });
    format = $(if ($vhd) { $vhd.VhdFormat.ToString() }) } })
[pscustomobject]@{ id = $snap.InstanceID; type = $snap.VirtualSystemType; disks = $disks } | ConvertTo-Json -Compress -Depth 4
"""

DESTROY_SNAPSHOT = r"""
$snap = Get-WmiObject -Namespace $ns -Class Msvm_VirtualSystemSettingData -Filter "InstanceID='$($p.snapshot_id)'"
if (-not $snap) { '{"removed":false}'; return }
if ($snap.VirtualSystemType -ne 'Microsoft:Hyper-V:Snapshot:Recovery') { throw "refusing to delete $($p.snapshot_id): not a recovery snapshot" }
$svc = Get-WmiObject -Namespace $ns -Class Msvm_VirtualSystemSnapshotService
Wait-Job2 ($svc.DestroySnapshot($snap)) 'DestroySnapshot'
'{"removed":true}'
"""

# ---- RCT (experimental) -------------------------------------------------------------------
CONVERT_TO_REFERENCE_POINT = r"""
$snap = Get-WmiObject -Namespace $ns -Class Msvm_VirtualSystemSettingData -Filter "InstanceID='$($p.snapshot_id)'"
if ($snap.VirtualSystemType -ne 'Microsoft:Hyper-V:Snapshot:Recovery') { throw "not a recovery snapshot" }
$paths = @($snap.GetRelated('Msvm_StorageAllocationSettingData') | Where-Object { $_.ResourceType -eq 31 } | ForEach-Object { $_.HostResource[0] })
$svc = Get-WmiObject -Namespace $ns -Class Msvm_VirtualSystemReferencePointService
$r = $svc.ConvertToReferencePoint($snap, [uint16]1)
Wait-Job2 $r 'ConvertToReferencePoint'
$rp = [wmi]$r.ResultingReferencePoint
[pscustomobject]@{ id = $rp.InstanceID; rct_ids = @($rp.ResilientChangeTrackingIdentifiers); paths = $paths } | ConvertTo-Json -Compress -Depth 4
"""

DESTROY_REFERENCE_POINT = r"""
$rp = Get-WmiObject -Namespace $ns -Class Msvm_VirtualSystemReferencePoint -Filter "InstanceID='$($p.reference_point_id)'"
if (-not $rp) { '{"removed":false}'; return }
$svc = Get-WmiObject -Namespace $ns -Class Msvm_VirtualSystemReferencePointService
Wait-Job2 ($svc.DestroyReferencePoint($rp)) 'DestroyReferencePoint'
'{"removed":true}'
"""

CHANGED = r"""
$ims = Get-WmiObject -Namespace $ns -Class Msvm_ImageManagementService
$r = $ims.GetVirtualDiskChanges($p.path, $p.limit_id, [uint64]$p.offset, [uint64]$p.length)
if ($r.ReturnValue -ne 0) { throw "GetVirtualDiskChanges returned $($r.ReturnValue)" }
[pscustomobject]@{ offsets = @($r.ChangedByteOffsets); lengths = @($r.ChangedByteLengths); processed = $r.ProcessedByteLength } | ConvertTo-Json -Compress
"""


def render(script: str, params: dict[str, Any] | None = None) -> str:
    """Prelude + script with parameters embedded as base64 JSON (no injection possible)."""
    b64 = base64.b64encode(json.dumps(params or {}).encode()).decode()
    return PRELUDE.replace("__PARAMS__", b64) + script


class HypervClient:
    def __init__(self, host: str, user: str, password: str, *, transport: str = "ntlm", port: int = 5986,
                 use_ssl: bool = True, verify_tls: bool = True):
        import winrm  # provider-private dependency
        scheme = "https" if use_ssl else "http"
        self.session = winrm.Session(f"{scheme}://{host}:{port}/wsman", auth=(user, password), transport=transport,
                                     server_cert_validation="validate" if verify_tls else "ignore")
        self.host = host

    def ps(self, script: str, params: dict[str, Any] | None = None) -> Any:
        try:
            r = self.session.run_ps(render(script, params))
        except Exception as e:  # noqa: BLE001 - connection level
            raise TransientProviderError(f"WinRM to {self.host}: {e}") from e
        out = r.std_out.decode("utf-8", "replace").strip()
        if r.status_code != 0:
            err = r.std_err.decode("utf-8", "replace")
            raise ProviderError(f"PowerShell on {self.host} failed ({r.status_code}): {err[-2000:]}")
        try:
            return json.loads(out) if out else None
        except json.JSONDecodeError as e:
            raise ProviderError(f"unexpected PowerShell output from {self.host}: {out[:500]!r}") from e

    def close(self) -> None:
        pass

    # ------------------------------------------------------------------ operations
    def ping(self) -> dict[str, Any]:
        return self.ps(PING)

    def list_vms(self) -> list[dict[str, Any]]:
        from .mapping import as_list
        return as_list(self.ps(LIST_VMS))

    def vm_info(self, vm_id: str) -> dict[str, Any]:
        return self.ps(VM_INFO, {"id": vm_id})

    def create_snapshot(self, vm_id: str, consistency: int) -> dict[str, Any]:
        return self.ps(CREATE_SNAPSHOT, {"id": vm_id, "consistency": consistency})

    def destroy_snapshot(self, vm_id: str, snapshot_id: str) -> None:
        self.ps(DESTROY_SNAPSHOT, {"snapshot_id": snapshot_id})

    def convert_to_reference_point(self, snapshot_id: str) -> dict[str, Any]:
        return self.ps(CONVERT_TO_REFERENCE_POINT, {"snapshot_id": snapshot_id})

    def destroy_reference_point(self, rp_id: str) -> None:
        self.ps(DESTROY_REFERENCE_POINT, {"reference_point_id": rp_id})

    def changed(self, path: str, limit_id: str, offset: int, length: int) -> tuple[list[tuple[int, int]], int]:
        from ...core.errors import ChangeTrackingUnavailable
        from .mapping import as_list
        try:
            r = self.ps(CHANGED, {"path": path, "limit_id": limit_id, "offset": offset, "length": length})
        except ProviderError as e:
            raise ChangeTrackingUnavailable(str(e)) from e
        return list(zip((int(x) for x in as_list(r["offsets"])), (int(x) for x in as_list(r["lengths"])))), int(r["processed"])
