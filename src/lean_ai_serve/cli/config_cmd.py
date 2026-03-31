"""CLI subcommands for configuration management."""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.syntax import Syntax

from lean_ai_serve.cli.main import _init_settings

config_app = typer.Typer(help="Configuration management.")
console = Console()

# Fields that should be masked when displaying config
_SENSITIVE_FIELDS = {
    "jwt_secret",
    "bind_password",
    "key",
    "encryption_key",
}


def _mask_sensitive(data: dict, depth: int = 0) -> dict:
    """Recursively mask sensitive fields in a config dict."""
    if depth > 10:
        return data
    result = {}
    for key, value in data.items():
        if any(s in key.lower() for s in _SENSITIVE_FIELDS) and isinstance(value, str) and value:
            result[key] = "***REDACTED***"
        elif isinstance(value, dict):
            result[key] = _mask_sensitive(value, depth + 1)
        elif isinstance(value, list):
            result[key] = [
                _mask_sensitive(v, depth + 1) if isinstance(v, dict) else v
                for v in value
            ]
        else:
            result[key] = value
    return result


@config_app.command("show")
def config_show(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
    raw: bool = typer.Option(False, "--raw", help="Show without masking secrets"),
):
    """Show the resolved configuration (env + YAML + defaults)."""
    settings = _init_settings(config)
    data = settings.model_dump()

    if not raw:
        data = _mask_sensitive(data)

    formatted = json.dumps(data, indent=2, default=str)
    syntax = Syntax(formatted, "json", theme="monokai", line_numbers=False)
    console.print(syntax)


@config_app.command("validate")
def config_validate(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """Validate configuration without starting the server."""
    try:
        settings = _init_settings(config)
    except Exception as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        raise typer.Exit(1) from None

    # Run semantic checks
    issues: list[str] = []

    if settings.security.mode == "oidc" and not settings.security.oidc.issuer_url:
        issues.append("OIDC mode enabled but no issuer_url configured")

    if settings.security.mode == "ldap" and not settings.security.ldap.server_url:
        issues.append("LDAP mode enabled but no server_url configured")

    if settings.tracing.enabled and not settings.tracing.endpoint:
        issues.append("Tracing enabled but no endpoint configured")

    if settings.alerts.enabled and not settings.metrics.enabled:
        issues.append("Alerts enabled but metrics disabled (alerts require metrics)")

    if issues:
        console.print("[yellow]Configuration warnings:[/yellow]")
        for issue in issues:
            console.print(f"  [yellow]⚠[/yellow] {issue}")
    else:
        console.print("[green]Configuration is valid[/green]")

    # Summary
    console.print(f"\n  Security mode: {settings.security.mode}")
    console.print(f"  Metrics: {'enabled' if settings.metrics.enabled else 'disabled'}")
    console.print(f"  Alerts: {'enabled' if settings.alerts.enabled else 'disabled'}")
    console.print(f"  Tracing: {'enabled' if settings.tracing.enabled else 'disabled'}")
    log_fmt = "JSON" if settings.logging.json_output else "console"
    console.print(f"  Logging: {settings.logging.level} ({log_fmt})")
    console.print(f"  Models: {len(settings.models)}")
    console.print(f"  Training: {'enabled' if settings.training.enabled else 'disabled'}")
