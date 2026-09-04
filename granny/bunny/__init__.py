"""Bunny account-level clients that are not tied to one product.

Bunny's product clients already live under the layer they belong to:
``granny.cdn.bunny`` for pull zones, ``granny.storage.bunny`` for storage
zones, ``granny.dns.bunny`` for DNS. Billing spans all three, so it lives
here rather than picking one of them arbitrarily.
"""
