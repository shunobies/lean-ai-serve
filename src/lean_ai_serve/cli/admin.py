"""CLI subcommands for administrative tasks."""

from __future__ import annotations

import csv
import io
import json

import typer
from rich.console import Console
from rich.table import Table

from lean_ai_serve.cli.main import _init_settings, _run

admin_app = typer.Typer(help="Administrative commands.")
console = Console()


def _get_db():
    """Create a database connection from current settings."""
    from lean_ai_serve.config import get_settings
    from lean_ai_serve.db import Database, get_database_url

    settings = get_settings()
    return Database(get_database_url(settings))


# ---------------------------------------------------------------------------
# audit-verify
# ---------------------------------------------------------------------------


@admin_app.command("audit-verify")
def audit_verify(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
    limit: int = typer.Option(10000, "--limit", "-n", help="Max entries to verify"),
):
    """Verify audit log hash chain integrity."""
    _init_settings(config)

    async def _verify():
        from lean_ai_serve.security.audit import AuditLogger

        db = _get_db()
        await db.connect()
        audit = AuditLogger(db)
        await audit.initialize()

        valid, message = await audit.verify_chain(limit=limit)
        await db.close()

        if valid:
            console.print(f"[green]✓[/green] {message}")
        else:
            console.print(f"[red]✗[/red] {message}")
            raise typer.Exit(1) from None

    _run(_verify())


# ---------------------------------------------------------------------------
# audit-export
# ---------------------------------------------------------------------------


@admin_app.command("audit-export")
def audit_export(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
    output_format: str = typer.Option("json", "--format", "-f", help="Output format: json or csv"),
    from_time: str = typer.Option(None, "--from", help="Start time (ISO 8601)"),
    to_time: str = typer.Option(None, "--to", help="End time (ISO 8601)"),
    limit: int = typer.Option(1000, "--limit", "-n", help="Max entries to export"),
    output: str = typer.Option(None, "--output", "-o", help="Output file (default: stdout)"),
):
    """Export audit log entries to JSON or CSV."""
    _init_settings(config)

    async def _export():
        from datetime import datetime

        from lean_ai_serve.security.audit import AuditLogger

        db = _get_db()
        await db.connect()
        audit = AuditLogger(db)

        ft = datetime.fromisoformat(from_time) if from_time else None
        tt = datetime.fromisoformat(to_time) if to_time else None

        entries, total = await audit.query(
            from_time=ft, to_time=tt, limit=limit
        )
        await db.close()

        if not entries:
            console.print("[dim]No audit entries found[/dim]")
            return

        if output_format == "csv":
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=entries[0].keys())
            writer.writeheader()
            writer.writerows(entries)
            content = buf.getvalue()
        else:
            content = json.dumps(entries, indent=2, default=str)

        if output:
            with open(output, "w") as f:
                f.write(content)
            console.print(f"[green]Exported {len(entries)} entries to {output}[/green]")
        else:
            console.print(content)

        console.print(f"[dim]({len(entries)} of {total} total entries)[/dim]")

    _run(_export())


# ---------------------------------------------------------------------------
# token-cleanup
# ---------------------------------------------------------------------------


@admin_app.command("token-cleanup")
def token_cleanup(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """Manually clean up expired revoked JWT tokens."""
    _init_settings(config)

    async def _cleanup():
        from lean_ai_serve.security.auth import cleanup_revoked_tokens

        db = _get_db()
        await db.connect()
        removed = await cleanup_revoked_tokens(db)
        await db.close()
        console.print(f"[green]Cleaned up {removed} expired revoked tokens[/green]")

    _run(_cleanup())


# ---------------------------------------------------------------------------
# db-stats
# ---------------------------------------------------------------------------


@admin_app.command("db-stats")
def db_stats(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """Show database table sizes and row counts."""
    _init_settings(config)

    async def _stats():
        db = _get_db()
        await db.connect()

        table = Table(title="Database Statistics")
        table.add_column("Table", style="bold")
        table.add_column("Rows", justify="right")

        tables = [
            "models", "api_keys", "audit_log", "usage",
            "adapters", "training_jobs", "datasets", "revoked_tokens",
        ]
        total = 0
        for tbl in tables:
            row = await db.fetchone(f"SELECT COUNT(*) as cnt FROM {tbl}")
            count = row["cnt"] if row else 0
            total += count
            table.add_row(tbl, str(count))

        await db.close()

        console.print(table)
        console.print(f"\n  Total rows: {total}")

        # Show file size for SQLite only
        if db.dialect == "sqlite" and "///" in db.url:
            from pathlib import Path

            db_path = Path(db.url.split("///", 1)[-1])
            if db_path.exists():
                db_size = db_path.stat().st_size
                if db_size > 1024 * 1024:
                    console.print(f"  DB file size: {db_size / (1024*1024):.1f} MB")
                else:
                    console.print(f"  DB file size: {db_size / 1024:.1f} KB")
        else:
            console.print(f"  Backend: {db.dialect}")

    _run(_stats())
