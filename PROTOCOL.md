# Protocol

The contract between agents and the substrate. Everything here is deterministic:
given the same metis state, every command returns the same answer.

Storage is one SQLite file per workspace, WAL mode. Runs are rows, not files.

---

## 1. Schema

```sql
CREATE TABLE runs (
    id            TEXT PRIMARY KEY,      -- e.g. 20260802-160701
    workspace     TEXT NOT NULL,
    environment   TEXT NOT NULL,
    requirement   TEXT NOT NULL,
    max_iterations INTEGER NOT NULL DEFAULT 4,
    status        TEXT NOT NULL DEFAULT 'RUNNING',  -- RUNNING | DONE | HALTED
    created_at    TEXT NOT NULL
);

-- Append-only. Never updated, never deleted. This is the truth.
CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(id),
    ts          TEXT NOT NULL,
    type        TEXT NOT NULL,
    agent       TEXT,                    -- who posted it
    target      TEXT,                    -- which project it concerns
    environment TEXT,
    iteration   INTEGER NOT NULL DEFAULT 1,
    payload     TEXT,                    -- JSON

    -- Audit. See AUDIT.md.
    tier        TEXT NOT NULL DEFAULT 'testimony',  -- 'ground_truth' | 'testimony'
    caused_by   INTEGER REFERENCES events(id),      -- the event that triggered this
    session_id  TEXT,                    -- links to the Claude session transcript
    rationale   TEXT                     -- one line: why the agent did this
);
CREATE INDEX idx_events_run_id_id  ON events(run_id, id);
CREATE INDEX idx_events_type       ON events(run_id, type);
CREATE INDEX idx_events_caused_by  ON events(caused_by);

-- Stored diffs and captured output, referenced by events.
CREATE TABLE artifacts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id),
    kind     TEXT NOT NULL,              -- 'diff' | 'log' | 'report'
    sha256   TEXT NOT NULL,
    body     BLOB NOT NULL
);

-- One row per held slot. capacity N keys hold up to N rows.
CREATE TABLE claims (
    key         TEXT NOT NULL,
    slot        INTEGER NOT NULL,
    run_id      TEXT NOT NULL,
    owner       TEXT NOT NULL,           -- agent name
    pid         INTEGER,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    PRIMARY KEY (key, slot)
);

-- How far each agent has consumed the event stream.
CREATE TABLE cursors (
    agent         TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent, run_id)
);

CREATE TABLE messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    ts         TEXT NOT NULL,
    from_agent TEXT NOT NULL,
    to_agent   TEXT NOT NULL,
    subject    TEXT,
    body       TEXT,
    read_at    TEXT
);
```

`events` is append-only because a whole-row rewrite is how concurrent writers
silently destroy each other's work. Current state is always a projection —
compute the balance from the ledger, never store the balance as truth.

---

## 2. Event types

Posted by agents; the substrate posts only the last three.

| Type | Posted by | Payload |
|---|---|---|
| `requirement` | human | the goal |
| `code_ready` | SWE | `{sha, files[]}` |
| `review_findings` | SWE (review mode) | `{findings[], blocking}` |
| `build_passed` | DevOps | `{sha, duration_ms}` |
| `build_failed` | DevOps | `{sha, summary, detail}` ← fault slice, not the log |
| `deploy_requested` | SWE or DevOps | `{sha, environment}` |
| `deployed` | DevOps | `{sha, environment, previous_ref}` |
| `deploy_failed` | DevOps | `{sha, summary, detail}` |
| `test_passed` | Tester | `{suite, environment, count}` |
| `test_failed` | Tester | `{suite, environment, summary, detail, owning_target}` |
| `approval_requested` | DevOps | `{action, environment}` |
| `approved` / `rejected` | human | `{action}` |
| `stalled` | **substrate** | `{waiting_agents[]}` |
| `halted` | **substrate** | `{reason: iteration_cap}` |

