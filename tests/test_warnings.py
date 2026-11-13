"""A gated step fails on a warning: the suite's own filters, and `scripts/warning_lines.py`."""

from __future__ import annotations

import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from tests._scripts import REPO_ROOT


FILTER = REPO_ROOT / "scripts" / "warning_lines.py"
SITE_BUILD = REPO_ROOT / "site" / "scripts" / "build.mjs"
COLOURED_I18N_PROBE = (
    '\x1b[33m\x1b[1m21:17:42\x1b[22m [WARN] [content]\x1b[39m The collection "i18n" does not '
    "exist or is empty. Please check your content config file for errors."
)


def test_a_warning_under_the_suite_is_an_error() -> None:
    with pytest.raises(UserWarning, match="planted"):
        warnings.warn("planted", UserWarning, stacklevel=1)


def test_a_future_warning_other_than_the_named_pyspark_one_is_an_error() -> None:
    with pytest.raises(FutureWarning, match="a default"):
        warnings.warn("PySpark changes a default", FutureWarning, stacklevel=1)


def test_clean_output_passes_through_unchanged() -> None:
    text = "Checked 622 files\nAll checks passed!\n"
    run = _filter(text)

    assert (run.returncode, run.stdout, run.stderr) == (0, text, "")


@pytest.mark.parametrize(
    "line",
    [
        "warning: Failed to hardlink files; falling back to full copy.",
        "npm warn deprecated inflight@1.0.6: This module is not supported",
        "warning[unused-ignore-comment]: Unused blanket `type: ignore` directive",
        "/venv/lib/site-packages/pkg/mod.py:12: FutureWarning: a default changes",
        "26/11/12 09:14:03 WARN SparkContext: Another SparkContext is being constructed",
    ],
)
def test_a_warning_line_fails_the_step_and_is_named(line: str) -> None:
    run = _filter(f"before\n{line}\nafter\n")

    assert run.returncode == 1
    assert run.stdout == f"before\n{line}\nafter\n"
    assert line in run.stderr


def test_a_line_the_step_cannot_avoid_is_dropped() -> None:
    run = _filter("WARNING: Using incubator modules: jdk.incubator.vector\nresolved\n")

    assert (run.returncode, run.stdout) == (0, "resolved\n")


def test_output_that_is_not_utf_8_still_names_its_warning() -> None:
    run = subprocess.run(
        [sys.executable, str(FILTER)],
        input=b"\xff\xfe bytes\nwarning: a default changes\n",
        capture_output=True,
        check=False,
    )

    assert run.returncode == 1
    assert b"warning: a default changes" in run.stderr


def test_the_site_build_drops_a_coloured_starlight_probe(tmp_path: Path) -> None:
    run = _site_build(tmp_path, f"print({COLOURED_I18N_PROBE!r})\nprint('Complete!')")

    assert (run.returncode, run.stdout) == (0, "Complete!\n")


def test_a_site_build_warning_fails_the_build_and_is_named(tmp_path: Path) -> None:
    run = _site_build(tmp_path, "print('[astro] `markdown.gfm` is deprecated.')")

    assert run.returncode == 1
    assert "`markdown.gfm` is deprecated" in run.stderr


def test_a_site_build_killed_by_a_signal_fails(tmp_path: Path) -> None:
    run = _site_build(tmp_path, "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)")

    assert run.returncode != 0


def _filter(text: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(FILTER)],
        input=text,
        capture_output=True,
        text=True,
        check=False,
    )


def _site_build(tmp_path: Path, astro_body: str) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")

    if node is None:
        pytest.skip("node is not installed; the site build runs on it")

    astro = tmp_path / "astro"
    astro.write_text(f"#!{sys.executable}\n{astro_body}\n", encoding="utf-8")
    astro.chmod(0o755)

    return subprocess.run(
        [node, str(SITE_BUILD)],
        cwd=tmp_path,
        env={"PATH": f"{tmp_path}:{Path(node).parent}"},
        capture_output=True,
        text=True,
        check=False,
    )
