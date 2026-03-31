"""CLI subcommands for audit log management."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from lean_ai_serve.cli.main import _init_settings, _run

audit_app = typer.Typer(help="Query and manage audit logs.")
console = Console()


@audit_app.command("query")
def audit_query(
    user_id: str = typer.Option(None, "--user", help="Filter by user"),
    action: str = typer.Option(None, "--action", help="Filter by action"),
    model: str = typer.Option(None, "--model", help="Filter by model"),
    limit: int = typer.Option(20, "--limit", help="Max entries to show"),
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """Query recent audit log entries."""
    _init_settings(config)

    async def _query():
        from lean_ai_serve.config import get_settings
        from lean_ai_serve.db import Database
        from lean_ai_serve.security.audit import AuditLogger

        settings = get_settings()
        db = Database(Path(settings.cache.directory) / "lean_ai_serve.db")
        await db.connect()

        audit = AuditLogger(db)
        await audit.initialize()
        entries, total = await audit.query(
            user_id=user_id, action=action, model=model, limit=limit
        )

        table = Table(title=f"Audit Log ({total} total)")
        table.add_column("Time", style="dim")
        table.add_column("User")
        table.add_column("Action")
        table.add_column("Model")
        table.add_column("Status")
        table.add_column("Latency")

        for e in entries:
            status_color = "green" if e["status"] == "success" else "red"
            table.add_row(
                e["timestamp"][:19],
                e["user_id"],
                e["action"],
                e["model"] or "-",
                f"[{status_color}]{e['status']}[/{status_color}]",
                f"{e['latency_ms']}ms" if e["latency_ms"] else "-",
            )

        console.print(table)
        await db.close()

    _run(_query())


@audit_app.command("verify")
def audit_verify(
    limit: int = typer.Option(1000, "--limit", help="Number of entries to verify"),
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """Verify audit log hash chain integrity."""
    _init_settings(config)

    async def _verify():
        from lean_ai_serve.config import get_settings
        from lean_ai_serve.db import Database
        from lean_ai_serve.security.audit import AuditLogger

        settings = get_settings()
        db = Database(Path(settings.cache.directory) / "lean_ai_serve.db")
        await db.connect()

        audit = AuditLogger(db)
        await audit.initialize()
        is_valid, message = await audit.verify_chain(limit=limit)

        if is_valid:
            console.print(f"[green]{message}[/green]")
        else:
            console.print(f"[red]INTEGRITY FAILURE: {message}[/red]")

        await db.close()

    _run(_verify())
