"""CLI subcommands for API key management."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from lean_ai_serve.cli.main import _init_settings, _run

keys_app = typer.Typer(help="Manage API keys.")
console = Console()


@keys_app.command("create")
def keys_create(
    key_name: str = typer.Option(..., "--name", help="Name for the API key"),
    role: str = typer.Option("user", "--role", help="Role: admin, model-manager, trainer, user"),
    models_list: str = typer.Option("*", "--models", help="Comma-separated model names or *"),
    rate_limit: int = typer.Option(0, "--rate-limit", help="Requests per minute (0=unlimited)"),
    expires_days: int = typer.Option(None, "--expires", help="Expire after N days"),
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """Create a new API key."""
    _init_settings(config)

    async def _create():
        from lean_ai_serve.config import get_settings
        from lean_ai_serve.db import Database, get_database_url
        from lean_ai_serve.security.auth import create_api_key

        settings = get_settings()
        db = Database(get_database_url(settings))
        await db.connect()

        allowed_models = [m.strip() for m in models_list.split(",")]
        key_id, raw_key = await create_api_key(
            db,
            name=key_name,
            role=role,
            models=allowed_models,
            rate_limit=rate_limit,
            expires_days=expires_days,
        )

        console.print("\n[bold green]API Key Created[/bold green]")
        console.print(f"  Name:  {key_name}")
        console.print(f"  Role:  {role}")
        console.print(f"  ID:    {key_id}")
        console.print(f"  Key:   [bold]{raw_key}[/bold]")
        console.print("\n[yellow]Save this key — it cannot be retrieved later.[/yellow]")
        await db.close()

    _run(_create())


@keys_app.command("list")
def keys_list(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """List all API keys."""
    _init_settings(config)

    async def _list():
        from lean_ai_serve.config import get_settings
        from lean_ai_serve.db import Database, get_database_url

        settings = get_settings()
        db = Database(get_database_url(settings))
        await db.connect()

        rows = await db.fetchall("SELECT * FROM api_keys ORDER BY created_at DESC")

        table = Table(title="API Keys")
        table.add_column("Name", style="bold")
        table.add_column("Prefix")
        table.add_column("Role")
        table.add_column("Models")
        table.add_column("Rate Limit")
        table.add_column("Created")
        table.add_column("Expires")
        table.add_column("Last Used")

        for row in rows:
            table.add_row(
                row["name"],
                row["key_prefix"],
                row["role"],
                row["models"],
                str(row["rate_limit"]) if row["rate_limit"] else "unlimited",
                row["created_at"][:10] if row["created_at"] else "-",
                row["expires_at"][:10] if row["expires_at"] else "never",
                row["last_used_at"][:10] if row["last_used_at"] else "never",
            )

        console.print(table)
        await db.close()

    _run(_list())


@keys_app.command("revoke")
def keys_revoke(
    key_prefix: str = typer.Argument(help="Key prefix or ID to revoke"),
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """Revoke an API key by prefix or ID."""
    _init_settings(config)

    async def _revoke():
        from lean_ai_serve.config import get_settings
        from lean_ai_serve.db import Database, get_database_url

        settings = get_settings()
        db = Database(get_database_url(settings))
        await db.connect()

        # Try by ID first, then by prefix
        result = await db.execute("DELETE FROM api_keys WHERE id = ?", (key_prefix,))
        if result.rowcount == 0:
            result = await db.execute(
                "DELETE FROM api_keys WHERE key_prefix LIKE ?",
                (f"{key_prefix}%",),
            )
        await db.commit()

        if result.rowcount > 0:
            console.print(f"[green]Revoked {result.rowcount} key(s)[/green]")
        else:
            console.print("[red]No matching keys found[/red]")

        await db.close()

    _run(_revoke())
