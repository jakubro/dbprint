"""Plain-text rendering of `dbprint annotate`'s authoring brief for one table."""

from __future__ import annotations

from dbprint.engine import OpenQuestion
from dbprint.engine.yaml_dumper import spell_literal


_NONE_OPEN = (
    "No open questions: the print records an answer for everything it can detect on this table."
)


def render_brief(
    steps: str,
    context: str,
    questions: list[OpenQuestion],
    table: str,
    file_prefix: str,
) -> str:
    """The skill's steps, the table's query context, then what the print cannot answer."""

    lines = [f"## Open questions for {table}", ""]
    lines += [f"- {_question(q, file_prefix)}" for q in questions] or [_NONE_OPEN]

    return "\n\n---\n\n".join((steps.rstrip(), context.rstrip(), "\n".join(lines))) + "\n"


def _question(question: OpenQuestion, file_prefix: str) -> str:
    where = f"`{file_prefix}/{question.file}`" if file_prefix else f"`{question.file}`"
    at = f" at `{question.key}`" if question.key else ""
    spelled = ", ".join(spell_literal(v) for v in question.values)

    if question.kind == "description":
        return (
            f"No description: write {where} - the grain, where rows come from, and the rules a "
            "correct query needs."
        )

    if question.kind == "codes":
        return (
            f"`{question.column}` lists codes with no meaning ({spelled}): say what each stands "
            f"for in {where}{at}."
        )

    if question.kind == "frequent_value":
        return (
            f"`{question.column}` holds {spelled} on a large share of rows: say what it stands "
            f"for in {where}{at}."
        )

    return (
        f"Join `{question.edge}` has no note: confirm it with a note, or reject it with "
        f"`verdict: rejected`, in {where} under `{question.key}`."
    )
