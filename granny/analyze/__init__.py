"""Cloud resource analysis and inventory (AWS, GCP, Azure)."""

from granny.analyze.lambdas import get_all_regions, list_lambdas
from granny.analyze.vpcs import list_vpcs

__all__ = [
    "get_all_regions",
    "list_lambdas",
    "list_vpcs",
]


def __getattr__(name: str):
    # Lazy re-exports so importing `granny.analyze` doesn't drag in boto3
    # for the new cross-cloud helpers, and so optional GCP/Azure SDKs aren't
    # required for AWS-only callers.
    if name in {
        "GpuInstance",
        "GpuReservation",
        "CapacityBlockOffering",
        "aws_profiles",
        "list_aws_gpu_instances",
        "list_aws_gpu_reservations",
        "list_aws_capacity_block_offerings",
        "gcp_projects",
        "list_gcp_gpu_instances",
        "list_gcp_gpu_reservations",
        "azure_subscriptions",
        "list_azure_gpu_instances",
        "list_azure_gpu_reservations",
    }:
        from granny.analyze import gpus

        return getattr(gpus, name)
    if name in {"CreditBalance", "aws_credits", "gcp_credits", "azure_credits"}:
        from granny.analyze import credits

        return getattr(credits, name)
    if name in {"CostSummary", "aws_costs", "gcp_costs", "azure_costs"}:
        from granny.analyze import costs

        return getattr(costs, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
