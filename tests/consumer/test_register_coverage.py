"""Every consumer surface asserts the whole claims register.

Each `tests/consumer/test_*.py` declares what it covers in a module-level `COVERS` frozenset;
this walks every surface listed in `_SURFACES` and fails by name on any register entry left
unasserted. `_SURFACES` is hand-maintained, not a package scan, so a module missing from it
is not checked at all.
"""

from __future__ import annotations

import pytest

from tests.consumer import (
    register,
    test_context_json,
    test_context_md,
    test_docs_site,
    test_mcp_resource,
    test_mcp_tool,
    test_reading_guide_claims,
)


# One entry per consumer surface; the module IS the surface's test suite, so a new surface
# is added here the same commit that adds its module.
_SURFACES: dict[str, frozenset[str]] = {
    "dbprint context --format md": test_context_md.COVERS,
    "dbprint context --format json": test_context_json.COVERS,
    "MCP tool channel": test_mcp_tool.COVERS,
    "MCP resource channel": test_mcp_resource.COVERS,
    "docs site": test_docs_site.COVERS,
    "reading guide": test_reading_guide_claims.COVERS,
}


@pytest.mark.parametrize("surface", sorted(_SURFACES))
def test_surface_covers_the_full_register(surface: str) -> None:
    register.assert_full_coverage(surface, _SURFACES[surface])


def test_the_register_itself_is_not_empty() -> None:
    """A register that shrank to nothing would make every surface pass vacuously."""

    assert register.REGISTER
