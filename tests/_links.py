"""A print tree holding every kind of link the containment rule (SPEC 2.5) must tell apart."""

from __future__ import annotations

from pathlib import Path


def linked_tree(tmp_path: Path) -> tuple[Path, Path]:
    """A print root and a directory beside it; the root's links point out, in, and nowhere."""

    root = tmp_path / "primary"
    outside = tmp_path / "outside"
    (root / "public" / "curator").mkdir(parents=True)
    (root / "shared").mkdir()
    outside.mkdir()
    (outside / "notes.md").write_text("outside\n", encoding="utf-8")
    (root / "shared" / "curator.md").write_text("inside\n", encoding="utf-8")
    (root / "public" / "herbarium").symlink_to(outside)
    (root / "public" / "curator" / "description.md").symlink_to(outside / "notes.md")
    (root / "public" / "curator" / "inside.md").symlink_to(root / "shared" / "curator.md")
    (root / "public" / "curator" / "gone_out.md").symlink_to(outside / "missing.md")
    (root / "public" / "curator" / "gone_in.md").symlink_to(root / "shared" / "missing.md")
    (root / "reading.md").symlink_to(outside / "notes.md")
    (root / "shared" / "ddl.sql").symlink_to(outside / "notes.md")
    (root / "shared" / "loop").symlink_to(root / "shared")
    (root / "public" / "nursery").symlink_to(root / "shared")
    (root / "public" / "seedlings").symlink_to(root / "shared")

    return root, outside


ESCAPING = frozenset(
    {
        "public/herbarium",
        "public/curator/description.md",
        "public/curator/gone_out.md",
        "reading.md",
        "shared/ddl.sql",
        "public/nursery/ddl.sql",
        "public/seedlings/ddl.sql",
    },
)
