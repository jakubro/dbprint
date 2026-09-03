"""Rendering for the CLI - render-mode detection + generate progress renderers."""

from __future__ import annotations

from .detect import RenderMode, resolve_render_mode
from .progress import (
    ProgressRenderer,
    build_progress_renderer,
    install_log_handler,
    remove_log_handler,
    supports_live,
)


__all__ = [
    "ProgressRenderer",
    "RenderMode",
    "build_progress_renderer",
    "install_log_handler",
    "remove_log_handler",
    "resolve_render_mode",
    "supports_live",
]
