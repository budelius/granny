"""AWS S3 client for static website hosting.

Extracted from ``tools/create/setup_s3_website.py`` and
``tools/create/setup_aws_cloudfront.py``.
"""

import json
import logging
import time

import boto3

logger = logging.getLogger(__name__)


def _website_endpoint(bucket: str, region: str) -> str:
    if region == "us-east-1":
        return f"{bucket}.s3-website-us-east-1.amazonaws.com"
    return f"{bucket}.s3-website.{region}.amazonaws.com"


class AWSS3Client:
    """AWS S3 bucket management for static website hosting."""

    def __init__(
        self, *, aws_profile: str | None = None, region: str | None = None
    ) -> None:
        session_kwargs: dict = {}
        if aws_profile:
            session_kwargs["profile_name"] = aws_profile
        if region:
            session_kwargs["region_name"] = region
        session = boto3.Session(**session_kwargs)
        self.region = session.region_name or "us-east-1"
        self.client = session.client("s3")

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
        kwargs: dict = {"Bucket": bucket_name}
        if self.region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {
                "LocationConstraint": self.region
            }
        self.client.create_bucket(**kwargs)
        logger.info("Created bucket %s in %s", bucket_name, self.region)

    def enable_website_hosting(
        self, bucket_name: str, index: str = "index.html", error: str = "error.html"
    ) -> None:
        """Enable static website hosting with public-read policy."""
        # Disable block-public-access
        self.client.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": False,
                "IgnorePublicAcls": False,
                "BlockPublicPolicy": False,
                "RestrictPublicBuckets": False,
            },
        )
        time.sleep(5)

        self.client.put_bucket_website(
            Bucket=bucket_name,
            WebsiteConfiguration={
                "IndexDocument": {"Suffix": index},
                "ErrorDocument": {"Key": error},
            },
        )

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
        logger.info("Website hosting enabled on %s", bucket_name)

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

    def get_website_endpoint(self, bucket_name: str) -> str:
        return _website_endpoint(bucket_name, self.region)
