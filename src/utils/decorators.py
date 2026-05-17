"""
decorators.py – Reusable decorators for the Modbus Server Simulator.

Provides:
    @log_errors  – Wraps a method/function so that any unhandled exception is
                   logged at ERROR level with full traceback, then re-raised.
                   Keeps call-sites clean while ensuring nothing is silently
                   swallowed.
"""

from __future__ import annotations

import functools
import logging
import traceback
from typing import Callable, TypeVar, Any

F = TypeVar("F", bound=Callable[..., Any])

logger = logging.getLogger(__name__)


def log_errors(func: F) -> F:
    """
    Decorator that logs any exception raised by *func* at ERROR level
    (including the full traceback) and then re-raises it.

    Usage
    -----
    @log_errors
    def my_method(self, ...):
        ...
    """
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception:
            logger.error(
                "Unhandled exception in %s:\n%s",
                func.__qualname__,
                traceback.format_exc(),
            )
            raise

    return wrapper  # type: ignore[return-value]


def log_errors_silent(func: F) -> F:
    """
    Variant of @log_errors that logs the exception but does NOT re-raise it.
    Returns None on failure.  Use only where a failure is non-fatal and the
    caller does not need to know about it (e.g., live-update pushes to a
    running server).
    """
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception:
            logger.error(
                "Suppressed exception in %s:\n%s",
                func.__qualname__,
                traceback.format_exc(),
            )
            return None

    return wrapper  # type: ignore[return-value]
