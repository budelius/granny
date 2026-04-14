"""List AWS VPCs."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import boto3

logger = logging.getLogger(__name__)


@dataclass
class VpcInfo:
    vpc_id: str
    cidr_block: str
    name: str
    is_default: bool
    region: str


def list_vpcs(regions: list[str] | None = None) -> list[VpcInfo]:
    """List all VPCs, optionally filtered by region.

    Args:
        regions: Specific regions to query. Defaults to the session's default region.

    Returns:
        List of VpcInfo objects.
    """
    if regions is None:
        regions = [boto3.session.Session().region_name or "us-east-1"]

    results: list[VpcInfo] = []
    for region in regions:
        try:
            ec2 = boto3.client("ec2", region_name=region)
            paginator = ec2.get_paginator("describe_vpcs")
            for page in paginator.paginate():
                for vpc in page["Vpcs"]:
                    name = "N/A"
                    for tag in vpc.get("Tags", []):
                        if tag["Key"] == "Name":
                            name = tag["Value"]
                            break
                    results.append(
                        VpcInfo(
                            vpc_id=vpc["VpcId"],
                            cidr_block=vpc["CidrBlock"],
                            name=name,
                            is_default=vpc.get("IsDefault", False),
                            region=region,
                        )
                    )
        except Exception as e:
            logger.warning("Error listing VPCs in %s: %s", region, e)
    return results