### Ground-truth events

Written by hooks rather than by agents, so they cannot be forgotten or
misreported. `tier = 'ground_truth'`.

| Type | Written by | Payload |
|---|---|---|
| `command_run` | `PostToolUse` hook on Bash | `{argv, cwd, exit, duration_ms}` |
| `file_changed` | `PostToolUse` hook on Edit/Write | `{path, insertions, deletions, diff_sha}` |
| `lease_acquired` | `metis claim` | `{key, slot, ttl}` |
| `lease_released` | `metis release` | `{key, slot}` |
| `lease_expired` | substrate | `{key, owner}` |

An agent's stated `rationale` is testimony; these are what actually happened.
When they disagree, believe these.

**`detail` is always a fault slice, never a raw log.** Producers pipe through
`metis extract` first. A Maven run emits thousands of lines; forwarding them buries
the one line that matters and fills the consumer's context with download
progress.

`test_failed.owning_target` is required. An integration suite tests services from
outside, so a failure in `test_payment_stripe.py` belongs to `payment-service`,
not to the suite. Discovery derives this map.

---

## 3. Lock keys

| Key | Capacity | Derived from | Held by |
|---|---|---|---|
| `worktree:<repo>@<ref>` | 1 | `git.repo_root` + branch | SWE (edits), DevOps (builds) |
| `branch:<repo>@<ref>` | 1 | same | DevOps (pushes) |
| `cluster:<name>` | N | `deployment.identifiers.cluster` | DevOps |
| `schema:<db>.<schema>` | 1 | datasource config + Liquibase properties | DevOps (migrations) |
| `env:<name>` | 1 | the run's environment | Tester |

Keys are **derived by discovery, not hand-maintained**. The system infers its own
contention graph from facts it already collected.

`env:` capacity 1 is why a Tester is not "one agent". Add a staging environment
and `env:dev` and `env:staging` are different keys — two concurrent test runs,
no code change.

### Change sets

One feature routinely spans repositories — a contract change touching both the
producer and its consumers, a shared type, a renamed field. **Those repositories
must be changed in one context: same intent, same identity, built together,
pushed together or not at all, rolled back together.**

Without something holding them together, each repository is an independent
change that can land on its own. A change set is that container.

```bash
metis changeset new --targets service-a,service-b --reason "rename the user id field"
```

Two things follow. A **commit trailer** (`Metis-Change-Id: <run>/cs-<n>`) gives
the feature one identity across every repository it touches, so months later the
pieces are findable as one change. And a **push gate**:

> No repository in a set may be pushed until every repository in that set has
> built.

Enforced by the pre-tool hook rather than requested in a prompt, because it is
exactly the rule an agent under pressure to ship half a change would reason its
way around. Without it, repo A lands, repo B's build fails, and production holds
two services disagreeing about a contract with nothing recording that they were
meant to go together.

The gate **fails closed**. Events count toward a set only when tagged with it
(`metis post --change-set`, or `METIS_CHANGE_SET`). An untagged build leaves the
set blocked, which is recoverable — tag it and rebuild. Letting untagged builds
satisfy a set would be the exact false positive the gate exists to prevent.

`metis changeset rollback-plan <id>` emits a per-repository reset covering the
whole set. Emitted, never executed.

### Rules

- An agent may act only while holding **every** key its action declares.
- Multiple keys are acquired in **sorted order**. Two multi-repo changes taking
  locks in different orders will deadlock.
- Every claim has a TTL and must be renewed while work continues.
- Expiry is the crash backstop; explicit `release` is the normal path.

---

## 4. CLI

```bash
metis init --workspace ~/Desktop/LandGo --environment dev --requirement "..."
```

```bash
metis post --type code_ready --target user-service --payload '{"sha":"a9faded"}'
```

```bash
metis await --for build_failed,test_failed --timeout 600
```

```bash
metis tail --agent devops
```

