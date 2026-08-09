"""What to write back on the ticket when work finishes.

The ledger already knows everything worth reporting, and knows it from two
different kinds of witness. File changes and commands are recorded by hooks as
the tool call happens, so they cannot be forgotten, embellished, or quietly
omitted. Event types and rationale come from agents, and are useful but
fallible.

A summary built from the first kind is a record. One assembled from an agent's
recollection is a claim. This module reports the record, and says so on the
ticket, because the person reading it months later has no other way to tell
which they are holding.

Volume is the other half. `sync.MIRRORED` stays deliberately terse -- a comment
per build attempt makes a ticket unreadable and trains people to ignore it.
This is the opposite case: one substantial comment when the work lands, which
is the one people actually read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .bus import events as ev
from .bus.store import Store

# Headings people already write on tickets. Preserved rather than paraphrased:
# acceptance criteria are the author's words, and rewording them changes what
# was agreed.
_CRITERIA = re.compile(
    r"^\s*#{0,4}\s*(acceptance criteria|acceptance|done when|definition of done)\s*:?\s*$",
    re.I | re.M,
)


@dataclass
class Work:
    """Everything the ledger holds about one requirement."""

    issue_key: str
    title: str
    target: str | None
    body: str = ""
    design: str | None = None
    design_approved_by: str | None = None
    files: dict[str, tuple[int, int]] = field(default_factory=dict)
    commands: list[tuple[str, int | None]] = field(default_factory=list)
    outcomes: list[tuple[str, str]] = field(default_factory=list)

    @property
    def insertions(self) -> int:
        return sum(i for i, _ in self.files.values())

    @property
    def deletions(self) -> int:
        return sum(d for _, d in self.files.values())

    @property
    def failed_commands(self) -> list[tuple[str, int | None]]:
        return [(c, e) for c, e in self.commands if e not in (0, None)]


def _payload(row) -> dict:
    try:
        body = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return body if isinstance(body, dict) else {}


def collect(store: Store, run_id: str, requirement_id: int) -> Work | None:
    """Gather what happened to one requirement, from the ledger alone."""
    rows = ev.read_since(store, run_id, 0, limit=10_000)

    requirement = next((r for r in rows if int(r["id"]) == requirement_id), None)
    if requirement is None:
        return None

    body = _payload(requirement)
    work = Work(
        issue_key=body.get("issue_key", "--"),
        title=body.get("title") or (requirement["rationale"] or ""),
        target=requirement["target"],
        body=body.get("body", ""),
    )

    # Everything after the requirement, on its target. Same linkage the
    # dashboard uses, and the same limitation: target is all the ledger has.
    for row in rows:
        if int(row["id"]) <= requirement_id or row["target"] != work.target:
            continue

        kind = row["type"]
        load = _payload(row)

        if kind == "design_proposed":
            work.design = load.get("design") or row["rationale"]
        elif kind == "approved" and work.design:
            work.design_approved_by = row["agent"] or "a human"
        elif kind == "file_changed":
            path = load.get("path")
            if path:
                before = work.files.get(path, (0, 0))
                work.files[path] = (before[0] + int(load.get("insertions") or 0),
                                    before[1] + int(load.get("deletions") or 0))
        elif kind == "command_run":
            work.commands.append((load.get("argv", ""), load.get("exit")))
        elif kind in ("build_passed", "build_failed", "test_passed", "test_failed",
                      "deployed", "deploy_failed", "halted"):
            work.outcomes.append((kind, row["rationale"] or ""))

    return work


def acceptance_criteria(body: str) -> list[str]:
    """Lift criteria the ticket author already wrote, verbatim.

    Not invented. Metis has no way to know what "done" means for a piece of
    work, and inventing criteria would put words in the author's mouth on their
    own ticket.
    """
    match = _CRITERIA.search(body or "")
    if not match:
        return []

    lines = []
    for line in body[match.end():].splitlines():
        stripped = line.strip()
        if not stripped:
            if lines:
                break
            continue
        if re.match(r"^#{1,4}\s", stripped):
            break
        lines.append(re.sub(r"^[-*•]\s*|^\d+[.)]\s*", "", stripped))
    return lines[:10]


def render(work: Work, *, done: bool = True) -> str:
    """The comment that goes on the ticket."""
    out: list[str] = []
    verdict = "complete" if done else "needs attention"
    out.append(f"**Metis: {work.issue_key} {verdict}**")
    if work.title:
        out.append(f"\n_{work.title}_")

    if work.design:
        who = f" (approved by {work.design_approved_by})" if work.design_approved_by else ""
        out.append(f"\n**Approach{who}**\n{work.design}")

    criteria = acceptance_criteria(work.body)
    if criteria:
        out.append("\n**Acceptance criteria, as written on this ticket**")
        out += [f"- {c}" for c in criteria]

    if work.files:
        out.append(f"\n**Changed** — {len(work.files)} file(s), "
                   f"+{work.insertions} −{work.deletions}")
        for path, (added, removed) in sorted(work.files.items()):
            out.append(f"- `{path}` (+{added} −{removed})")

    if work.outcomes:
        out.append("\n**Verified**")
        seen: set[str] = set()
        for kind, why in work.outcomes:
            if kind in seen:
                continue
            seen.add(kind)
            out.append(f"- {kind.replace('_', ' ')}" + (f" — {why}" if why else ""))

    if work.commands:
        failed = work.failed_commands
        out.append(f"\n**Commands run** — {len(work.commands)}, "
                   + (f"{len(failed)} failed" if failed else "all succeeded"))
        for argv, code in failed[:3]:
            out.append(f"- `{argv[:120]}` exited {code}")

    out.append("\n---\n_Written from the Metis ledger. File changes and commands "
               "are recorded by hooks as they happen, not reported by the agent._")
    return "\n".join(out)


def for_requirement(store: Store, run_id: str, requirement_id: int,
                    done: bool = True) -> str | None:
    work = collect(store, run_id, requirement_id)
    return render(work, done=done) if work else None
