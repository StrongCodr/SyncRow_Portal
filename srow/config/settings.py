"""Application settings and configuration.

Settings are loaded from environment variables, typically set via a .env file.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from .env_utils import load_env_file


# Default cache directory
DEFAULT_CACHE_DIR = Path.home() / ".srow" / "cache"

# Example .env file content for documentation
EXAMPLE_ENV = """
INFLUX_URL=https://us-east-1-1.aws.cloud2.influxdata.com
INFLUX_TOKEN=<your-token>
INFLUX_ORG=Self
INFLUX_ORG_ID=<your-org-id>
INFLUX_BUCKET=syncrow
CACHE_ENABLED=true
CACHE_DIR=~/.srow/cache
""".strip()


@dataclass(frozen=True)
class Settings:
    """Immutable application settings.

    Attributes:
        url: InfluxDB server URL.
        token: InfluxDB API token.
        org: InfluxDB organization name.
        org_id: InfluxDB organization ID (preferred over org).
        bucket: InfluxDB bucket name.
        cache_enabled: Whether local caching is enabled.
        cache_dir: Path to local cache directory.
    """

    url: str
    token: str
    org: str
    org_id: str
    bucket: str
    cache_enabled: bool = True
    cache_dir: Path = DEFAULT_CACHE_DIR
    # How far back interval list/load queries look. Data is often months old,
    # so the default is generous; override with INFLUX_LOOKBACK (Flux duration).
    query_lookback: str = "-3650d"

    def effective_org(self) -> str:
        """Return the organization identifier to use for API calls.

        Prefers org_id if set, otherwise falls back to org name.
        """
        return self.org_id or self.org


def load_settings(env_path: str = ".env") -> Settings:
    """Load settings from environment variables.

    Args:
        env_path: Path to .env file to load. Defaults to ".env".

    Returns:
        Settings instance with loaded configuration.

    Raises:
        ValueError: If required settings are missing.
    """
    load_env_file(env_path)

    url = os.getenv("INFLUX_URL", "")
    token = os.getenv("INFLUX_TOKEN", "")
    org = os.getenv("INFLUX_ORG", "")
    org_id = os.getenv("INFLUX_ORG_ID", "")
    bucket = os.getenv("INFLUX_BUCKET", "")

    # Cache settings (optional, with defaults)
    cache_enabled_str = os.getenv("CACHE_ENABLED", "true").lower()
    cache_enabled = cache_enabled_str in ("true", "1", "yes")

    cache_dir_str = os.getenv("CACHE_DIR", "")
    if cache_dir_str:
        cache_dir = Path(cache_dir_str).expanduser()
    else:
        cache_dir = DEFAULT_CACHE_DIR

    query_lookback = os.getenv("INFLUX_LOOKBACK", "-3650d")

    # Validate required fields
    missing = []
    if not url:
        missing.append("INFLUX_URL")
    if not token:
        missing.append("INFLUX_TOKEN")
    if not (org or org_id):
        missing.append("INFLUX_ORG or INFLUX_ORG_ID")
    if not bucket:
        missing.append("INFLUX_BUCKET")

    if missing:
        raise ValueError(
            f"Missing InfluxDB settings: {', '.join(missing)}. "
            f"Please create a .env file with the required variables.\n\n"
            f"Example:\n{EXAMPLE_ENV}"
        )

    return Settings(
        url=url,
        token=token,
        org=org,
        org_id=org_id,
        bucket=bucket,
        cache_enabled=cache_enabled,
        cache_dir=cache_dir,
        query_lookback=query_lookback,
    )
