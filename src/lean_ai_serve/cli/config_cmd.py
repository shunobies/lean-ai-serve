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
    "huggingface_token",
    "client_secret",
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


def _load_cli_key(config: str | None, key_file: str | None) -> bytes:
    """Load the master key from --config or --key-file for CLI commands."""
    from lean_ai_serve.security.secrets import load_key_from_file, load_master_key

    if key_file:
        return load_key_from_file(key_file)
    if config:
        import yaml

        with open(config) as f:
            data = yaml.safe_load(f) or {}
        enc_config = data.get("encryption")
        if not enc_config:
            console.print("[red]No encryption section found in config file[/red]")
            raise typer.Exit(1) from None
        return load_master_key(enc_config)
    console.print("[red]Provide --config or --key-file to locate the master key[/red]")
    raise typer.Exit(1) from None


@config_app.command("show")
def config_show(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
    raw: bool = typer.Option(False, "--raw", help="Show without masking secrets"),
):
    """Show the resolved configuration (YAML + defaults)."""
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


# ---------------------------------------------------------------------------
# Secret management commands
# ---------------------------------------------------------------------------


@config_app.command("generate-key")
def config_generate_key(
    output: str = typer.Argument(help="Path to write the 256-bit master key file"),
):
    """Generate a master key for encrypting config secrets.

    The generated key is used with ``encryption.at_rest`` to encrypt sensitive
    values in config.yaml via the ``ENC[...]`` pattern.
    """
    from lean_ai_serve.security.encryption import generate_key_file

    try:
        generate_key_file(output)
    except Exception as e:
        console.print(f"[red]Failed to generate key:[/red] {e}")
        raise typer.Exit(1) from None
    console.print(f"[green]Master key generated:[/green] {output}")
    console.print("[dim]File permissions set to 600.  Keep this file safe.[/dim]")


@config_app.command("encrypt-value")
def config_encrypt_value(
    value: str = typer.Argument(help="Plain text value to encrypt"),
    config: str = typer.Option(None, "--config", "-c", help="Config file (reads key from it)"),
    key_file: str = typer.Option(None, "--key-file", "-k", help="Direct path to key file"),
):
    """Encrypt a value for use in config.yaml as ENC[...].

    Provide either --config (reads key from encryption.at_rest section) or
    --key-file (direct path to the 256-bit key file).
    """
    from lean_ai_serve.security.secrets import encrypt_value

    try:
        key = _load_cli_key(config, key_file)
        encrypted = encrypt_value(value, key)
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Encryption failed:[/red] {e}")
        raise typer.Exit(1) from None

    console.print(f"[green]Encrypted value:[/green]\n{encrypted}")
    console.print("\n[dim]Paste this into your config.yaml[/dim]")


@config_app.command("decrypt-value")
def config_decrypt_value(
    value: str = typer.Argument(help='ENC[...] value to decrypt'),
    config: str = typer.Option(None, "--config", "-c", help="Config file (reads key from it)"),
    key_file: str = typer.Option(None, "--key-file", "-k", help="Direct path to key file"),
):
    """Decrypt an ENC[...] value from config.yaml."""
    from lean_ai_serve.security.secrets import decrypt_value

    try:
        key = _load_cli_key(config, key_file)
        decrypted = decrypt_value(value, key)
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Decryption failed:[/red] {e}")
        raise typer.Exit(1) from None

    console.print(f"[green]Decrypted value:[/green] {decrypted}")
