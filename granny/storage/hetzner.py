"""Hetzner Object Storage (S3-compatible) client.

Lifted from ``tools/create/setup_hetzner_bunny.py:HetznerS3Client``.
Uses boto3 with S3v4 signatures against Hetzner's S3-compatible endpoint.
"""

import json
import logging
import time

import boto3
from botocore.config import Config

from granny.credentials.secrets import get_secret

logger = logging.getLogger(__name__)

HETZNER_S3_REGIONS = {
    "fsn1": "fsn1.your-objectstorage.com",
    "nbg1": "nbg1.your-objectstorage.com",
    "hel1": "hel1.your-objectstorage.com",
}


class HetznerS3Client:
    """Hetzner Object Storage bucket management (S3-compatible)."""

    def __init__(self, region: str = "fsn1") -> None:
        if region not in HETZNER_S3_REGIONS:
            raise ValueError(
                f"Unknown Hetzner region: {region}. "
                f"Choices: {', '.join(HETZNER_S3_REGIONS)}"
            )
        self.region = region
        self.endpoint = f"https://{HETZNER_S3_REGIONS[region]}"

        access_key = get_secret("HETZNER_S3_ACCESS_KEY")
        secret_key = get_secret("HETZNER_S3_SECRET_KEY")
        if not access_key or not secret_key:
            raise ValueError(
                "HETZNER_S3_ACCESS_KEY / HETZNER_S3_SECRET_KEY not set (env or vault)."
            )

        self.client = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(signature_version="s3v4"),
        )

    def bucket_exists(self, bucket_name: str) -> bool:
        try:
            self.client.head_bucket(Bucket=bucket_name)
            return True
        except Exception:
            return False

    def create_bucket(self, bucket_name: str) -> None:
        if self.bucket_exists(bucket_name):
            logger.info("Bucket %s already exists", bucket_name)
            return
        self.client.create_bucket(Bucket=bucket_name)
        logger.info("Created bucket %s in %s", bucket_name, self.region)
        time.sleep(3)  # Hetzner eventual consistency

    def set_bucket_policy_public(self, bucket_name: str) -> None:
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicReadGetObject",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": f"arn:aws:s3:::{bucket_name}/*",
                }
            ],
        }
        self.client.put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(policy))
        logger.info("Set public-read policy on %s", bucket_name)

    def set_cors_config(
        self, bucket_name: str, origins: list[str] | None = None
    ) -> None:
        cors = {
            "CORSRules": [
                {
                    "AllowedHeaders": ["*"],
                    "AllowedMethods": ["GET", "HEAD"],
                    "AllowedOrigins": origins or ["*"],
                    "MaxAgeSeconds": 3600,
                }
            ]
        }
        self.client.put_bucket_cors(Bucket=bucket_name, CORSConfiguration=cors)
        logger.info("CORS configured on %s", bucket_name)

    def upload_file(
        self,
        bucket_name: str,
        key: str,
        content: str | bytes,
        content_type: str = "text/html",
        cache_control: str = "max-age=300",
    ) -> None:
        body = content.encode("utf-8") if isinstance(content, str) else content
        self.client.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=body,
            ContentType=content_type,
            CacheControl=cache_control,
        )

    def get_bucket_url(self, bucket_name: str) -> str:
        return f"https://{bucket_name}.{HETZNER_S3_REGIONS[self.region]}"
