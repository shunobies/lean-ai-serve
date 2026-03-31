"""FastAPI application entrypoint with lifespan management."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from lean_ai_serve import __version__
from lean_ai_serve.config import get_settings, load_settings, set_settings
from lean_ai_serve.db import Database
from lean_ai_serve.engine.lifecycle import LifecycleManager, RequestTracker
from lean_ai_serve.engine.process import ProcessManager
from lean_ai_serve.engine.proxy import close_proxy_client
from lean_ai_serve.engine.router import Router
from lean_ai_serve.models.downloader import ModelDownloader
from lean_ai_serve.models.registry import ModelRegistry
from lean_ai_serve.models.schemas import ModelState
from lean_ai_serve.security.audit import AuditLogger
from lean_ai_serve.security.auth import load_revoked_tokens
from lean_ai_serve.security.usage import UsageTracker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    settings = get_settings()
    logger.info("lean-ai-serve %s starting", __version__)

    # --- Startup ---

    # Database
    cache_dir = Path(settings.cache.directory)
    cache_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cache_dir / "lean_ai_serve.db")
    await db.connect()
    app.state.db = db

    # Model registry
    registry = ModelRegistry(db)
    await registry.sync_from_config(settings.models)
    app.state.registry = registry

    # Audit logger (with optional encryption)
    encryption_service = None
    if settings.encryption.at_rest.enabled:
        from lean_ai_serve.security.encryption import EncryptionService

        encryption_service = EncryptionService(settings.encryption.at_rest)
        app.state.encryption_service = encryption_service
        logger.info("Encryption at rest enabled")

    audit = AuditLogger(db, encryption=encryption_service)
    await audit.initialize()
    app.state.audit = audit

    # Load revoked JWT tokens into memory
    await load_revoked_tokens(db)

    # LDAP service (if configured)
    if "ldap" in settings.security.mode.lower():
        from lean_ai_serve.security.ldap_auth import LDAPService

        ldap_service = LDAPService(settings.security.ldap)
        await ldap_service.initialize()
        app.state.ldap_service = ldap_service
        logger.info("LDAP authentication enabled")

    # Model downloader
    downloader = ModelDownloader()
    app.state.downloader = downloader

    # Process manager
    pm = ProcessManager()
    app.state.process_manager = pm

    # Router
    router = Router(registry, pm)
    app.state.router = router

    # Request tracker (idle detection for lifecycle management)
    request_tracker = RequestTracker()
    app.state.request_tracker = request_tracker

    # Usage tracker
    usage_tracker = UsageTracker(db)
    app.state.usage_tracker = usage_tracker

    # Lifecycle manager (idle sleep/wake)
    lifecycle = LifecycleManager(registry, pm, request_tracker)
    await lifecycle.start()
    app.state.lifecycle_manager = lifecycle

    # Training subsystem (if enabled)
    if settings.training.enabled:
        from lean_ai_serve.training.adapters import AdapterRegistry
        from lean_ai_serve.training.backend import create_backend
        from lean_ai_serve.training.datasets import DatasetManager
        from lean_ai_serve.training.orchestrator import TrainingOrchestrator

        dataset_manager = DatasetManager(db, settings)
        app.state.dataset_manager = dataset_manager

        adapter_registry = AdapterRegistry(db)
        app.state.adapter_registry = adapter_registry

        training_backend = create_backend(settings)
        app.state.training_backend = training_backend

        orchestrator = TrainingOrchestrator(
            db, settings, training_backend, dataset_manager, adapter_registry
        )
        app.state.training_orchestrator = orchestrator

        logger.info("Training subsystem enabled (backend=%s)", training_backend.name)

    # Timing
    app.state.start_time = time.monotonic()

    # Autoload models
    for name, config in settings.models.items():
        if config.autoload:
            model = await registry.get_model(name)
            if model and model.state == ModelState.DOWNLOADED:
                logger.info("Autoloading model: %s", name)
                model_path = downloader.get_local_path(config.source)
                if model_path:
                    try:
                        await registry.set_state(name, ModelState.LOADING)
                        info = await pm.start(name, config, model_path)
                        await registry.set_state(
                            name, ModelState.LOADED, port=info.port, pid=info.pid
                        )
                    except Exception:
                        logger.exception("Failed to autoload '%s'", name)
                        await registry.set_state(
                            name, ModelState.ERROR, error_message="Autoload failed"
                        )

    logger.info("lean-ai-serve ready on %s:%d", settings.server.host, settings.server.port)

    yield

    # --- Shutdown ---
    logger.info("lean-ai-serve shutting down")

    # Close adapter registry HTTP client
    adapter_reg = getattr(app.state, "adapter_registry", None)
    if adapter_reg:
        await adapter_reg.close()

    # Close LDAP connections
    ldap_svc = getattr(app.state, "ldap_service", None)
    if ldap_svc:
        await ldap_svc.close()

    # Stop lifecycle manager before process manager
    await lifecycle.stop()

    await pm.close()
    await close_proxy_client()
    await db.close()
    logger.info("Shutdown complete")


def create_app(config_path: str | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if config_path:
        settings = load_settings(config_path)
        set_settings(settings)

    app = FastAPI(
        title="lean-ai-serve",
        description="Secure vLLM inference, model management & fine-tuning server",
        version=__version__,
        lifespan=lifespan,
    )

    # Register routers
    from lean_ai_serve.api.audit_routes import router as audit_router
    from lean_ai_serve.api.auth_routes import router as auth_router
    from lean_ai_serve.api.health import router as health_router
    from lean_ai_serve.api.keys import router as keys_router
    from lean_ai_serve.api.models import router as models_router
    from lean_ai_serve.api.openai_compat import router as openai_router
    from lean_ai_serve.api.usage import router as usage_router

    app.include_router(health_router)
    app.include_router(openai_router)
    app.include_router(models_router)
    app.include_router(keys_router)
    app.include_router(audit_router)
    app.include_router(auth_router)
    app.include_router(usage_router)

    # Training router (conditional on config)
    if settings.training.enabled:
        from lean_ai_serve.api.training import router as training_router

        app.include_router(training_router)

    # Content filter middleware (must be added after routers)
    settings = get_settings()
    if settings.security.content_filtering.enabled:
        from lean_ai_serve.security.content_filter import ContentFilter, ContentFilterMiddleware

        cf = ContentFilter(settings.security.content_filtering)
        app.add_middleware(ContentFilterMiddleware, content_filter=cf)

    return app


# Default app instance for uvicorn
app = create_app()
