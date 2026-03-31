"""CLI entry point — Typer application for lean-ai-serve."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from lean_ai_serve import __version__
from lean_ai_serve.config import load_settings, set_settings


def _version_callback(value: bool):
    if value:
        print(f"lean-ai-serve {__version__}")
        raise typer.Exit()


app = typer.Typer(
    name="lean-ai-serve",
    help="Secure vLLM inference, model management & fine-tuning server.",
    no_args_is_help=True,
)
console = Console()


@app.callback()
def main_callback(
    version: bool = typer.Option(
        None, "--version", "-V", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
):
    """Secure vLLM inference, model management & fine-tuning server."""


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
# lean-ai-serve check
# ---------------------------------------------------------------------------


@app.command()
def check(
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
):
    """Pre-flight check: validate config, GPUs, and optional dependencies."""
    from shutil import which

    settings = _init_settings(config)
    issues: list[str] = []
    ok: list[str] = []

    # Config validation
    ok.append("Config loaded successfully")

    # Security checks
    if settings.security.mode == "oidc" and not settings.security.oidc.issuer_url:
        issues.append("OIDC mode enabled but no issuer_url configured")
    if settings.security.mode == "ldap" and not settings.security.ldap.server_url:
        issues.append("LDAP mode enabled but no server_url configured")
    if settings.tracing.enabled and not settings.tracing.endpoint:
        issues.append("Tracing enabled but no endpoint configured")

    # GPU check
    from lean_ai_serve.utils.gpu import get_gpu_info

    gpus = get_gpu_info()
    if gpus:
        ok.append(f"GPUs detected: {len(gpus)}")
    else:
        issues.append("No NVIDIA GPUs detected (nvidia-ml-py may not be installed)")

    # vLLM check
    if which("python"):
        ok.append("Python available in PATH")
    else:
        issues.append("Python not found in PATH (vLLM subprocess won't work)")

    # Optional dependency checks
    try:
        import llmlingua  # noqa: F401

        ok.append("llmlingua available (context compression)")
    except ImportError:
        if settings.context_compression.enabled:
            issues.append("Context compression enabled but llmlingua not installed")

    try:
        import hvac  # noqa: F401

        ok.append("hvac available (Vault integration)")
    except ImportError:
        if settings.encryption.at_rest.key_source == "vault":
            issues.append("Vault key source configured but hvac not installed")

    # Report
    for item in ok:
        console.print(f"  [green]✓[/green] {item}")
    for issue in issues:
        console.print(f"  [yellow]⚠[/yellow] {issue}")

    if issues:
        console.print(f"\n[yellow]{len(issues)} warning(s)[/yellow]")
    else:
        console.print("\n[green]All checks passed[/green]")


# ---------------------------------------------------------------------------
# Subcommand groups (imported from separate modules)
# ---------------------------------------------------------------------------

from lean_ai_serve.cli.admin import admin_app  # noqa: E402
from lean_ai_serve.cli.audit import audit_app  # noqa: E402
from lean_ai_serve.cli.config_cmd import config_app  # noqa: E402
from lean_ai_serve.cli.keys import keys_app  # noqa: E402
from lean_ai_serve.cli.training import training_app  # noqa: E402

app.add_typer(keys_app, name="keys")
app.add_typer(audit_app, name="audit")
app.add_typer(training_app, name="training")
app.add_typer(config_app, name="config")
app.add_typer(admin_app, name="admin")


if __name__ == "__main__":
    app()