```bash
metis claim worktree:user-service@develop --ttl 900
```

```bash
metis renew worktree:user-service@develop --ttl 900
```

```bash
metis release worktree:user-service@develop
```

```bash
metis state --target user-service
```

```bash
metis context --agent devops --event 42
```

```bash
metis send --to swe --subject "build broke" --body "@42"
```

```bash
metis inbox --agent swe
```

```bash
metis extract --kind maven --file build.log
```

```bash
metis discover ~/Desktop/LandGo
```

Audit commands are documented in [AUDIT.md](AUDIT.md): `log`, `trace`, `why`,
`timeline`, `diff`, `replay`, `watch`, `doctor`, `report`.

### Semantics

- **`await`** blocks until a matching event or timeout. Exit 0 = matched (event
  on stdout), exit 1 = timed out. Advances the cursor only on match.
- **`tail`** long-runs, one line per matching event, **flushed per line**.
  Buffered output means Monitor delivers notifications in clumps or never.
- **`claim`** exits 0 on success, 1 if no slot is free, 2 if the run is halted.
  It never blocks — a caller that wants to wait retries.
- **`post`** returns the new event id, so a message can reference `@42` rather
  than duplicating a payload.

---

## 5. `metis context` — the spawned-mode contract

The single most important command, because a cold agent has nothing else.

```
metis context --agent devops --event 42
```

Must return:

1. The agent's role text
2. The triggering event, in full
3. The target's current phase and iteration
4. **Prior attempts** — what has already been tried, and what happened
5. The most recent fault slice for that target
6. Which lock keys the intended action requires
7. Remaining iterations before the cap

**Acceptance test:** a human handed only this output could do the work correctly.
If they could not, a cold agent cannot either.

Item 4 is the one that is easy to skip and expensive to omit — without it, a
fresh agent will confidently re-apply a fix that already failed twice.

---

## 6. Invariants

Enforced in code. None of these are prompt instructions.

1. `events` is append-only. No updates, no deletes.
2. An actuating command runs only while holding every declared lease.
3. Multiple leases are acquired in sorted key order.
4. Past `max_iterations`, `claim` refuses and the run halts.
5. `detail` fields carry fault slices, never raw logs.
6. An agent may not modify the test file named in the fault slice it is repairing.
7. Write scoping is enforced by `PreToolUse` hooks, per role.
8. Secret-shaped config values are resolved first, then redacted — never the
   reverse, which makes an unset secret look configured.
9. Deploys carry an idempotency key derived from the artifact hash.
10. Rollback plans are emitted, never executed automatically.
11. Every event carries `caused_by`, except the run's first. Causality is
    recorded, never inferred from timestamps.
12. Commands, file changes, and lease transitions are written by hooks, not by
    agents, and are marked `tier = 'ground_truth'`.

---

## 7. Worked example

```
SWE                                       DevOps
─────────────────────────────────────     ─────────────────────────────────────
                                          [Monitor: metis tail --agent devops]

metis claim worktree:user-service@develop
edits ObjectStoreProbe.java
metis release worktree:user-service@develop
metis post --type code_ready \
  --target user-service                ──▶ notification: code_ready user-service

                                          metis claim worktree:user-service@develop
                                          ./mvnw -B verify   → exit 1
                                          metis extract --kind maven --file b.log
                                          metis post --type build_failed \
                                            --target user-service \
                                            --payload '{"summary":"...","detail":"..."}'
                                          metis release worktree:user-service@develop
notification: build_failed  ◀──────────

metis state --target user-service
  → iteration 2 of 4, 1 prior attempt
repairs, posts code_ready
```

Nobody was assigned anything. The bus recorded events, arbitrated one lease, and
woke the right session.

Note the release before `code_ready`: SWE hands the worktree back so DevOps can
take it. Holding a lease across a handoff is the most common way to deadlock two
otherwise correct agents.
