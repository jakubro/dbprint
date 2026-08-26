"""Python sources are ASCII outside string literals.

Docstrings and comments are prose the codebase owns, so they stay ASCII. String literals are
exempt as a category, because their contents are data bound for somewhere else whose charset
the codebase does not decide - `spec_ref` values reach the terminal verbatim and the format
specification illustrates that field with a section sign.

The split is drawn from the AST, not per-line markers: a string constant that is the first
statement of a module, class or function is a docstring; every other one is a literal.
`RUF001`-`RUF003` flag only characters confusable with ASCII, so they miss the em dash, the
section sign and the box-drawing dash; they stay on as defence against homoglyphs. The bans
are written as code points, so this module's own source needs no exemption from itself.
"""

from __future__ import annotations

import ast
import io
import subprocess
import tokenize
from dataclasses import dataclass
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

# Named by code point so this file stays ASCII. The rule bans every non-ASCII character;
# these are the likely-accidental ones, kept for the message and the planted-violation tests.
SECTION_SIGN = chr(0x00A7)
EN_DASH = chr(0x2013)
EM_DASH = chr(0x2014)
BOX_DRAWING_DASH = chr(0x2500)

KNOWN_DRIFT = {
    SECTION_SIGN: "section sign; write the bare section number instead",
    EN_DASH: "en dash; use an ASCII hyphen",
    EM_DASH: "em dash; use an ASCII hyphen",
    BOX_DRAWING_DASH: "box-drawing dash; use ASCII hyphens",
}


@dataclass(frozen=True)
class Offence:
    """One non-ASCII character in prose, located precisely enough to fix."""

    path: str
    line: int
    kind: str
    char: str
    excerpt: str

    def render(self) -> str:
        hint = KNOWN_DRIFT.get(self.char, "non-ASCII character")
        code = f"U+{ord(self.char):04X}"

        return f"{self.path}:{self.line}: [{self.kind}] {code} ({hint}) in {self.excerpt.strip()[:80]!r}"


def prose_offences(source: str, path: str) -> list[Offence]:
    """Every non-ASCII character in a docstring or comment of `source`.

    Non-docstring literals are skipped: their contents are data the codebase does not own.
    """

    offences: list[Offence] = []
    lines = source.splitlines()

    def excerpt(lineno: int) -> str:
        return lines[lineno - 1] if 0 < lineno <= len(lines) else ""

    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        if not node.body:
            continue

        first = node.body[0]

        if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
            continue

        if not isinstance(first.value.value, str):
            continue

        for offset, text in enumerate(first.value.value.splitlines()):
            for char in text:
                if ord(char) > 127:
                    lineno = first.value.lineno + offset
                    offences.append(
                        Offence(path, lineno, "docstring", char, excerpt(lineno) or text),
                    )

    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue

        for char in token.string:
            if ord(char) > 127:
                offences.append(
                    Offence(path, token.start[0], "comment", char, excerpt(token.start[0])),
                )

    return offences


def tracked_python_files() -> list[str]:
    """Repo-relative paths of every tracked `.py` file.

    `git ls-files` searches upward, so a tree sitting inside an unrelated checkout lists
    nothing and would pass having scanned zero files; the listing counts only when git
    reports this directory as the repo root, otherwise the walk takes over.
    """

    if _git_toplevel() == REPO_ROOT.resolve():
        listed = _git(["ls-files", "-z", "*.py"])

        if listed is not None:
            return [entry for entry in listed.split("\0") if entry]

    return [
        p.relative_to(REPO_ROOT).as_posix()
        for p in REPO_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts and ".venv" not in p.parts
    ]


def _git_toplevel() -> Path | None:
    """The repository root git resolves for this tree, or None if there is none."""

    output = _git(["rev-parse", "--show-toplevel"])

    return Path(output.strip()).resolve() if output and output.strip() else None


def _git(args: list[str]) -> str | None:
    """Run a git command in the repo root, returning stdout or None on failure."""

    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return None

    return completed.stdout if completed.returncode == 0 else None


def scan_repo() -> list[Offence]:
    """Every prose offence across every tracked Python file."""

    offences: list[Offence] = []

    for relative in tracked_python_files():
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        offences.extend(prose_offences(source, relative))

    return offences


