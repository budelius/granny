"""AWS resource analysis and inventory."""

from granny.analyze.lambdas import get_all_regions, list_lambdas
from granny.analyze.vpcs import list_vpcs

__all__ = ["get_all_regions", "list_lambdas", "list_vpcs"]
