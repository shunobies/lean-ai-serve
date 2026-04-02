"""CLI subcommands for training management."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from lean_ai_serve.cli.main import _init_settings, _run

training_app = typer.Typer(help="Manage training jobs, datasets, and adapters.")
console = Console()


@training_app.command("datasets")
def training_datasets(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """List uploaded training datasets."""
    _init_settings(config)

    async def _list():
        from lean_ai_serve.config import get_settings
        from lean_ai_serve.db import Database, get_database_url
        from lean_ai_serve.training.datasets import DatasetManager

        settings = get_settings()
        db = Database(get_database_url(settings))
        await db.connect()
        dm = DatasetManager(db, settings)

        datasets = await dm.list_datasets()

        table = Table(title="Datasets")
        table.add_column("Name", style="bold")
        table.add_column("Format")
        table.add_column("Rows", justify="right")
        table.add_column("Size")
        table.add_column("Uploaded By")
        table.add_column("Created")

        for ds in datasets:
            size = f"{ds.size_bytes / 1024:.1f} KB" if ds.size_bytes else "-"
            table.add_row(
                ds.name,
                ds.format.value,
                str(ds.row_count or "-"),
                size,
                ds.uploaded_by,
                str(ds.created_at)[:10],
            )

        console.print(table)
        await db.close()

    _run(_list())


@training_app.command("jobs")
def training_jobs(
    state: str = typer.Option(None, "--state", help="Filter by state"),
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """List training jobs."""
    _init_settings(config)

    async def _list():
        from lean_ai_serve.config import get_settings
        from lean_ai_serve.db import Database, get_database_url
        from lean_ai_serve.training.schemas import TrainingJobState

        # Minimal orchestrator for DB queries — no backend needed
        settings = get_settings()
        db = Database(get_database_url(settings))
        await db.connect()

        job_state = None
        if state:
            try:
                job_state = TrainingJobState(state)
            except ValueError as e:
                console.print(f"[red]Invalid state: {state}[/red]")
                await db.close()
                raise typer.Exit(1) from e

        # Direct DB query for the CLI — avoids needing full orchestrator deps
        conditions = []
        params: list = []
        if job_state:
            conditions.append("state = ?")
            params.append(job_state.value)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        rows = await db.fetchall(
            f"SELECT * FROM training_jobs {where} ORDER BY submitted_at DESC",
            tuple(params) if params else None,
        )

        table = Table(title="Training Jobs")
        table.add_column("ID", max_width=8)
        table.add_column("Name", style="bold")
        table.add_column("Base Model")
        table.add_column("Dataset")
        table.add_column("State")
        table.add_column("Submitted By")
        table.add_column("Submitted At")

        for row in rows:
            st = row["state"]
            state_color = {
                "queued": "yellow",
                "running": "cyan",
                "completed": "green",
                "failed": "red",
                "cancelled": "dim",
            }.get(st, "white")

            table.add_row(
                row["id"][:8],
                row["name"],
                row["base_model"],
                row["dataset"],
                f"[{state_color}]{st}[/{state_color}]",
                row["submitted_by"],
                row["submitted_at"][:10] if row["submitted_at"] else "-",
            )

        console.print(table)
        await db.close()

    _run(_list())


@training_app.command("adapters")
def training_adapters(
    base_model: str = typer.Option(None, "--model", help="Filter by base model"),
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """List registered LoRA adapters."""
    _init_settings(config)

    async def _list():
        from lean_ai_serve.config import get_settings
        from lean_ai_serve.db import Database, get_database_url
        from lean_ai_serve.training.adapters import AdapterRegistry

        settings = get_settings()
        db = Database(get_database_url(settings))
        await db.connect()
        reg = AdapterRegistry(db)

        adapters = await reg.list_adapters(base_model)

        table = Table(title="Adapters")
        table.add_column("Name", style="bold")
        table.add_column("Base Model")
        table.add_column("State")
        table.add_column("Job ID", max_width=8)
        table.add_column("Created")
        table.add_column("Deployed")

        for a in adapters:
            state_color = {
                "available": "cyan",
                "deployed": "green",
                "error": "red",
            }.get(a.state.value, "white")

            table.add_row(
                a.name,
                a.base_model,
                f"[{state_color}]{a.state.value}[/{state_color}]",
                (a.training_job_id or "-")[:8],
                str(a.created_at)[:10],
                str(a.deployed_at)[:10] if a.deployed_at else "-",
            )

        console.print(table)
        await reg.close()
        await db.close()

    _run(_list())
