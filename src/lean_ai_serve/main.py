"""FastAPI application entrypoint with lifespan management."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from lean_ai_serve import __version__
from lean_ai_serve.config import get_settings, load_settings, set_settings
from lean_ai_serve.db import Database, get_database_url
from lean_ai_serve.engine.lifecycle import LifecycleManager, RequestTracker
from lean_ai_serve.engine.process import ProcessManager
from lean_ai_serve.engine.proxy import close_proxy_client
from lean_ai_serve.engine.router import Router
from lean_ai_serve.models.downloader import ModelDownloader
from lean_ai_serve.models.registry import ModelRegistry
from lean_ai_serve.models.schemas import ModelState
from lean_ai_serve.observability.logging import setup_logging
from lean_ai_serve.security.audit import AuditLogger
from lean_ai_serve.security.auth import load_revoked_tokens
from lean_ai_serve.security.usage import UsageTracker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    settings = get_settings()

    # Set up structured logging early (before any log output)
    setup_logging(
        json_output=settings.logging.json_output,
        log_level=settings.logging.level,
    )

    logger.info("lean-ai-serve %s starting", __version__)

    # --- Startup ---

    # Database
    db = Database(get_database_url(settings))
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

    # OIDC validator (if configured)
    if "oidc" in settings.security.mode.lower():
        from lean_ai_serve.security.oidc import OIDCValidator

        oidc_validator = OIDCValidator(settings.security.oidc)
        await oidc_validator.initialize()
        app.state.oidc_validator = oidc_validator
        logger.info("OIDC authentication enabled (issuer=%s)", settings.security.oidc.issuer_url)

    # Metrics collector (may already exist from create_app middleware setup)
    metrics = getattr(app.state, "metrics", None)
    if settings.metrics.enabled and metrics is None:
        from lean_ai_serve.observability.metrics import MetricsCollector

        metrics = MetricsCollector()
        app.state.metrics = metrics
    if metrics is not None:
        logger.info("Prometheus metrics enabled")

    # Alert evaluator
    alert_evaluator = None
    if settings.alerts.enabled and metrics is not None:
        from lean_ai_serve.observability.alerts import AlertEvaluator, AlertRule

        rules = None
        if settings.alerts.rules:
            rules = [
                AlertRule(
                    name=r.name,
                    metric=r.metric,
                    condition=r.condition,
                    threshold=r.threshold,
                    severity=r.severity,
                )
                for r in settings.alerts.rules
            ]
        alert_evaluator = AlertEvaluator(metrics, rules=rules)
        app.state.alert_evaluator = alert_evaluator
        logger.info("Alert evaluator enabled (%d rules)", len(alert_evaluator._rules))

    # OpenTelemetry tracing (optional dependency)
    if settings.tracing.enabled:
        from lean_ai_serve.observability.tracing import setup_tracing

        if setup_tracing(settings.tracing):
            logger.info("OpenTelemetry tracing enabled")

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

    # Rate limiter (for background scheduler cleanup)
    rate_limiter = getattr(app.state, "rate_limiter", None)

    # Background scheduler
    from lean_ai_serve.observability.tasks import BackgroundScheduler

    scheduler = BackgroundScheduler(
        db,
        settings,
        metrics=metrics,
        rate_limiter=rate_limiter,
        usage_tracker=usage_tracker,
        alert_evaluator=alert_evaluator,
    )
    await scheduler.start()
    app.state.background_scheduler = scheduler

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

    # --- Shutdown (ordered, with timeout guards) ---
    logger.info("lean-ai-serve shutting down")
    shutdown_timeout = 15.0  # Per-component timeout

    async def _safe_close(name: str, coro) -> None:
        """Run a shutdown coroutine with a timeout guard."""
        try:
            await asyncio.wait_for(coro, timeout=shutdown_timeout)
            logger.debug("Shutdown: %s closed", name)
        except TimeoutError:
            logger.warning("Shutdown: %s timed out after %.0fs", name, shutdown_timeout)
        except Exception:
            logger.exception("Shutdown: %s failed", name)

    # 1. Stop background scheduler first (prevents new tasks)
    await _safe_close("background-scheduler", scheduler.stop())

    # 2. Close external auth connectors (independent, run in parallel)
    auth_tasks = []
    adapter_reg = getattr(app.state, "adapter_registry", None)
    if adapter_reg:
        auth_tasks.append(_safe_close("adapter-registry", adapter_reg.close()))
    ldap_svc = getattr(app.state, "ldap_service", None)
    if ldap_svc:
        auth_tasks.append(_safe_close("ldap", ldap_svc.close()))
    oidc_val = getattr(app.state, "oidc_validator", None)
    if oidc_val:
        auth_tasks.append(_safe_close("oidc", oidc_val.close()))
    if auth_tasks:
        await asyncio.gather(*auth_tasks)

    # 3. Stop lifecycle manager before process manager
    await _safe_close("lifecycle-manager", lifecycle.stop())

    # 4. Stop vLLM processes
    await _safe_close("process-manager", pm.close())

    # 5. Close HTTP clients
    await _safe_close("proxy-client", close_proxy_client())

    # 6. Close database last (other components may have final writes)
    await _safe_close("database", db.close())

    logger.info("Shutdown complete")


def create_app(config_path: str | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if config_path:
        settings = load_settings(config_path)
        set_settings(settings)

    settings = get_settings()

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
    from lean_ai_serve.api.metrics import router as metrics_router
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
    app.include_router(metrics_router)

    # Training router (conditional on config)
    if settings.training.enabled:
        from lean_ai_serve.api.training import router as training_router

        app.include_router(training_router)

    # Dashboard UI (opt-in, enabled by default)
    if settings.dashboard.enabled:
        from starlette.staticfiles import StaticFiles

        from lean_ai_serve.dashboard import get_static_dir
        from lean_ai_serve.dashboard.api_views import router as dashboard_api_router
        from lean_ai_serve.dashboard.routes import router as dashboard_router

        app.include_router(dashboard_router)
        app.include_router(dashboard_api_router)
        app.mount("/static", StaticFiles(directory=str(get_static_dir())), name="static")

    # Middlewares (order: outermost first)
    # Execution order for incoming requests: request-id → metrics → content-filter → compression
    if settings.context_compression.enabled:
        from lean_ai_serve.middleware.compression import CompressionMiddleware, ContextCompressor

        compressor = ContextCompressor(settings.context_compression)
        app.add_middleware(CompressionMiddleware, compressor=compressor)

    if settings.security.content_filtering.enabled:
        from lean_ai_serve.security.content_filter import ContentFilter, ContentFilterMiddleware

        cf = ContentFilter(settings.security.content_filtering)
        app.add_middleware(ContentFilterMiddleware, content_filter=cf)

    if settings.metrics.enabled:
        from lean_ai_serve.observability.metrics import MetricsCollector
        from lean_ai_serve.observability.middleware import MetricsMiddleware

        # Create collector here so middleware and lifespan share the same instance
        metrics = MetricsCollector()
        app.state.metrics = metrics
        app.add_middleware(MetricsMiddleware, metrics=metrics)

    from lean_ai_serve.observability.logging import RequestIDMiddleware

    app.add_middleware(RequestIDMiddleware)

    return app


# Default app instance for uvicorn
app = create_app()
