"""Data-driven external dependency evidence and URL validation.

Only import/constructor origins are used; receiver names are never providers.
"""

from __future__ import annotations

import re

SDK_ORIGINS = {
    "boto3": "aws",
    "botocore": "aws",
    "google.ads": "google.ads",
    "google.cloud": "google.cloud",
    "datadog": "datadog",
    "statsd": "statsd",
    "redis": "redis",
    "kafka": "kafka",
    "confluent_kafka": "kafka",
    "elasticsearch": "elasticsearch",
    "sqlite3": "database.sqlite",
    "psycopg": "database.postgres",
    "psycopg2": "database.postgres",
    "pymysql": "database.mysql",
    "MySQLdb": "database.mysql",
}


def sdk_provider(origin: str, argument: str = "") -> str:
    if origin.startswith("sdk:"):
        return origin[4:]
    for prefix, provider in SDK_ORIGINS.items():
        if origin == prefix or origin.startswith(prefix + "."):
            if (
                prefix in {"boto3", "botocore"}
                and argument
                and re.fullmatch(r"[a-z0-9_-]+", argument)
            ):
                return provider + "." + argument
            return provider
    return ""


def url_shaped(value: str) -> bool:
    if (
        not value
        or re.search(r"\s", value)
        or re.match(r"(?i)^(SELECT|INSERT|UPDATE|DELETE|SHOW|WITH)\b", value)
    ):
        return False
    if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/{}]+", value):
        return True
    if value.startswith("/"):
        return True
    # A symbolic base may be followed by a dynamic path, but a bare env value
    # does not prove HTTP (it can name a file, bucket, resource id, etc.).
    return bool(re.match(r"^\{(?:env|setting|attr|call):[^}]+\}(?:/|\{|[^{}]+/)", value))
