# Audit and debugging

Three agents acting unattended is only acceptable if you can reconstruct, after
the fact, exactly what happened and why. This is the surface that makes that
possible.

**The rule: if it is not in the ledger, it did not happen.** Anything an agent
does that matters produces an event. Nothing of consequence lives only inside a
session's context, because context compacts and cannot be queried.

---

## 1. Two tiers of evidence

Not everything in the ledger deserves the same trust.

| Tier | Written by | Examples | Trust |
|---|---|---|---|
| **Ground truth** | hooks, outside the agent | commands run, files changed, leases taken, exit codes | an agent cannot forget or misreport it |
| **Testimony** | the agent itself | event type, rationale, failure classification | self-reported; may not match what it did |

This distinction is the whole point. An agent that claims it "fixed the null
check" while the diff shows it deleted an assertion is caught by comparing the
two tiers. Collapse them into one stream and you are trusting an agent's account
of itself.

Hooks are what make the first tier possible: a `PostToolUse` hook on Bash and
Edit records what actually ran, whether or not the agent remembers to say so.
Agents forget. Hooks do not.

---

## 2. What gets recorded

| Event | Tier | Carries |
|---|---|---|
| `command_run` | ground truth | argv, cwd, exit code, duration, agent, session |
| `file_changed` | ground truth | path, diff hash, insertions/deletions, diff stored as artifact |
| `lease_acquired` / `lease_released` / `lease_expired` | ground truth | key, owner, ttl |
| `code_ready`, `build_failed`, `test_failed`, … | testimony | payload, plus `rationale` |

Every event additionally carries:

| Field | Why it matters |
|---|---|
| `caused_by` | the event id that triggered this action — turns a flat log into a causal graph |
| `session_id` | links to the Claude session transcript, where the reasoning lives |
| `rationale` | one line: why the agent did this |
| `iteration` | which pass of the loop |

`caused_by` is the field that makes debugging tractable. Without it you have a
timestamp-ordered list and have to infer causality by eye. With it, "show me
everything that followed from event 42" is a query.

The claims *table* holds current state only. Lease events hold the history —
without them, an expired lease leaves no trace and "why did this stall at 16:04"
is unanswerable.

---

## 3. Reading what happened

```bash
metis log --run 20260802-160701
```

Interleaved timeline, one line per event, agent-coloured.

```bash
metis log --target user-service --since 16:00
```

```bash
metis trace 42
```

Causal tree **forward** from an event — everything that followed from it.

```bash
metis why 87
```

The chain **backward** — what caused this, and what caused that. The fastest way
to answer "how on earth did we end up here".

```bash
metis timeline
```

Swimlane per agent, so overlapping work and lease contention are visible.

```bash
metis diff 55
```

The actual change made by a `file_changed` event, from stored artifacts.

```bash
metis replay --at 2026-08-02T16:04:00Z
```

State as of that instant, rebuilt by replaying events up to it. This is what
answers *"what did the SWE agent know when it decided that?"* — and it only
works because events are append-only.

---

## 4. Watching it live

```bash
metis watch
```

A refreshing terminal view: each agent's state, leases held, last event,
iteration count, remaining budget. This is the "what is happening right now"
answer, and the thing you leave open in a fourth terminal.

---

## 5. Debugging a stuck system

By far the most common failure is not a crash — it is silence. Nothing is
happening and it is not obvious why.

```bash
metis doctor
```

Checks, in the order they usually bite:

| Check | Symptom it catches |
|---|---|
| **Orphan events** | something was posted that no agent has in its `wake_on` — the message went nowhere |
| **Cursor lag** | an agent's cursor is far behind — its Monitor died and it stopped listening |
| **Held leases with no activity** | an agent holds a key but has posted nothing recently — crashed mid-work, or deadlocked |
| **Lease handoff deadlock** | agent A waits on a key held by agent B, which is waiting on A |
| **All idle** | every agent waiting, no pending events — livelock |
| **Budget exhausted** | at the iteration cap, claims being refused |

Orphan-event detection is the one worth building first. A typo in a `wake_on`
list produces a system that looks perfectly healthy and does nothing at all,
forever.

---

## 6. Reasoning lives in the transcripts

The bus records **actions**. It does not record *why* an agent thought something
— that reasoning is in the Claude session transcript.

`session_id` on every event is the bridge. From "DevOps did something strange at
16:04" you can jump straight to the transcript of the session that did it.

Be clear about the limit: `rationale` is the agent's own account and may be
incomplete or wrong. The transcript shows the reasoning; the hook-captured
commands and diffs show what actually happened. **When they disagree, believe the
hooks.**

---

## 7. After a run

```bash
metis report 20260802-160701
```

A single readable summary: timeline, every file changed with its diff, every
command run, what failed and how it was classified, iterations used, leases
contended, and the final state of each target.

This is the artifact worth keeping — the thing you read on Monday about what ran
over the weekend, and the thing you attach when something reached an environment
that should not have.

---

## 8. What you still cannot see

Honest limits, so nobody assumes more coverage than exists:

- **An agent's discarded reasoning.** Options it considered and rejected are in
  the transcript at best, and often nowhere.
- **Why a model chose one fix over another.** You see the diff and the stated
  rationale, not the deliberation.
- **Anything done outside the harness.** A hook records tool calls; it cannot
  record something a human did in a fourth terminal.
- **Environment drift.** If a test failed because someone changed a dev database
  by hand, the ledger shows the failure and nothing about the cause.

The first two are inherent to using models. The last two argue for keeping the
run's environment as reproducible as possible, so that the ledger is a complete
explanation rather than a partial one.
