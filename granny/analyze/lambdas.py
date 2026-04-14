"""List AWS Lambda functions across regions."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import boto3

logger = logging.getLogger(__name__)


@dataclass
class LambdaFunction:
    name: str
    runtime: str
    version: str
    region: str


def get_all_regions() -> list[str]:
    """Return all available AWS region names."""
    ec2 = boto3.client("ec2")
    response = ec2.describe_regions()
    return [r["RegionName"] for r in response["Regions"]]


def list_lambdas(
    regions: list[str] | None = None,
) -> list[LambdaFunction]:
    """List Lambda functions, optionally filtered by region.

    Args:
        regions: Specific regions to query. Defaults to all regions.

    Returns:
        List of LambdaFunction objects.
    """
    if regions is None:
        regions = get_all_regions()

    results: list[LambdaFunction] = []
    for region in regions:
        try:
            client = boto3.client("lambda", region_name=region)
            paginator = client.get_paginator("list_functions")
            for page in paginator.paginate():
                for fn in page.get("Functions", []):
                    results.append(
                        LambdaFunction(
                            name=fn["FunctionName"],
                            runtime=fn.get("Runtime", "N/A"),
                            version=fn.get("Version", "N/A"),
                            region=region,
                        )
                    )
        except Exception as e:
            logger.warning("Error listing lambdas in %s: %s", region, e)
    return results
