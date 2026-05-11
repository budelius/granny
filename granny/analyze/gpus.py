"""List GPU compute resources and reservations across cloud providers.

Covers AWS (all profiles, all regions), GCP (all reachable projects), and
Azure (all visible subscriptions). For each provider we surface two things:

  * Active GPU instances -- VMs with attached H100 / H200 / A100 / L4 / ...
  * Reserved GPU capacity -- capacity reservations, RIs, capacity blocks,
    committed-use discounts; whatever each provider exposes natively.

The provider SDKs for GCP and Azure are optional extras; their helpers raise
``RuntimeError`` with an install hint if the SDK isn't importable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GPU instance type catalog
# ---------------------------------------------------------------------------

# AWS: pattern -> (model, count, mem_gb)
_AWS_GPU_PATTERNS: list[tuple[re.Pattern[str], str, int, int]] = [
    (re.compile(r"^p5\.48xlarge$"), "H100", 8, 80),
    (re.compile(r"^p5\.4xlarge$"), "H100", 1, 80),
    (re.compile(r"^p5e\.48xlarge$"), "H200", 8, 141),
    (re.compile(r"^p5en\.48xlarge$"), "H200", 8, 141),
    (re.compile(r"^p4d\.24xlarge$"), "A100", 8, 40),
    (re.compile(r"^p4de\.24xlarge$"), "A100", 8, 80),
    (re.compile(r"^p3\.2xlarge$"), "V100", 1, 16),
    (re.compile(r"^p3\.8xlarge$"), "V100", 4, 16),
    (re.compile(r"^p3\.16xlarge$"), "V100", 8, 16),
    (re.compile(r"^p3dn\.24xlarge$"), "V100", 8, 32),
    (re.compile(r"^g6e\.(xlarge|2xlarge|4xlarge|8xlarge|16xlarge)$"), "L40S", 1, 48),
    (re.compile(r"^g6e\.12xlarge$"), "L40S", 4, 48),
    (re.compile(r"^g6e\.24xlarge$"), "L40S", 4, 48),
    (re.compile(r"^g6e\.48xlarge$"), "L40S", 8, 48),
    (re.compile(r"^g6\.(xlarge|2xlarge|4xlarge|8xlarge|16xlarge)$"), "L4", 1, 24),
    (re.compile(r"^g6\.12xlarge$"), "L4", 4, 24),
    (re.compile(r"^g6\.24xlarge$"), "L4", 4, 24),
    (re.compile(r"^g6\.48xlarge$"), "L4", 8, 24),
    (re.compile(r"^g5\.(xlarge|2xlarge|4xlarge|8xlarge|16xlarge)$"), "A10G", 1, 24),
    (re.compile(r"^g5\.12xlarge$"), "A10G", 4, 24),
    (re.compile(r"^g5\.24xlarge$"), "A10G", 4, 24),
    (re.compile(r"^g5\.48xlarge$"), "A10G", 8, 24),
    (re.compile(r"^g4dn\.(xlarge|2xlarge|4xlarge|8xlarge|16xlarge)$"), "T4", 1, 16),
    (re.compile(r"^g4dn\.12xlarge$"), "T4", 4, 16),
    (re.compile(r"^g4dn\.metal$"), "T4", 8, 16),
]

# GCP: pattern -> (model, count, mem_gb). count==0 means "use regex group 1".
_GCP_GPU_PATTERNS: list[tuple[re.Pattern[str], str, int, int]] = [
    (re.compile(r"^a3-ultragpu-8g$"), "H200", 8, 141),
    (re.compile(r"^a3-megagpu-8g$"), "H100", 8, 80),
    (re.compile(r"^a3-edgegpu-8g$"), "H100", 8, 80),
    (re.compile(r"^a3-highgpu-(\d+)g$"), "H100", 0, 80),
    (re.compile(r"^a2-ultragpu-(\d+)g$"), "A100", 0, 80),
    (re.compile(r"^a2-megagpu-(\d+)g$"), "A100", 0, 40),
    (re.compile(r"^a2-highgpu-(\d+)g$"), "A100", 0, 40),
    (re.compile(r"^g2-standard-\d+$"), "L4", 1, 24),
]

# Azure: pattern -> (model, count, mem_gb)
_AZURE_GPU_PATTERNS: list[tuple[re.Pattern[str], str, int, int]] = [
    (re.compile(r"^Standard_ND96isr_H200_v5$", re.IGNORECASE), "H200", 8, 141),
    (re.compile(r"^Standard_ND96isr_H100_v5$", re.IGNORECASE), "H100", 8, 80),
    (re.compile(r"^Standard_ND96amsr_H100_v5$", re.IGNORECASE), "H100", 8, 80),
    (re.compile(r"^Standard_ND96asr_A100_v4$", re.IGNORECASE), "A100", 8, 80),
    (re.compile(r"^Standard_ND96amsr_A100_v4$", re.IGNORECASE), "A100", 8, 80),
    (re.compile(r"^Standard_NC24ads_A100_v4$", re.IGNORECASE), "A100", 1, 80),
    (re.compile(r"^Standard_NC48ads_A100_v4$", re.IGNORECASE), "A100", 2, 80),
    (re.compile(r"^Standard_NC96ads_A100_v4$", re.IGNORECASE), "A100", 4, 80),
    (re.compile(r"^Standard_NV(6|12|18|36|72)ads_A10_v5$", re.IGNORECASE), "A10", 0, 24),
    (re.compile(r"^Standard_ND96isr_MI300X_v5$", re.IGNORECASE), "MI300X", 8, 192),
]


def _classify_aws(instance_type: str) -> tuple[str, int, int] | None:
    for pat, model, count, mem in _AWS_GPU_PATTERNS:
        if pat.match(instance_type):
            return model, count, mem
    return None


def _classify_gcp(machine_type: str) -> tuple[str, int, int] | None:
    for pat, model, count, mem in _GCP_GPU_PATTERNS:
        m = pat.match(machine_type)
        if not m:
            continue
        if count == 0 and m.groups():
            try:
                count = int(m.group(1))
            except (IndexError, ValueError):
                count = 1
        return model, count, mem
    return None


def _classify_azure(vm_size: str) -> tuple[str, int, int] | None:
    for pat, model, count, mem in _AZURE_GPU_PATTERNS:
        if pat.match(vm_size):
            return model, count, mem
    return None


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class GpuInstance:
    provider: str
    account: str
    region: str
    zone: str
    instance_id: str
    instance_name: str
    instance_type: str
    gpu_model: str
    gpu_count: int
    gpu_memory_gb: int
    state: str
    launch_time: str | None = None
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class GpuReservation:
    provider: str
    account: str
    region: str
    zone: str | None
    reservation_id: str
    instance_type: str
    gpu_model: str
    gpu_count_per_instance: int
    instance_count: int
    state: str
    start_time: str | None
    end_time: str | None
    commitment_term: str | None


@dataclass
class CapacityBlockOffering:
    """An available EC2 Capacity Block for ML offering (not yet purchased)."""

    account: str
    region: str
    zone: str
    offering_id: str
    instance_type: str
    instance_count: int
    duration_hours: int
    start_time: str
    end_time: str
    upfront_fee: float
    currency: str
    tenancy: str


# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------


def aws_profiles() -> list[str]:
    import boto3

    profiles = boto3.session.Session().available_profiles or []
    return list(profiles) if profiles else ["default"]


def _aws_regions(profile: str) -> list[str]:
    import boto3

    session = boto3.Session(profile_name=profile if profile != "default" else None)
    ec2 = session.client("ec2", region_name="us-east-1")
    resp = ec2.describe_regions(AllRegions=False)
    return [r["RegionName"] for r in resp["Regions"]]


def list_aws_gpu_instances(
    profiles: list[str] | None = None,
    regions: list[str] | None = None,
    include_stopped: bool = False,
) -> list[GpuInstance]:
    """Enumerate GPU EC2 instances across one or more AWS profiles."""
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    profiles = profiles or aws_profiles()
    results: list[GpuInstance] = []

    for profile in profiles:
        try:
            regs = regions or _aws_regions(profile)
        except (BotoCoreError, ClientError) as e:
            logger.warning("AWS profile %s: cannot enumerate regions: %s", profile, e)
            continue

        for region in regs:
            try:
                session = boto3.Session(profile_name=profile if profile != "default" else None)
                ec2 = session.client("ec2", region_name=region)
                paginator = ec2.get_paginator("describe_instances")
                for page in paginator.paginate():
                    for reservation in page.get("Reservations", []):
                        for inst in reservation.get("Instances", []):
                            itype = inst.get("InstanceType", "")
                            gpu = _classify_aws(itype)
                            if not gpu:
                                continue
                            state = inst.get("State", {}).get("Name", "unknown")
                            if not include_stopped and state != "running":
                                continue
                            model, count, mem = gpu
                            tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                            results.append(
                                GpuInstance(
                                    provider="aws",
                                    account=profile,
                                    region=region,
                                    zone=inst.get("Placement", {}).get("AvailabilityZone", ""),
                                    instance_id=inst["InstanceId"],
                                    instance_name=tags.get("Name", ""),
                                    instance_type=itype,
                                    gpu_model=model,
                                    gpu_count=count,
                                    gpu_memory_gb=mem,
                                    state=state,
                                    launch_time=_iso(inst.get("LaunchTime")),
                                    tags=tags,
                                )
                            )
            except (BotoCoreError, ClientError) as e:
                logger.warning("AWS %s/%s instances: %s", profile, region, e)
    return results


def list_aws_gpu_reservations(
    profiles: list[str] | None = None,
    regions: list[str] | None = None,
) -> list[GpuReservation]:
    """Enumerate active EC2 capacity reservations and RIs for GPU types."""
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    profiles = profiles or aws_profiles()
    results: list[GpuReservation] = []

    for profile in profiles:
        try:
            regs = regions or _aws_regions(profile)
        except (BotoCoreError, ClientError) as e:
            logger.warning("AWS profile %s: cannot enumerate regions: %s", profile, e)
            continue

        for region in regs:
            session = boto3.Session(profile_name=profile if profile != "default" else None)
            ec2 = session.client("ec2", region_name=region)

            try:
                paginator = ec2.get_paginator("describe_capacity_reservations")
                for page in paginator.paginate():
                    for cr in page.get("CapacityReservations", []):
                        itype = cr.get("InstanceType", "")
                        gpu = _classify_aws(itype)
                        if not gpu:
                            continue
                        model, count, _ = gpu
                        results.append(
                            GpuReservation(
                                provider="aws",
                                account=profile,
                                region=region,
                                zone=cr.get("AvailabilityZone"),
                                reservation_id=cr["CapacityReservationId"],
                                instance_type=itype,
                                gpu_model=model,
                                gpu_count_per_instance=count,
                                instance_count=cr.get("TotalInstanceCount", 0),
                                state=cr.get("State", "unknown"),
                                start_time=_iso(cr.get("StartDate")),
                                end_time=_iso(cr.get("EndDate")),
                                commitment_term=(
                                    "capacity-block"
                                    if cr.get("ReservationType") == "capacity-block"
                                    else cr.get("CommitmentInfo", {}).get("CommitmentDuration")
                                ),
                            )
                        )
            except (BotoCoreError, ClientError) as e:
                logger.warning("AWS %s/%s capacity reservations: %s", profile, region, e)

            try:
                ris = ec2.describe_reserved_instances().get("ReservedInstances", [])
                for ri in ris:
                    itype = ri.get("InstanceType", "")
                    gpu = _classify_aws(itype)
                    if not gpu:
                        continue
                    model, count, _ = gpu
                    seconds = int(ri.get("Duration", 0))
                    months = seconds // (30 * 86400)
                    term = f"{months // 12}y" if months >= 12 else f"{months}m"
                    results.append(
                        GpuReservation(
                            provider="aws",
                            account=profile,
                            region=region,
                            zone=ri.get("AvailabilityZone"),
                            reservation_id=ri["ReservedInstancesId"],
                            instance_type=itype,
                            gpu_model=model,
                            gpu_count_per_instance=count,
                            instance_count=ri.get("InstanceCount", 0),
                            state=ri.get("State", "unknown"),
                            start_time=_iso(ri.get("Start")),
                            end_time=_iso(ri.get("End")),
                            commitment_term=term,
                        )
                    )
            except (BotoCoreError, ClientError) as e:
                logger.warning("AWS %s/%s reserved instances: %s", profile, region, e)
    return results


# Regions where Capacity Blocks for ML are generally available.
# describe-capacity-block-offerings only works here; other regions return
# UnsupportedOperation. Update as AWS rolls out to new regions.
_AWS_CAPACITY_BLOCK_REGIONS: tuple[str, ...] = (
    "us-east-1",
    "us-east-2",
    "us-west-2",
    "eu-west-1",
    "eu-west-2",
    "eu-north-1",
    "eu-central-1",
    "ap-northeast-1",
    "ap-southeast-2",
)


def list_aws_capacity_block_offerings(
    instance_type: str,
    instance_count: int = 1,
    duration_hours: int = 24,
    start_window_days: int = 30,
    profiles: list[str] | None = None,
    regions: list[str] | None = None,
) -> list[CapacityBlockOffering]:
    """Find available EC2 Capacity Block offerings for ML GPU instances.

    Args:
        instance_type: GPU instance type, e.g. ``p5.48xlarge`` or ``p5e.48xlarge``.
        instance_count: How many instances the block must hold.
        duration_hours: Block length in hours (AWS supports 1, 6, 12, 24, 48,
            72, 168, 336, 504, 672 ... — pass what you need).
        start_window_days: Search for blocks starting between now and this many
            days out.
        profiles: AWS profiles to query (default: all configured profiles).
        regions: Regions to query (default: the known Capacity-Block-enabled
            regions; pass an explicit list to override).

    Returns:
        Offerings sorted cheapest-first.
    """
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    profiles = profiles or aws_profiles()
    regions = regions or list(_AWS_CAPACITY_BLOCK_REGIONS)
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=start_window_days)

    out: list[CapacityBlockOffering] = []
    for profile in profiles:
        for region in regions:
            try:
                session = boto3.Session(profile_name=profile if profile != "default" else None)
                ec2 = session.client("ec2", region_name=region)
                paginator = ec2.get_paginator("describe_capacity_block_offerings")
                for page in paginator.paginate(
                    InstanceType=instance_type,
                    InstanceCount=instance_count,
                    CapacityDurationHours=duration_hours,
                    StartDateRange=now,
                    EndDateRange=end,
                ):
                    for o in page.get("CapacityBlockOfferings", []):
                        try:
                            fee = float(o.get("UpfrontFee", 0) or 0)
                        except (TypeError, ValueError):
                            fee = 0.0
                        out.append(
                            CapacityBlockOffering(
                                account=profile,
                                region=region,
                                zone=o.get("AvailabilityZone", ""),
                                offering_id=o.get("CapacityBlockOfferingId", ""),
                                instance_type=o.get("InstanceType", instance_type),
                                instance_count=o.get("InstanceCount", instance_count),
                                duration_hours=o.get(
                                    "CapacityBlockDurationHours", duration_hours
                                ),
                                start_time=_iso(o.get("StartDate")) or "",
                                end_time=_iso(o.get("EndDate")) or "",
                                upfront_fee=fee,
                                currency=o.get("CurrencyCode", "USD"),
                                tenancy=o.get("Tenancy", "default"),
                            )
                        )
            except (BotoCoreError, ClientError) as e:
                logger.debug("AWS %s/%s capacity-block offerings: %s", profile, region, e)
    out.sort(key=lambda o: (o.upfront_fee, o.start_time))
    return out


# ---------------------------------------------------------------------------
# GCP
# ---------------------------------------------------------------------------


def _require_gcp() -> None:
    try:
        from google.cloud import compute_v1  # noqa: F401
        from google.cloud import resourcemanager_v3  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "GCP support requires the [gcp] extra: pip install 'granny-devops[gcp]'"
        ) from e


def gcp_projects() -> list[str]:
    _require_gcp()
    from google.cloud import resourcemanager_v3

    client = resourcemanager_v3.ProjectsClient()
    return [p.project_id for p in client.search_projects(query="state:ACTIVE")]


def list_gcp_gpu_instances(
    projects: list[str] | None = None,
    include_stopped: bool = False,
) -> list[GpuInstance]:
    """Enumerate GPU VMs across one or more GCP projects (all zones)."""
    _require_gcp()
    from google.api_core.exceptions import GoogleAPIError
    from google.cloud import compute_v1

    projects = projects or gcp_projects()
    results: list[GpuInstance] = []
    instances = compute_v1.InstancesClient()

    for project in projects:
        try:
            request = compute_v1.AggregatedListInstancesRequest(project=project)
            for zone_url, scoped in instances.aggregated_list(request=request):
                zone = zone_url.split("/")[-1] if zone_url else ""
                region = zone.rsplit("-", 1)[0] if zone else ""
                for vm in scoped.instances or []:
                    machine_type = (vm.machine_type or "").split("/")[-1]
                    gpu = _classify_gcp(machine_type)
                    if not gpu and vm.guest_accelerators:
                        accel = vm.guest_accelerators[0]
                        model = _gcp_accel_model(accel.accelerator_type)
                        if model:
                            gpu = (model, accel.accelerator_count or 1, _gpu_mem_for_model(model))
                    if not gpu:
                        continue
                    state = (vm.status or "").lower()
                    if not include_stopped and state != "running":
                        continue
                    model, count, mem = gpu
                    results.append(
                        GpuInstance(
                            provider="gcp",
                            account=project,
                            region=region,
                            zone=zone,
                            instance_id=str(vm.id),
                            instance_name=vm.name or "",
                            instance_type=machine_type,
                            gpu_model=model,
                            gpu_count=count,
                            gpu_memory_gb=mem,
                            state=state,
                            launch_time=vm.creation_timestamp or None,
                            tags=dict(vm.labels or {}),
                        )
                    )
        except GoogleAPIError as e:
            logger.warning("GCP project %s instances: %s", project, e)
    return results


def list_gcp_gpu_reservations(
    projects: list[str] | None = None,
) -> list[GpuReservation]:
    """Enumerate GCP zonal GPU reservations and regional GPU commitments."""
    _require_gcp()
    from google.api_core.exceptions import GoogleAPIError
    from google.cloud import compute_v1

    projects = projects or gcp_projects()
    results: list[GpuReservation] = []
    reservations = compute_v1.ReservationsClient()
    commitments = compute_v1.RegionCommitmentsClient()
    regions_client = compute_v1.RegionsClient()

    for project in projects:
        try:
            req = compute_v1.AggregatedListReservationsRequest(project=project)
            for zone_url, scoped in reservations.aggregated_list(request=req):
                zone = zone_url.split("/")[-1] if zone_url else ""
                region = zone.rsplit("-", 1)[0] if zone else ""
                for r in scoped.reservations or []:
                    spec = r.specific_reservation
                    itype = spec.instance_properties.machine_type if spec else ""
                    gpu = _classify_gcp(itype) if itype else None
                    if not gpu and spec and spec.instance_properties.guest_accelerators:
                        accel = spec.instance_properties.guest_accelerators[0]
                        model = _gcp_accel_model(accel.accelerator_type)
                        if model:
                            gpu = (model, accel.accelerator_count or 1, _gpu_mem_for_model(model))
                    if not gpu:
                        continue
                    model, count, _ = gpu
                    results.append(
                        GpuReservation(
                            provider="gcp",
                            account=project,
                            region=region,
                            zone=zone,
                            reservation_id=r.name or "",
                            instance_type=itype,
                            gpu_model=model,
                            gpu_count_per_instance=count,
                            instance_count=spec.count if spec else 0,
                            state=(r.status or "").lower(),
                            start_time=r.creation_timestamp or None,
                            end_time=None,
                            commitment_term=None,
                        )
                    )
        except GoogleAPIError as e:
            logger.warning("GCP project %s reservations: %s", project, e)

        try:
            for region_obj in regions_client.list(project=project):
                region = region_obj.name
                try:
                    for c in commitments.list(project=project, region=region):
                        gpu_resources = [
                            r for r in (c.resources or []) if r.type_ == "ACCELERATOR"
                        ]
                        if not gpu_resources:
                            continue
                        for gr in gpu_resources:
                            model = _gcp_accel_model(gr.accelerator_type)
                            if not model:
                                continue
                            results.append(
                                GpuReservation(
                                    provider="gcp",
                                    account=project,
                                    region=region,
                                    zone=None,
                                    reservation_id=c.name or "",
                                    instance_type="",
                                    gpu_model=model,
                                    gpu_count_per_instance=1,
                                    instance_count=gr.amount or 0,
                                    state=(c.status or "").lower(),
                                    start_time=c.start_timestamp or None,
                                    end_time=c.end_timestamp or None,
                                    commitment_term=(c.plan or "").lower() or None,
                                )
                            )
                except GoogleAPIError as e:
                    logger.debug("GCP commitments %s/%s: %s", project, region, e)
        except GoogleAPIError as e:
            logger.warning("GCP project %s region list: %s", project, e)
    return results


def _gcp_accel_model(accelerator_type: str) -> str | None:
    if not accelerator_type:
        return None
    name = accelerator_type.rsplit("/", 1)[-1].lower()
    if "h200" in name:
        return "H200"
    if "h100" in name:
        return "H100"
    if "a100" in name:
        return "A100"
    if "l4" in name:
        return "L4"
    if "t4" in name:
        return "T4"
    if "v100" in name:
        return "V100"
    if "p100" in name:
        return "P100"
    if "p4" in name:
        return "P4"
    return None


def _gpu_mem_for_model(model: str) -> int:
    return {
        "H200": 141, "H100": 80, "A100": 80, "L40S": 48, "L4": 24,
        "A10": 24, "A10G": 24, "T4": 16, "V100": 16, "P100": 16, "P4": 8,
        "MI300X": 192,
    }.get(model, 0)


# ---------------------------------------------------------------------------
# Azure
# ---------------------------------------------------------------------------


def _require_azure() -> None:
    try:
        from azure.identity import DefaultAzureCredential  # noqa: F401
        from azure.mgmt.compute import ComputeManagementClient  # noqa: F401
        from azure.mgmt.subscription import SubscriptionClient  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "Azure support requires the [azure] extra: pip install 'granny-devops[azure]'"
        ) from e


def azure_subscriptions() -> list[tuple[str, str]]:
    """Return list of (subscription_id, display_name) visible to the caller."""
    _require_azure()
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.subscription import SubscriptionClient

    credential = DefaultAzureCredential()
    sub_client = SubscriptionClient(credential)
    return [(s.subscription_id, s.display_name or "") for s in sub_client.subscriptions.list()]


def list_azure_gpu_instances(
    subscriptions: list[str] | None = None,
    include_stopped: bool = False,
) -> list[GpuInstance]:
    """Enumerate GPU VMs across one or more Azure subscriptions."""
    _require_azure()
    from azure.core.exceptions import HttpResponseError
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.compute import ComputeManagementClient

    credential = DefaultAzureCredential()
    subs = subscriptions or [sid for sid, _ in azure_subscriptions()]
    results: list[GpuInstance] = []

    for sub in subs:
        try:
            compute = ComputeManagementClient(credential, sub)
            for vm in compute.virtual_machines.list_all():
                size = (vm.hardware_profile.vm_size or "") if vm.hardware_profile else ""
                gpu = _classify_azure(size)
                if not gpu:
                    continue
                model, count, mem = gpu
                state = "unknown"
                try:
                    rg = _azure_rg_from_id(vm.id)
                    iv = compute.virtual_machines.instance_view(rg, vm.name)
                    for s in iv.statuses or []:
                        if s.code and s.code.startswith("PowerState/"):
                            state = s.code.split("/", 1)[1]
                except HttpResponseError as e:
                    logger.debug("Azure power state %s: %s", vm.id, e)
                if not include_stopped and state != "running":
                    continue
                results.append(
                    GpuInstance(
                        provider="azure",
                        account=sub,
                        region=vm.location or "",
                        zone=",".join(vm.zones or []),
                        instance_id=vm.id or "",
                        instance_name=vm.name or "",
                        instance_type=size,
                        gpu_model=model,
                        gpu_count=count,
                        gpu_memory_gb=mem,
                        state=state,
                        launch_time=None,
                        tags=dict(vm.tags or {}),
                    )
                )
        except HttpResponseError as e:
            logger.warning("Azure sub %s instances: %s", sub, e)
    return results


def list_azure_gpu_reservations(
    subscriptions: list[str] | None = None,
) -> list[GpuReservation]:
    """Enumerate Azure capacity reservations and reserved-instance orders."""
    _require_azure()
    from azure.core.exceptions import HttpResponseError
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.compute import ComputeManagementClient

    credential = DefaultAzureCredential()
    subs = subscriptions or [sid for sid, _ in azure_subscriptions()]
    results: list[GpuReservation] = []

    for sub in subs:
        try:
            compute = ComputeManagementClient(credential, sub)
            for group in compute.capacity_reservation_groups.list_by_subscription():
                rg = _azure_rg_from_id(group.id)
                for r in compute.capacity_reservations.list_by_capacity_reservation_group(
                    rg, group.name
                ):
                    size = (r.sku.name or "") if r.sku else ""
                    gpu = _classify_azure(size)
                    if not gpu:
                        continue
                    model, count, _ = gpu
                    results.append(
                        GpuReservation(
                            provider="azure",
                            account=sub,
                            region=r.location or "",
                            zone=",".join(r.zones or []) if r.zones else None,
                            reservation_id=r.id or "",
                            instance_type=size,
                            gpu_model=model,
                            gpu_count_per_instance=count,
                            instance_count=(r.sku.capacity or 0) if r.sku else 0,
                            state=r.provisioning_state or "unknown",
                            start_time=None,
                            end_time=None,
                            commitment_term=None,
                        )
                    )
        except HttpResponseError as e:
            logger.warning("Azure sub %s capacity reservations: %s", sub, e)

    try:
        from azure.mgmt.reservations import AzureReservationAPI

        reservations = AzureReservationAPI(credential)
        for order in reservations.reservation_order.list():
            for r in reservations.reservation.list(order.name):
                sku = (r.sku.name or "") if r.sku else ""
                gpu = _classify_azure(sku)
                if not gpu:
                    continue
                model, count, _ = gpu
                props = r.properties
                results.append(
                    GpuReservation(
                        provider="azure",
                        account=order.name or "",
                        region=(props.applied_scopes[0] if props and props.applied_scopes else ""),
                        zone=None,
                        reservation_id=r.id or "",
                        instance_type=sku,
                        gpu_model=model,
                        gpu_count_per_instance=count,
                        instance_count=(props.quantity or 0) if props else 0,
                        state=(props.provisioning_state if props else "unknown"),
                        start_time=(
                            str(props.effective_date_time)
                            if props and props.effective_date_time else None
                        ),
                        end_time=(
                            str(props.expiry_date) if props and props.expiry_date else None
                        ),
                        commitment_term=(props.term if props else None),
                    )
                )
    except ImportError:
        logger.debug("azure-mgmt-reservations not installed -- skipping RI orders")
    except Exception as e:  # noqa: BLE001 -- reservations API often 403s without RBAC
        logger.warning("Azure reservation orders: %s", e)

    return results


def _azure_rg_from_id(resource_id: str) -> str:
    parts = (resource_id or "").split("/")
    for i, p in enumerate(parts):
        if p.lower() == "resourcegroups" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(dt)
