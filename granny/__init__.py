"""Granny -- Cloud tools collection for AWS infrastructure and DevOps automation."""

__version__ = "0.18.1"
__all__ = [
    "get_secret",
    "load_secrets_into_env",
]


def __getattr__(name: str):
    if name == "get_secret":
        from granny.credentials.secrets import get_secret

        return get_secret
    if name == "load_secrets_into_env":
        from granny.credentials.secrets import load_secrets_into_env

        return load_secrets_into_env
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
