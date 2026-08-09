"""Deciding what an agent may start on its own, and what needs a person first.

Most work is safe to hand over. A bug fix, a new endpoint, a failing test --
the blast radius is a diff, the tests judge it, and a mistake costs a rerun.

Some work is not. Adding a service, taking a new dependency, migrating a schema,
changing infrastructure: these commit money, lock in structure, and are painful
to walk back. The tests still pass, so nothing downstream catches them. That is
the point of a gate here rather than a review later -- by review time the
decision has already been made.

Two rules keep this honest:

* **The tracker is the authority.** A label or issue type says "this needs me",
  and that is respected exactly.
* **Heuristics may only escalate.** Text signals can raise a task to gated;
  nothing can lower one. A classifier that quietly decides "just a bug fix" and
  is wrong is the single failure a safety gate cannot have, so the code makes
  that direction impossible rather than unlikely.

Ambiguity therefore costs a person thirty seconds. The opposite mistake costs a
production migration nobody agreed to.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .bus import events as ev
from .bus.store import Store

AUTONOMOUS = "autonomous"
GATED = "gated"

# A tracker label or issue type carrying any of these means the human already
# said this needs them. Matched case-insensitively against labels.
GATING_LABELS = frozenset({
    "architecture", "architectural", "design", "adr", "rfc",
    "infra", "infrastructure", "needs-review", "needs-design",
    "breaking-change", "migration", "spike",
})

# Signals that raise a task to gated on their own. Each is something whose cost
# is not visible in a diff and not caught by a test suite.
ESCALATIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("adds or changes infrastructure",
     re.compile(r"\b(terraform|cloudformation|pulumi|helm chart|kubernetes manifest|"
                r"\.tf\b|iac|provision(?:ing|ed)?)\b", re.I)),
    ("creates or splits a service",
     re.compile(r"\b(new (?:micro)?service|extract (?:a )?service|split (?:out|the)|"
                r"stand up a|spin up a|separate (?:micro)?service)\b", re.I)),
    # Schema work splits in two, and only one half is dangerous.
    #
    # Additive changes -- a new column, a new table, a new index -- are ordinary
    # feature work. Gating them is noise, and noise is how a review queue starts
    # getting waved through.
    #
    # Destructive ones lose data or break readers, and no test suite catches
    # that: the build is green precisely because the column is gone. These match
    # loosely on purpose, because tickets say "drop the audit_log table", not
    # "DROP TABLE".
    ("makes a destructive schema change",
     re.compile(r"\b(?:drop|truncate|delete|remove|purge)\b[^.\n]{0,40}"
                r"\b(?:table|column|field|collection|index|constraint)\b"
                r"|\b(?:table|column|field|collection)\b[^.\n]{0,25}"
                r"\b(?:is |be )?(?:dropped|removed|deleted|truncated)\b"
                r"|\balter\b[^.\n]{0,40}\b(?:table|column|type)\b"
                r"|\brename\b[^.\n]{0,40}\b(?:table|column|field)\b"
                r"|\bdrop\s+(?:table|column)\b", re.I)),

    # Separate from the above: a migration is a production-affecting operation
    # whatever it contains -- it locks, it runs once, and it is awkward to undo.
    ("runs a schema migration or backfill",
     re.compile(r"\b(migrat(?:e|ion|ions)|backfill|reindex|"
                r"schema change|data fix(?:up)?)\b", re.I)),
    ("takes a new dependency",
     re.compile(r"\b(add (?:a )?(?:new )?(?:dependency|library|package|sdk)|"
                r"switch to|replace .{0,20}\bwith\b|adopt)\b", re.I)),
    ("has a cost or capacity implication",
     re.compile(r"\b(cost|billing|pricing|spend|budget|instance type|scale up|"
                r"autoscal(?:e|ing)|throughput|quota|tier)\b", re.I)),
    ("changes a public contract",
     re.compile(r"\b(breaking change|api version|deprecat(?:e|ion)|"
                r"rename .{0,20}\b(?:field|endpoint|route)|contract change)\b", re.I)),
    ("rewrites or removes a component",
     re.compile(r"\b(rewrite|re-architect|refactor everything|remove the|"
                r"decommission|sunset)\b", re.I)),
)


@dataclass
class Verdict:
    risk: str
    reasons: list[str] = field(default_factory=list)
    from_tracker: bool = False

    @property
    def gated(self) -> bool:
        return self.risk == GATED

    def describe(self) -> str:
        if not self.gated:
            return "autonomous -- no gating signal"
        source = "labelled on the tracker" if self.from_tracker else "flagged from the text"
        return f"gated ({source}): " + "; ".join(self.reasons)


def classify(title: str, body: str, labels: list[str] | None = None,
             issue_type: str | None = None) -> Verdict:
    """Decide whether this needs a person before an agent starts.

    Escalation only. There is deliberately no path that returns AUTONOMOUS
    after something has gated it.
    """
    reasons: list[str] = []

    tagged = {str(l).strip().lower() for l in (labels or [])}
    if issue_type:
        tagged.add(str(issue_type).strip().lower())

    hit = sorted(tagged & GATING_LABELS)
    if hit:
        return Verdict(GATED, [f"tagged '{h}'" for h in hit], from_tracker=True)

    text = f"{title}\n{body or ''}"
    for reason, pattern in ESCALATIONS:
        if pattern.search(text):
            reasons.append(reason)

    return Verdict(GATED, reasons) if reasons else Verdict(AUTONOMOUS)


# --------------------------------------------------------------- the gate


def _payload(row) -> dict:
    try:
        body = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return body if isinstance(body, dict) else {}


def _decided(rows) -> tuple[set[int], set[int]]:
    """Requirement ids a human has approved, and ones they have rejected."""
    approved: set[int] = set()
    rejected: set[int] = set()

    for row in rows:
        if row["type"] not in ("approved", "rejected"):
            continue
        body = _payload(row)
        which = body.get("requirement") or row["caused_by"]
        if which is None:
            continue
        (approved if row["type"] == "approved" else rejected).add(int(which))

    return approved, rejected


def pending(store: Store, run_id: str, include_rejected: bool = False) -> list[dict]:
    """Gated requirements a human has not decided on yet.

    Derived from the ledger, and approval cannot be forged -- the bus refuses
    `approved` from anything but a human.

    A rejected task is *decided*, so it leaves this queue while staying blocked.
    Otherwise saying no would mean being asked again forever.
    """
    rows = ev.read_since(store, run_id, 0, limit=10_000)
    approved, rejected = _decided(rows)

    waiting = []
    for row in rows:
        if row["type"] != "requirement":
            continue
        body = _payload(row)
        if body.get("gate") != GATED or int(row["id"]) in approved:
            continue
        if int(row["id"]) in rejected and not include_rejected:
            continue
        waiting.append({
            "event_id": int(row["id"]),
            "target": row["target"],
            "issue_key": body.get("issue_key", "--"),
            "title": body.get("title") or (row["rationale"] or ""),
            "reasons": body.get("gate_reasons") or [],
            "body": body.get("body", ""),
        })
    return waiting


def blocked_targets(store: Store, run_id: str) -> dict[str, dict]:
    """target -> the gated requirement blocking it.

    Includes rejected work: saying no blocks it harder than not deciding.
    """
    return {p["target"]: p
            for p in pending(store, run_id, include_rejected=True) if p["target"]}


def check_key(store: Store, run_id: str, key: str) -> tuple[bool, str]:
    """May this lock key be claimed, or is its target awaiting review?

    The lease is the choke point: an agent may only act while holding every key
    its action declares, so refusing here refuses the work itself. Enforcing at
    the lock rather than in a prompt means the gate does not depend on an agent
    choosing to respect it.
    """
    blocked = blocked_targets(store, run_id)
    if not blocked:
        return True, ""

    # worktree:api@main -> api ; cluster:demo -> demo
    name = key.split(":", 1)[1] if ":" in key else key
    name = name.split("@", 1)[0]

    for target, waiting in blocked.items():
        if name == target:
            reasons = "; ".join(waiting["reasons"]) or "flagged for review"
            return False, (
                f"'{target}' is waiting on human review: {waiting['issue_key']} "
                f"({reasons}). Run `metis groom` to review it. "
                f"Architectural work is not started unattended."
            )
    return True, ""