class TestSourcesAreAscii:
    def test_no_non_ascii_in_docstrings_or_comments(self) -> None:
        offences = scan_repo()

        assert not offences, "\n".join(o.render() for o in offences)

    def test_the_scan_actually_read_the_tree(self) -> None:
        """A clean result is only meaningful if files were enumerated."""

        files = tracked_python_files()

        assert len(files) > 100, f"expected the whole source tree, got {len(files)} files"
        assert "src/dbprint/cli/main.py" in files

    def test_a_tree_inside_an_unrelated_checkout_is_walked_not_listed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """git searches upward, so an empty listing must not read as "no files"."""

        (tmp_path / "mod.py").write_text(f'"""Doc {EM_DASH} here."""\n', encoding="utf-8")
        monkeypatch.setattr("tests.test_charset.REPO_ROOT", tmp_path)

        assert tracked_python_files() == ["mod.py"]
        assert [o.char for o in scan_repo()] == [EM_DASH]


class TestEnforcementIsLive:
    @pytest.mark.parametrize("char", sorted(KNOWN_DRIFT))
    def test_docstring_violation_is_caught(self, char: str) -> None:
        source = f'"""Summary with {char} in it."""\n'
        found = prose_offences(source, "probe.py")

        assert [o.kind for o in found] == ["docstring"]
        assert found[0].char == char
        assert found[0].line == 1

    @pytest.mark.parametrize("char", sorted(KNOWN_DRIFT))
    def test_comment_violation_is_caught(self, char: str) -> None:
        source = f"x = 1  # trailing note with {char}\n"
        found = prose_offences(source, "probe.py")

        assert [o.kind for o in found] == ["comment"]
        assert found[0].char == char

    def test_violation_in_a_nested_function_docstring_is_caught(self) -> None:
        source = f'def outer():\n    def inner():\n        """Doc {EM_DASH} here."""\n\n    return inner\n'
        found = prose_offences(source, "probe.py")

        assert [o.kind for o in found] == ["docstring"]
        assert found[0].line == 3

    def test_multiline_docstring_reports_the_offending_line(self) -> None:
        source = f'"""Summary.\n\nSecond paragraph with {SECTION_SIGN} in it.\n"""\n'
        found = prose_offences(source, "probe.py")

        assert len(found) == 1
        assert found[0].line == 3

    def test_failure_message_names_file_line_and_character(self) -> None:
        found = prose_offences(f"# note {EM_DASH}\n", "some/file.py")
        rendered = found[0].render()

        assert "some/file.py:1" in rendered
        assert "U+2014" in rendered
        assert "comment" in rendered


class TestLiteralsAreExempt:
    def test_plain_literal_is_not_an_offence(self) -> None:
        assert prose_offences(f'X = "value with {SECTION_SIGN}2.5"\n', "probe.py") == []

    def test_spec_ref_shape_is_not_an_offence(self) -> None:
        """The real shape: a keyword argument carrying a spec citation."""

        source = f'issue = Issue(spec_ref="ASSERTIONS.md {SECTION_SIGN}3")\n'

        assert prose_offences(source, "probe.py") == []

    def test_a_literal_that_merely_follows_a_docstring_is_exempt(self) -> None:
        """Only the FIRST statement is a docstring; a later string is a literal."""

        source = f'"""Clean summary."""\n\nX = "later value with {EM_DASH}"\n'

        assert prose_offences(source, "probe.py") == []

    def test_the_exemption_does_not_swallow_the_docstring(self) -> None:
        """A file with both must still report the docstring."""

        source = f'"""Summary {EM_DASH} here."""\n\nX = "literal {EM_DASH} fine"\n'
        found = prose_offences(source, "probe.py")

        assert [o.kind for o in found] == ["docstring"]
        assert found[0].line == 1


class TestThisModuleNeedsNoExemption:
    def test_its_own_source_is_pure_ascii(self) -> None:
        """The bans are written as code points, so this file is its own subject."""

        source = Path(__file__).read_text(encoding="utf-8")
        non_ascii = {c for c in source if ord(c) > 127}

        assert not non_ascii, f"this module must stay ASCII; found {non_ascii}"
