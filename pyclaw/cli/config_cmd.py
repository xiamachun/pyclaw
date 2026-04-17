"""
Configuration management commands.

Supports reading, setting (dot-path), and validating ~/.pyclaw/pyclaw.json.
"""

import json
import os
import re

import click

from pyclaw.config.paths import get_paths as _get_paths
CONFIG_PATH = str(_get_paths().config_file)

SENSITIVE_KEYS = re.compile(
    r"(api_key|apiKey|token|secret|password)", re.IGNORECASE
)


def _read_config() -> dict:
    """Read the configuration file and return its contents as a dict."""
    if not os.path.isfile(CONFIG_PATH):
        raise click.ClickException(f"Configuration file not found: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as config_file:
        return json.load(config_file)


def _write_config(config: dict) -> None:
    """Write the configuration dict back to the config file."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as config_file:
        json.dump(config, config_file, indent=2, ensure_ascii=False)


def _redact_sensitive(obj: object, parent_key: str = "") -> object:
    """Recursively redact sensitive values in a config dict."""
    if isinstance(obj, dict):
        return {
            key: _redact_sensitive(value, key) for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_sensitive(item, parent_key) for item in obj]
    if SENSITIVE_KEYS.search(parent_key) and isinstance(obj, str) and obj:
        return "***REDACTED***"
    return obj


def _parse_value(value: str) -> object:
    """Auto-detect the type of a string value."""
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _set_nested(obj: dict, parts: list[str], value: object) -> None:
    """Set a value in a nested dict using a list of key parts."""
    for part in parts[:-1]:
        if part not in obj or not isinstance(obj[part], dict):
            obj[part] = {}
        obj = obj[part]
    obj[parts[-1]] = value


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group("config", help="Configuration management")
def config():
    """Configuration management commands."""
    pass


@config.command("show", help="Display current configuration (redacted)")
def show_config():
    """Display the current configuration with sensitive data redacted."""
    config_data = _read_config()
    redacted = _redact_sensitive(config_data)
    click.echo("Current configuration:")
    click.echo(json.dumps(redacted, indent=2, ensure_ascii=False))


@config.command("set", help="Set a configuration value")
@click.argument("key")
@click.argument("value")
def set_config(key, value):
    """Set a configuration value using dot-path notation.

    Examples:
        pyclaw config set agents.defaults.memorySearch.enabled true
        pyclaw config set gateway.port 9000
    """
    config_data = _read_config()

    parts = key.split(".")
    parsed_value = _parse_value(value)
    _set_nested(config_data, parts, parsed_value)

    _write_config(config_data)
    click.echo(f"Set {key} = {parsed_value!r}")


@config.command("validate", help="Validate configuration")
def validate_config():
    """Validate the current configuration against the schema."""
    config_data = _read_config()

    try:
        from pyclaw.config.schema import PyClawConfig
        PyClawConfig(**config_data)
        click.echo("Configuration is valid.")
    except Exception as err:
        raise click.ClickException(f"Configuration validation failed: {err}")
