"""Scaleway account-level clients that are not tied to one product.

``granny.serverless.scaleway`` covers Functions and Containers, which are
regional and project-scoped. Billing is neither: it is organization-scoped and
spans every product, so it lives here rather than under one product's module.
"""
