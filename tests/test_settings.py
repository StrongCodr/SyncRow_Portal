"""Tests for settings and configuration."""

import os
import pytest

from srow.config import Settings, load_settings, load_env_file


class TestSettings:
    """Tests for the Settings dataclass."""

    def test_settings_creation(self):
        """Test creating a Settings instance."""
        settings = Settings(
            url="http://localhost:8086",
            token="my-token",
            org="my-org",
            org_id="my-org-id",
            bucket="my-bucket",
        )

        assert settings.url == "http://localhost:8086"
        assert settings.token == "my-token"
        assert settings.org == "my-org"
        assert settings.org_id == "my-org-id"
        assert settings.bucket == "my-bucket"

    def test_settings_immutable(self):
        """Test that Settings is immutable."""
        settings = Settings(
            url="http://localhost:8086",
            token="token",
            org="org",
            org_id="",
            bucket="bucket",
        )

        with pytest.raises(AttributeError):
            settings.url = "http://new-url"

    def test_effective_org_with_org_id(self):
        """Test effective_org returns org_id when set."""
        settings = Settings(
            url="http://localhost:8086",
            token="token",
            org="org-name",
            org_id="org-id-123",
            bucket="bucket",
        )

        assert settings.effective_org() == "org-id-123"

    def test_effective_org_without_org_id(self):
        """Test effective_org returns org when org_id is empty."""
        settings = Settings(
            url="http://localhost:8086",
            token="token",
            org="org-name",
            org_id="",
            bucket="bucket",
        )

        assert settings.effective_org() == "org-name"


class TestLoadEnvFile:
    """Tests for load_env_file function."""

    def test_load_simple_env(self, tmp_path):
        """Test loading a simple .env file."""
        env_path = tmp_path / ".env"
        env_path.write_text("FOO=bar\nBAZ=qux")

        environ = {}
        load_env_file(str(env_path), environ)

        assert environ["FOO"] == "bar"
        assert environ["BAZ"] == "qux"

    def test_load_quoted_values(self, tmp_path):
        """Test loading values with quotes."""
        env_path = tmp_path / ".env"
        env_path.write_text('SINGLE=\'single quoted\'\nDOUBLE="double quoted"')

        environ = {}
        load_env_file(str(env_path), environ)

        assert environ["SINGLE"] == "single quoted"
        assert environ["DOUBLE"] == "double quoted"

    def test_load_with_export(self, tmp_path):
        """Test loading values with export keyword."""
        env_path = tmp_path / ".env"
        env_path.write_text("export FOO=bar")

        environ = {}
        load_env_file(str(env_path), environ)

        assert environ["FOO"] == "bar"

    def test_load_with_comments(self, tmp_path):
        """Test loading ignores comments."""
        env_path = tmp_path / ".env"
        env_path.write_text("# This is a comment\nFOO=bar # inline comment")

        environ = {}
        load_env_file(str(env_path), environ)

        assert environ["FOO"] == "bar"
        assert "# This is a comment" not in environ

    def test_does_not_override_existing(self, tmp_path):
        """Test that existing env vars are not overridden."""
        env_path = tmp_path / ".env"
        env_path.write_text("FOO=new-value")

        environ = {"FOO": "existing-value"}
        load_env_file(str(env_path), environ)

        assert environ["FOO"] == "existing-value"

    def test_missing_file_is_silent(self, tmp_path):
        """Test that missing file doesn't raise error."""
        environ = {}
        load_env_file(str(tmp_path / "nonexistent.env"), environ)
        # Should not raise, environ unchanged
        assert environ == {}


class TestLoadSettings:
    """Tests for load_settings function."""

    def test_load_settings_from_env_file(self, env_file, monkeypatch):
        """Test loading settings from .env file."""
        # Clear any existing env vars
        for key in ["INFLUX_URL", "INFLUX_TOKEN", "INFLUX_ORG", "INFLUX_ORG_ID", "INFLUX_BUCKET"]:
            monkeypatch.delenv(key, raising=False)

        settings = load_settings(env_file)

        assert settings.url == "http://localhost:8086"
        assert settings.token == "test-token"
        assert settings.org == "test-org"
        assert settings.bucket == "test-bucket"

    def test_load_settings_missing_required(self, tmp_path, monkeypatch):
        """Test that missing required settings raises ValueError."""
        # Clear any existing env vars
        for key in ["INFLUX_URL", "INFLUX_TOKEN", "INFLUX_ORG", "INFLUX_ORG_ID", "INFLUX_BUCKET"]:
            monkeypatch.delenv(key, raising=False)

        env_path = tmp_path / ".env"
        env_path.write_text("INFLUX_URL=http://localhost:8086")

        with pytest.raises(ValueError) as exc_info:
            load_settings(str(env_path))

        assert "INFLUX_TOKEN" in str(exc_info.value)
