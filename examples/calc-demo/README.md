# calc-demo — watch three agents work

A tiny Python package with a working test suite, wired up so you can watch
SWE, DevOps, and Tester coordinate on something real. No cloud, no network.

Requires `metis` on PATH — the hooks invoke `metis hook pre`, so without it the
safety rails silently never fire:

```bash
pipx install git+https://github.com/rayhimanshu/metis.git
```

## Setup

Copy it out of the Metis repository first. Agents key their worktree lease on
the enclosing git root and use its HEAD as the rollback anchor, so running it in
place would have them leasing and resetting Metis itself:

```bash
cp -r examples/calc-demo ~/calc-demo && cd ~/calc-demo
```

```bash
./setup.sh
```

That builds the venv, installs the hooks, and starts a run with this
requirement:

> Add a `divide(a, b)` function to the calc package. It must raise `ValueError`
> with a clear message when `b` is zero. Cover it with tests.

Ask what is happening at any point:

```bash
metis
```

That reads the local ledger and prints the run, what is in flight, what
finished, and who holds a lease. No network, so it answers instantly.

**There is no bus process to start.** The bus is a SQLite file at
`.metis/bus.db` — no daemon, nothing to launch. What earns a terminal is the
live view.

> This demo hands the agents a requirement directly, because it has no issue
> tracker attached. On a real project you would connect Jira or Trello and use
> **`metis work`** to see what is ready and pick something up — or
> `metis work --auto` to keep the queue fed without being asked. See the
> [main README](../../README.md#picking-up-work).

Open four terminals, all in this directory.

---

## Terminal 1 — the live view

```bash
metis watch
```

Refreshes every 2s: each agent's unread count, which leases are held, and the
last dozen events. Leave it running; this is the one to watch.

---

## Terminal 2 — SWE

```bash
METIS_ROLE=swe claude
```

Paste as the first message:

> You are the SWE agent in a Metis run. Run `metis context --agent swe` to get
> your role and the current state, then follow it. Arm a Monitor on
> `metis tail --agent swe` so you are woken by events, and keep working until
> the requirement is done or the iteration cap is reached.

---

## Terminal 3 — DevOps

```bash
METIS_ROLE=devops claude
```

> You are the DevOps agent in a Metis run. Run `metis context --agent devops`
> to get your role and the current state, then follow it. Arm a Monitor on
> `metis tail --agent devops` so you are woken by events. The build command is
> `venv/bin/python -m pytest -q`.

---

## Terminal 4 — Tester

```bash
METIS_ROLE=tester claude
```

> You are the Tester agent in a Metis run. Run `metis context --agent tester`
> to get your role and the current state, then follow it. Arm a Monitor on
> `metis tail --agent tester` so you are woken by events. This is a local
> environment, so "deployed" means the build passed on this machine.

---

## What should happen

1. SWE wakes on the `requirement`, claims `worktree:metis-demo@main`, writes
   `divide`, releases the lease, posts `code_ready`
2. DevOps wakes, claims the worktree, runs pytest, posts `build_passed` or
   `build_failed` **with a fault slice, not the whole log**
3. On failure SWE wakes with the slice and repairs; on success Tester runs the
   suite and posts `test_passed`
4. Terminal 1 shows every step as it lands, and `metis` in any other terminal
   gives you the same picture as a snapshot

Notice that the handoff works because SWE *releases* the worktree before
posting. Holding a lease across a handoff is the classic way two correct agents
deadlock.

---

## Watch the safety rails fire

The interesting part is what the agents **cannot** do. Try these yourself:

```bash
METIS_ROLE=devops metis post --type approved --i-am-human
```

Refused — approval an agent can grant itself is not approval.

Ask the DevOps session to "just fix the failing test yourself". It will be
blocked by the hook: DevOps cannot edit source, so it cannot be both the cause
of a change and the judge of whether it built.

Ask the SWE session to `git push`. Blocked — SWE has no path to an environment.

If a test failure names a `test_file`, ask SWE to delete that test to make the
build green. Blocked — repairing code and changing a test are separate,
visible acts.

---

## Afterwards

```bash
metis why $(metis log --limit 1 | head -1 | tr -d '#' | cut -d' ' -f1)
```

The backward causal chain: what caused this, and what caused that.

```bash
metis report
```

Every file changed, every command run, all failures and how they were
classified. Commands and diffs in there were written by hooks, not by the
agents — when an agent's account disagrees with them, believe the hooks.

```bash
metis doctor
```

If nothing is happening, this tells you why: an agent not listening, a stale
lease, or an event type nobody wakes on.

---

## Reset and run again

```bash
git checkout . && git clean -fd -e venv && ./setup.sh
```
