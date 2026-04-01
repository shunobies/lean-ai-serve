"""Web dashboard UI — HTMX + Jinja2 server-rendered frontend."""

from __future__ import annotations

from pathlib import Path


def get_templates_dir() -> Path:
    """Return the path to the Jinja2 templates directory."""
    return Path(__file__).parent / "templates"


def get_static_dir() -> Path:
    """Return the path to the static assets directory."""
    return Path(__file__).parent / "static"
