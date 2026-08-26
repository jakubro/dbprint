"""Exposes the session-scoped adversarial print to every test under tests/consumer/."""

from __future__ import annotations

from tests.fixtures.adversarial import adversarial_print


__all__ = ["adversarial_print"]
