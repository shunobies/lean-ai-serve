"""CLI entry point — Typer application for lean-ai-serve."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from lean_ai_serve.config import load_settings, set_settings

app = typer.Typer(
    name="lean-ai-serve",
    help="Secure vLLM inference, model management & fine-tuning server.",
    no_args_is_help=True,
)
console = Console()


def _run(coro):
    """Run an async function synchronously."""
    return asyncio.run(coro)


def _init_settings(config: str | None = None):
    """Load and set global settings."""
    settings = load_settings(config)
    set_settings(settings)
    return settings


# ---------------------------------------------------------------------------
# lean-ai-serve start
# ---------------------------------------------------------------------------


@app.command()
def start(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
    host: str = typer.Option(None, "--host", help="Override bind host"),
    port: int = typer.Option(None, "--port", "-p", help="Override bind port"),
):
    """Start the lean-ai-serve server."""
    import uvicorn

    settings = _init_settings(config)

    kwargs: dict = {
        "host": host or settings.server.host,
        "port": port or settings.server.port,
        "log_level": "info",
    }

    # TLS passthrough
    if settings.server.tls.enabled:
        if not settings.server.tls.cert_file or not settings.server.tls.key_file:
            console.print(
                "[red]TLS enabled but cert_file or key_file not set in config[/red]"
            )
            raise typer.Exit(1)
        kwargs["ssl_certfile"] = settings.server.tls.cert_file
        kwargs["ssl_keyfile"] = settings.server.tls.key_file
        console.print("[green]TLS enabled[/green]")

    uvicorn.run(
        "lean_ai_serve.main:create_app",
        factory=True,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# lean-ai-serve pull <source> [--name NAME]
# ---------------------------------------------------------------------------


@app.command()
def pull(
    source: str = typer.Argument(help="HuggingFace model ID (e.g. Qwen/Qwen3-Coder-30B-A3B)"),
    name: str = typer.Option(None, "--name", "-n", help="Friendly name for the model"),
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """Download a model from HuggingFace Hub."""
    _init_settings(config)

    async def _pull():
        from lean_ai_serve.config import ModelConfig, get_settings
        from lean_ai_serve.db import Database
        from lean_ai_serve.models.downloader import ModelDownloader
        from lean_ai_serve.models.registry import ModelRegistry
        from lean_ai_serve.models.schemas import ModelState

        settings = get_settings()
        db = Database(Path(settings.cache.directory) / "lean_ai_serve.db")
        await db.connect()
        registry = ModelRegistry(db)
        downloader = ModelDownloader()

        model_name = name or source.split("/")[-1]
        console.print(f"[bold]Pulling {source}[/bold] as '{model_name}'")

        # Check repo exists
        if not await downloader.check_exists(source):
            console.print(f"[red]Repository not found: {source}[/red]")
            await db.close()
            raise typer.Exit(1)

        # Register
        mc = ModelConfig(source=source)
        await registry.register_model(model_name, source, mc)
        await registry.set_state(model_name, ModelState.DOWNLOADING)

        # Download with progress
        async for progress in downloader.download(source):
            if progress.status == "downloading":
                console.print(f"  {progress.message}")
            elif progress.status == "verifying":
                console.print("  Verifying...")
            elif progress.status == "complete":
                await registry.set_state(model_name, ModelState.DOWNLOADED)
                console.print(f"[green]Done![/green] {progress.message}")
            elif progress.status == "error":
                await registry.set_state(
                    model_name, ModelState.ERROR, error_message=progress.message
                )
                console.print(f"[red]Error:[/red] {progress.message}")

        await db.close()

    _run(_pull())


# ---------------------------------------------------------------------------
# lean-ai-serve models
# ---------------------------------------------------------------------------


@app.command()
def models(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """List all registered models and their status."""
    _init_settings(config)

    async def _list():
        from lean_ai_serve.config import get_settings
        from lean_ai_serve.db import Database
        from lean_ai_serve.models.registry import ModelRegistry

        settings = get_settings()
        db = Database(Path(settings.cache.directory) / "lean_ai_serve.db")
        await db.connect()
        registry = ModelRegistry(db)
        await registry.sync_from_config(settings.models)
        all_models = await registry.list_models()

        table = Table(title="Models")
        table.add_column("Name", style="bold")
        table.add_column("Source")
        table.add_column("State")
        table.add_column("GPU")
        table.add_column("Port")
        table.add_column("Task")
        table.add_column("LoRA")

        for m in all_models:
            state_color = {
                "loaded": "green",
                "downloaded": "cyan",
                "downloading": "yellow",
                "loading": "yellow",
                "error": "red",
                "not_downloaded": "dim",
            }.get(m.state.value, "white")

            table.add_row(
                m.name,
                m.source,
                f"[{state_color}]{m.state.value}[/{state_color}]",
                ",".join(str(g) for g in m.gpu),
                str(m.port or "-"),
                m.task,
                "yes" if m.enable_lora else "no",
            )

        console.print(table)
        await db.close()

    _run(_list())


# ---------------------------------------------------------------------------
# lean-ai-serve load <name>
# ---------------------------------------------------------------------------


@app.command()
def load(
    name: str = typer.Argument(help="Model name to load into vLLM"),
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """Load a downloaded model into vLLM for serving."""
    _init_settings(config)

    async def _load():
        from lean_ai_serve.config import get_settings
        from lean_ai_serve.db import Database
        from lean_ai_serve.engine.process import ProcessManager
        from lean_ai_serve.models.downloader import ModelDownloader
        from lean_ai_serve.models.registry import ModelRegistry
        from lean_ai_serve.models.schemas import ModelState

        settings = get_settings()
        db = Database(Path(settings.cache.directory) / "lean_ai_serve.db")
        await db.connect()
        registry = ModelRegistry(db)
        pm = ProcessManager()
        downloader = ModelDownloader()

        model = await registry.get_model(name)
        if model is None:
            console.print(f"[red]Model not found: {name}[/red]")
            await db.close()
            raise typer.Exit(1)

        if model.state == ModelState.LOADED:
            console.print(f"[yellow]Already loaded on port {model.port}[/yellow]")
            await db.close()
            return

        if model.state not in (ModelState.DOWNLOADED, ModelState.ERROR, ModelState.SLEEPING):
            console.print(f"[red]Cannot load — current state: {model.state.value}[/red]")
            await db.close()
            raise typer.Exit(1)

        mc = await registry.get_config(name)
        if mc is None:
            console.print("[red]Model config not found[/red]")
            await db.close()
            raise typer.Exit(1)

        model_path = downloader.get_local_path(mc.source)
        if model_path is None:
            console.print("[red]Model files not found — run pull first[/red]")
            await db.close()
            raise typer.Exit(1)

        console.print(f"Loading [bold]{name}[/bold]...")
        await registry.set_state(name, ModelState.LOADING)

        try:
            info = await pm.start(name, mc, model_path)
            await registry.set_state(name, ModelState.LOADED, port=info.port, pid=info.pid)
            console.print(
                f"[green]Model loaded![/green] port={info.port} pid={info.pid}"
            )
        except Exception as e:
            await registry.set_state(name, ModelState.ERROR, error_message=str(e))
            console.print(f"[red]Failed to load: {e}[/red]")

        await db.close()

    _run(_load())


# ---------------------------------------------------------------------------
# lean-ai-serve unload <name>
# ---------------------------------------------------------------------------


@app.command()
def unload(
    name: str = typer.Argument(help="Model name to unload"),
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """Unload a model (stop its vLLM process)."""
    _init_settings(config)

    async def _unload():
        from lean_ai_serve.config import get_settings
        from lean_ai_serve.db import Database
        from lean_ai_serve.engine.process import ProcessManager
        from lean_ai_serve.models.registry import ModelRegistry
        from lean_ai_serve.models.schemas import ModelState

        settings = get_settings()
        db = Database(Path(settings.cache.directory) / "lean_ai_serve.db")
        await db.connect()
        registry = ModelRegistry(db)
        pm = ProcessManager()

        await pm.stop(name)
        await registry.set_state(name, ModelState.DOWNLOADED)
        console.print(f"[green]Model '{name}' unloaded[/green]")
        await db.close()

    _run(_unload())


# ---------------------------------------------------------------------------
# lean-ai-serve status
# ---------------------------------------------------------------------------


@app.command()
def status(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """Show server status including GPUs and loaded models."""
    _init_settings(config)

    from lean_ai_serve.utils.gpu import get_gpu_info

    gpus = get_gpu_info()
    if not gpus:
        console.print("[dim]No NVIDIA GPUs detected[/dim]")
        return

    table = Table(title="GPUs")
    table.add_column("Index", justify="right")
    table.add_column("Name")
    table.add_column("Memory (Used/Total)")
    table.add_column("Utilization")
    table.add_column("Temp")
    table.add_column("Model")

    for g in gpus:
        table.add_row(
            str(g.index),
            g.name,
            f"{g.memory_used_mb} / {g.memory_total_mb} MB",
            f"{g.utilization_pct:.0f}%",
            f"{g.temperature_c}C" if g.temperature_c is not None else "-",
            g.model_loaded or "-",
        )

    console.print(table)


# ---------------------------------------------------------------------------
# lean-ai-serve keys (subcommand group)
# ---------------------------------------------------------------------------

keys_app = typer.Typer(help="Manage API keys.")
app.add_typer(keys_app, name="keys")


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
        from lean_ai_serve.db import Database
        from lean_ai_serve.security.auth import create_api_key

        settings = get_settings()
        db = Database(Path(settings.cache.directory) / "lean_ai_serve.db")
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
        from lean_ai_serve.db import Database

        settings = get_settings()
        db = Database(Path(settings.cache.directory) / "lean_ai_serve.db")
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
        from lean_ai_serve.db import Database

        settings = get_settings()
        db = Database(Path(settings.cache.directory) / "lean_ai_serve.db")
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


# ---------------------------------------------------------------------------
# lean-ai-serve audit (subcommand group)
# ---------------------------------------------------------------------------

audit_app = typer.Typer(help="Query and manage audit logs.")
app.add_typer(audit_app, name="audit")


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


# ---------------------------------------------------------------------------
# lean-ai-serve training (subcommand group)
# ---------------------------------------------------------------------------

training_app = typer.Typer(help="Manage training jobs, datasets, and adapters.")
app.add_typer(training_app, name="training")


@training_app.command("datasets")
def training_datasets(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """List uploaded training datasets."""
    _init_settings(config)

    async def _list():
        from lean_ai_serve.config import get_settings
        from lean_ai_serve.db import Database
        from lean_ai_serve.training.datasets import DatasetManager

        settings = get_settings()
        db = Database(Path(settings.cache.directory) / "lean_ai_serve.db")
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
        from lean_ai_serve.db import Database
        from lean_ai_serve.training.schemas import TrainingJobState

        # Minimal orchestrator for DB queries — no backend needed
        settings = get_settings()
        db = Database(Path(settings.cache.directory) / "lean_ai_serve.db")
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
        from lean_ai_serve.db import Database
        from lean_ai_serve.training.adapters import AdapterRegistry

        settings = get_settings()
        db = Database(Path(settings.cache.directory) / "lean_ai_serve.db")
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


if __name__ == "__main__":
    app()
