"""Purpose: the slice of EC2 the AWS source needs, behind one class; the ONLY module that imports
boto3 (the EBS direct-API reader in ebs.py receives the `ebs` client from here).

Usage:
    c = AwsClient(region="eu-west-1", access_key_id=..., secret_access_key=..., role_arn=None)
    c.instances(); c.instance("i-0abc"); c.volumes_for("i-0abc"); c.instance_type("m6i.large")
    snaps = c.create_snapshots("i-0abc", run_tag="vmigrate-123")   # {volume_id: snapshot_id}, completed
    c.delete_snapshot("snap-...")                                   # only snapshots tagged vmigrate:run
    c.ebs                                                           # boto3 "ebs" client

IAM needed: ec2:DescribeInstances/Volumes/InstanceTypes/Snapshots, ec2:CreateSnapshots,
ec2:CreateTags, ec2:DeleteSnapshot, ebs:ListSnapshotBlocks, ebs:GetSnapshotBlock,
ebs:ListChangedBlocks (+ kms:Decrypt on the volume keys for encrypted volumes).
"""

from __future__ import annotations

import time
from typing import Any

from ...core.errors import ProviderError, SafetyGateError, TransientProviderError

TAG = "vmigrate:run"


class AwsClient:
    def __init__(self, *, region: str, access_key_id: str | None = None, secret_access_key: str | None = None,
                 session_token: str | None = None, role_arn: str | None = None, external_id: str | None = None,
                 endpoint_url: str | None = None, poll: float = 10, timeout: float = 6 * 3600):
        import boto3  # provider-private dependency
        s = boto3.Session(aws_access_key_id=access_key_id or None, aws_secret_access_key=secret_access_key or None,
                          aws_session_token=session_token or None, region_name=region)
        if role_arn:
            kw = {"RoleArn": role_arn, "RoleSessionName": "vmigrate"}
            if external_id:
                kw["ExternalId"] = external_id
            cr = s.client("sts").assume_role(**kw)["Credentials"]
            s = boto3.Session(aws_access_key_id=cr["AccessKeyId"], aws_secret_access_key=cr["SecretAccessKey"],
                              aws_session_token=cr["SessionToken"], region_name=region)
        self.ec2 = s.client("ec2", endpoint_url=endpoint_url)
        self.ebs = s.client("ebs")
        self.sts = s.client("sts")
        self.poll, self.timeout = poll, timeout

    def identity(self) -> dict[str, Any]:
        return self.sts.get_caller_identity()

    def instances(self) -> list[dict[str, Any]]:
        out = []
        for page in self.ec2.get_paginator("describe_instances").paginate(
                Filters=[{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]):
            for r in page["Reservations"]:
                out += r["Instances"]
        return out

    def instance(self, iid: str) -> dict[str, Any]:
        r = self.ec2.describe_instances(InstanceIds=[iid])["Reservations"]
        if not r:
            raise ProviderError(f"aws: instance {iid} not found")
        return r[0]["Instances"][0]

    def volumes_for(self, iid: str) -> list[dict[str, Any]]:
        out = []
        for page in self.ec2.get_paginator("describe_volumes").paginate(
                Filters=[{"Name": "attachment.instance-id", "Values": [iid]}]):
            out += page["Volumes"]
        return out

    def instance_type(self, t: str) -> dict[str, Any]:
        return self.ec2.describe_instance_types(InstanceTypes=[t])["InstanceTypes"][0]

    def create_snapshots(self, iid: str, run_tag: str) -> dict[str, str]:
        """Multi-volume, crash-consistent snapshot set of every EBS volume of the instance; waits
        until all are `completed` (required by the EBS direct APIs)."""
        if not run_tag.startswith("vmigrate-"):
            raise SafetyGateError("run tags must start with vmigrate-")
        r = self.ec2.create_snapshots(
            InstanceSpecification={"InstanceId": iid, "ExcludeBootVolume": False},
            Description=f"vmigrate read point {run_tag} (safe to delete after the migration/sync)",
            TagSpecifications=[{"ResourceType": "snapshot", "Tags": [{"Key": TAG, "Value": run_tag}]}],
            CopyTagsFromSource="volume")
        snaps = {s["VolumeId"]: s["SnapshotId"] for s in r["Snapshots"]}
        deadline = time.monotonic() + self.timeout
        while True:
            st = self.ec2.describe_snapshots(SnapshotIds=list(snaps.values()))["Snapshots"]
            if any(s["State"] == "error" for s in st):
                raise ProviderError(f"EBS snapshot failed: {[s['SnapshotId'] for s in st if s['State'] == 'error']}")
            if all(s["State"] == "completed" for s in st):
                return snaps
            if time.monotonic() > deadline:
                raise TransientProviderError(f"EBS snapshots still pending after {self.timeout}s")
            time.sleep(self.poll)

    def delete_snapshot(self, sid: str) -> None:
        st = self.ec2.describe_snapshots(Filters=[{"Name": "snapshot-id", "Values": [sid]}])["Snapshots"]
        if not st:
            return
        if not any(t["Key"] == TAG for t in st[0].get("Tags") or []):
            raise SafetyGateError(f"refusing to delete {sid}: not tagged {TAG}")
        self.ec2.delete_snapshot(SnapshotId=sid)
