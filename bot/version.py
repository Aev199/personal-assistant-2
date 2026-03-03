"""
Application version information.

This module provides version tracking following semantic versioning (MAJOR.MINOR.PATCH).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import sys


@dataclass
class VersionInfo:
    """Application version information.
    
    Attributes:
        version: Semantic version string (e.g., "1.0.0")
        commit_hash: Git commit SHA (7-40 characters)
        build_date: Build timestamp in UTC
        python_version: Python version string
        dependencies: Dictionary of package names to versions
    """
    version: str
    commit_hash: Optional[str] = None
    build_date: datetime = field(default_factory=lambda: datetime.utcnow())
    python_version: str = field(default_factory=lambda: f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.patch}")
    dependencies: dict[str, str] = field(default_factory=dict)
    
    def __str__(self) -> str:
        """Return human-readable version string."""
        parts = [f"v{self.version}"]
        if self.commit_hash:
            parts.append(f"({self.commit_hash[:7]})")
        return " ".join(parts)
    
    def to_dict(self) -> dict:
        """Convert version info to dictionary for JSON serialization."""
        return {
            "version": self.version,
            "commit_hash": self.commit_hash,
            "build_date": self.build_date.isoformat(),
            "python_version": self.python_version,
            "dependencies": self.dependencies,
        }


# Current application version
# Update this when releasing new versions
CURRENT_VERSION = VersionInfo(
    version="1.0.0",
    commit_hash=None,  # Will be populated from environment or git
    dependencies={
        # Core dependencies
        "aiogram": "3.x",
        "asyncpg": "0.29.0",
        "aiohttp": "3.9.1",
        # Integration dependencies
        "caldav": "1.3.9",
        "google-auth": "2.25.2",
        # New dependencies (Phase 1)
        "python-json-logger": "2.0.7",
        "boto3": "1.34.0",  # Optional: S3 storage
        "dropbox": "11.36.2",  # Optional: Dropbox storage
        "google-cloud-storage": "2.14.0",  # Optional: GCS storage
    }
)


def get_version() -> VersionInfo:
    """Get current application version.
    
    Returns:
        VersionInfo instance with current version information
    """
    return CURRENT_VERSION


def get_version_string() -> str:
    """Get version as a simple string.
    
    Returns:
        Version string (e.g., "v1.0.0" or "v1.0.0 (abc1234)")
    """
    return str(CURRENT_VERSION)
