<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <img src="assets/logo.svg" alt="Metis" width="96" height="96">
  </picture>
</p>

<h1 align="center">Metis</h1>

<p align="center">A substrate for autonomous engineering agents</p>

<p align="center">

[![CI](https://github.com/rayhimanshu/metis/actions/workflows/ci.yml/badge.svg)](https://github.com/rayhimanshu/metis/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</p>

Three Claude sessions — **SWE**, **DevOps**, **Tester** — working autonomously on
the same goal, in separate terminals, coordinating through a shared bus.

Work arrives from an issue tracker. The agents pick it up, implement it, build,
deploy, verify, and repair their own regressions. Nothing drives them; a small
piece of deterministic infrastructure gives them a way to talk, a shared memory,
and enough safety rails that two of them cannot break the same thing at once.

Nothing in the system is told about a language, a cloud, or a service.

- **[SETUP.md](SETUP.md)** — install, pick your repos, connect Jira or Trello, troubleshoot
- **[DESIGN.md](DESIGN.md)** — architecture, and why a substrate rather than an orchestrator
- **[PROTOCOL.md](PROTOCOL.md)** — bus schema, event types, lock keys
- **[AUDIT.md](AUDIT.md)** — seeing what happened, and diagnosing what is not
- **[metis/roles/](metis/roles/)** — the operational prompt for each agent

---

## Install

Requires Python 3.11+. [pipx](https://pipx.pypa.io) is recommended so Metis gets
its own environment and still lands on your PATH.

```bash
pipx install git+https://github.com/rayhimanshu/metis.git
```

Or with uv:

```bash
uv tool install git+https://github.com/rayhimanshu/metis.git
```

Or plain pip, into a virtualenv you manage:

```bash
pip install git+https://github.com/rayhimanshu/metis.git
```

Check it:

```bash
metis --version
```

### From a clone, for development

```bash
git clone https://github.com/rayhimanshu/metis.git && cd metis
```

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/pytest -q
```

## Quick start

Run the guided setup in the repo you want agents to work on:

```bash
metis setup
```

It asks for a workspace and an environment name — both with defaults — then what
you want to connect. **Nothing else is required unless you ask for it.** Choose
no work source and you are never asked for a token; choose Jira and its URL,
email, and API token become required, and are verified against the live service
before the wizard finishes.

Pressing enter through every question produces a working configuration. It is
re-runnable, offers your previous answers as defaults, and backs up an existing
`metis.yaml` before rewriting it.

| What | Required | Default |
|---|---|---|
| Workspace | yes | current directory |
| Environment name | yes | `dev` |
| Iteration cap | yes | `4` |
| Agent modes | yes | swe attached, devops + tester spawned |
| Work source | no | none — start runs by hand |
| Jira URL, email, API token | only if Jira | — |
| Trello board, key, token | only if Trello | — |
| Git token | no | uses `gh` if authenticated; without it agents build and test but cannot push |

For just a config file with no questions, `metis init` still works.

See what Metis makes of your repos — this writes nothing into your project:

```bash
metis discover
```

Check the setup:

```bash
metis doctor
```

Wire the safety hooks into that project's Claude settings. This merges rather
than overwrites, and it refuses if `metis` is not on your PATH, because hooks
pointing at a missing binary would silently never fire:

```bash
metis install-hooks
```

Start a run, then open a session as an agent:

```bash
metis init-run --requirement "Add a health-check endpoint"
```

```bash
METIS_ROLE=swe claude
```

Without `METIS_ROLE` the hooks stay inert, so your ordinary sessions are
unaffected.

### Role prompts

The three role prompts ship with the package, so a fresh install works with no
files to create. To customise one, copy them into your project — project copies
win:

```bash
metis roles --eject
```

### Which repositories agents work on

The workspace directory is the scope. Point it at one repository, or at a parent
holding several — discovery treats each git root as its own target with its own
`worktree:` lease, so agents can work on different repositories at once and
never on the same one. Full detail, including cross-repo changes and the absence
of an include/exclude filter, is in [SETUP.md](SETUP.md#choosing-what-agents-work-on).

## The idea in one paragraph

A Claude session is already an agentic loop — it reasons, acts, observes, and
retries. That does not need building. What is missing is communication, shared
truth, and safety. So the shared component decides nothing: it records events,
delivers them, grants leases, and enforces a stop condition. Everything above it
is non-deterministic and smart; the substrate is deterministic and dull. That
boundary is the design.

## Discovery

```bash
metis discover /path/to/repo
```

Accepts a git URL just as well. Produces every target, derived build commands,
capabilities with the evidence that proved them, deploy identifiers lifted from
CI, the test→target map, and **lock keys derived from all of it**.

### Evidence tiers

| Signal | Meaning |
|---|---|
| `declared` | a dependency coordinate in a build manifest |
| `imported` | main source actually references the client |
| `configured` | a config key names a concrete resource |
| `provisioned` | IaC creates it and grants permissions |

A capability is acted on only when **declared AND imported**. Everything else is
reported with the reason it fell short:

```
skipped (reported, not acted on):
  - maven-service/firestore [medium] declared and configured, but no main-source
    usage found -- the dependency is present without evidence the code exercises it
```

That rule is what makes unattended operation defensible — the system declines
what it cannot justify instead of guessing. IaC never creates a finding on its
own; it enriches one, and it *bounds* behaviour: a role granted only
`s3:GetObject` yields a read-only probe rather than a write that was always
going to fail.

## The bus

```bash
metis init-run --requirement "Add an S3 health probe"
```

```bash
METIS_ROLE=swe metis claim worktree:api@main --ttl 900
```

```bash
METIS_ROLE=swe metis post --type code_ready --target api --caused-by 1 --rationale "..."
```

```bash
METIS_ROLE=devops metis tail
```

Exit codes are part of the contract, because agents branch on them: `0` granted,
`1` refused, `2` the run is over.

## Running the agents

Each agent is **attached** (a session you open, woken by `metis tail` under a
Monitor) or **spawned** (a cold `claude -p` per event). Configured per agent;
recommended default is SWE attached, DevOps and Tester spawned.

```bash
METIS_ROLE=swe claude
```

```bash
metis dispatch --dry-run
```

Run `metis install-hooks` in the project first. One hook config serves all three
roles — the hooks read `$METIS_ROLE` at runtime, so there is no way to launch
with the wrong hook set installed.

## Safety, enforced rather than requested

| Rule | Where |
|---|---|
| DevOps cannot edit source | `PreToolUse` hook |
| SWE and Tester cannot deploy or push | `PreToolUse` hook |
| Nobody may edit the test that caught them | hook, derived from the ledger |
| Only a human can post `approved` | the bus refuses otherwise |
| An actuating agent must hold every declared lease | lease broker |
| No repo in a change set is pushed until all of them build | `PreToolUse` hook |
| Past the iteration cap, claims are refused | lease broker |
| Deep probes never attach to a load-balancer-polled path | probe policy |
| Secrets are resolved then redacted, never the reverse | discovery |

Agents never see raw tokens. The substrate holds credentials and acts on their
behalf, so an agent cannot leak what it was never handed.

## Intake

```bash
metis setup jira
```

```bash
metis intake --dry-run
```

Issue text is **untrusted input**. Bodies are fenced between
`<<<UNTRUSTED-ISSUE-TEXT` markers, instruction-shaped phrasing is *flagged rather
than stripped*, and a ticket can never grant permission or name a target outside
the workspace.

## Audit

```bash
metis why 42
```

```bash
metis trace 1
```

```bash
metis watch
```

```bash
metis report --out run.md
```

Two tiers of evidence, kept apart: **ground truth** (commands, diffs, leases —
written by hooks) and **testimony** (event types, rationale — written by agents).
When they disagree, believe the hooks.

## Layout

```
metis/
  bus/            schema, store, events, leases, context, audit
  discovery/      scan, detectors, capabilities, iac, deployment, testing, keys
  intake/         jira, trello, sync, untrusted-text handling
  policy/         probe families and permission bounds
  hooks/          PreToolUse (enforcement), PostToolUse (ground truth)
  roles/          packaged agent prompts
  enforcement.py  what each role may write and run
  secrets.py      keychain-backed; never logged, never given to agents
  dispatcher.py   spawned mode
fixtures/         synthetic repos — the correctness bar
```

Hooks and role prompts live *inside* the package. A settings file pointing at
`$SOMEWHERE/hooks/pre_tool_use.py` breaks the moment the tool is installed or
moved; `metis hook pre` works wherever `metis` is on PATH.

Stack support is a plugin file under `discovery/detectors/`. Capability
signatures and probe families are data, so adding a backing service is an edit,
not a release.

## Tests

```bash
pytest -q
```

All tests run against synthetic fixtures — no external services, no network,
runs anywhere.
