"""CLI subcommands for database management."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from lean_ai_serve.cli.main import _init_settings, _run

db_app = typer.Typer(help="Database setup and diagnostics.")
console = Console()


@db_app.command("init")
def db_init(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """Initialize the database — create all tables and indexes.

    Run this once after configuring a new database backend (PostgreSQL,
    Oracle, etc.).  For SQLite this happens automatically on startup.
    """
    _init_settings(config)

    async def _init():
        from lean_ai_serve.config import get_settings
        from lean_ai_serve.db import Database, get_database_url, metadata

        settings = get_settings()
        url = get_database_url(settings)
        db = Database(url)

        try:
            await db.connect()
        except Exception as e:
            console.print(f"[red]Connection failed:[/red] {e}")
            console.print(
                "\n[dim]Check your database.url in config.yaml and ensure "
                "the database server is running.[/dim]"
            )
            raise typer.Exit(1) from None

        table_count = len(metadata.tables)
        console.print(f"[green]Connected to {db.dialect} database[/green]")
        console.print(f"  Created/verified {table_count} tables")
        console.print("\n[bold green]Database ready.[/bold green]")
        await db.close()

    _run(_init())


@db_app.command("check")
def db_check(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """Verify that all expected tables exist in the database."""
    _init_settings(config)

    async def _check():
        import sqlalchemy as sa
        from sqlalchemy.ext.asyncio import create_async_engine

        from lean_ai_serve.config import get_settings
        from lean_ai_serve.db import get_database_url, metadata

        settings = get_settings()
        url = get_database_url(settings)

        try:
            engine = create_async_engine(url)
            async with engine.connect() as conn:
                existing = await conn.run_sync(
                    lambda sync_conn: sa.inspect(sync_conn).get_table_names()
                )
            await engine.dispose()
        except Exception as e:
            console.print(f"[red]Connection failed:[/red] {e}")
            raise typer.Exit(1) from None

        expected = set(metadata.tables.keys())
        found = set(existing) & expected
        missing = expected - found

        if not missing:
            console.print(
                f"[green]All {len(expected)} tables present[/green]"
            )
        else:
            console.print(f"[yellow]Found {len(found)}/{len(expected)} tables[/yellow]")
            for name in sorted(missing):
                console.print(f"  [red]Missing:[/red] {name}")
            console.print("\n[dim]Run 'lean-ai-serve db init' to create missing tables.[/dim]")
            raise typer.Exit(1) from None

    _run(_check())


@db_app.command("info")
def db_info(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """Show database connection info and table row counts."""
    _init_settings(config)

    async def _info():
        from lean_ai_serve.config import get_settings
        from lean_ai_serve.db import Database, get_database_url

        settings = get_settings()
        url = get_database_url(settings)
        db = Database(url)

        try:
            await db.connect()
        except Exception as e:
            console.print(f"[red]Connection failed:[/red] {e}")
            raise typer.Exit(1) from None

        console.print(f"  Backend:  [bold]{db.dialect}[/bold]")

        # Show file path for SQLite
        if db.dialect == "sqlite" and "///" in url:
            from pathlib import Path

            db_path = Path(url.split("///", 1)[-1])
            console.print(f"  Path:     {db_path}")
            if db_path.exists():
                size = db_path.stat().st_size
                if size > 1024 * 1024:
                    console.print(f"  Size:     {size / (1024*1024):.1f} MB")
                else:
                    console.print(f"  Size:     {size / 1024:.1f} KB")

        # Row counts
        console.print()
        table = Table(title="Tables")
        table.add_column("Table", style="bold")
        table.add_column("Rows", justify="right")

        table_names = [
            "models", "api_keys", "audit_log", "usage",
            "adapters", "training_jobs", "datasets", "revoked_tokens",
        ]
        total = 0
        for tbl in table_names:
            row = await db.fetchone(f"SELECT COUNT(*) as cnt FROM {tbl}")
            count = row["cnt"] if row else 0
            total += count
            table.add_row(tbl, str(count))

        console.print(table)
        console.print(f"\n  Total rows: {total}")
        await db.close()

    _run(_info())
