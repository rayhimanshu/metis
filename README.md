<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <img src="assets/logo.svg" alt="Metis" width="96" height="96">
  </picture>
</p>

<h1 align="center">Metis</h1>

<p align="center">
  <em>Distributed coordination for autonomous AI agents via append-only event ledger and lease-based mutual exclusion.</em>
</p>

<p align="center">

[![CI](https://github.com/rayhimanshu/metis/actions/workflows/ci.yml/badge.svg)](https://github.com/rayhimanshu/metis/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</p>

### Substrate, not orchestrator

Three autonomous Claude agents — **SWE**, **DevOps**, **Tester** — operate independently on the same codebase, coordinated through an append-only SQLite ledger. No central orchestrator. No message broker. No state consensus algorithm.

Each agent:
- Runs as a separate Claude session in its own terminal
- Reasons, acts, observes, and retries independently
- Claims exclusive leases (with TTL, all-or-nothing, sorted acquisition) before mutating shared resources
- Posts typed events to the ledger: `code_ready`, `build_passed`, `deploy_started`, `test_failed`, etc.
- Awaits or tails events from other agents to react

The ledger provides **eventual consistency** within a single run: all agents see the same causal history. It enforces **serializability** for resource access: two agents never hold conflicting leases. It tracks **ground truth** separately from testimony: what the OS witnessed (hooks) vs. what agents claim.

Work arrives from Jira or Trello. The agents ingest it, implement it, build, test, deploy, and self-repair on failure. Nothing orchestrates them. A deterministic substrate gives them shared memory, communication primitives, and safety enforcement that cannot be bypassed by a prompt.

| | |
|---|---|
| **[SETUP.md](SETUP.md)** | install, pick your repos, connect Jira or Trello, go live |
| **[DESIGN.md](DESIGN.md)** | why a substrate, not an orchestrator — the boundary between dumb and smart |
| **[PROTOCOL.md](PROTOCOL.md)** | the ledger: schema, event types, locks, and how changes that span repos stay coherent |
| **[AUDIT.md](AUDIT.md)** | seeing every decision, every pause, every fault — and why each one happened |
| **[metis/roles/](metis/roles/)** | the prompt for each agent — SWE, DevOps, Tester |

---

## Install

Requires **Python 3.11+**.

**Recommended:** [pipx](https://pipx.pypa.io) isolates Metis in its own environment while putting it on your PATH:

```bash
pipx install git+https://github.com/rayhimanshu/metis.git
```

**Or with uv** (faster, newer):

```bash
uv tool install git+https://github.com/rayhimanshu/metis.git
```

**Or plain pip** into a virtualenv you manage:

```bash
pip install git+https://github.com/rayhimanshu/metis.git
```

**Verify:**

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

### 1. Setup — first time only

Go to the project you want agents to work on:

```bash
cd ~/path/to/your-project
metis setup
```

It asks a few questions. Press enter to take defaults, or customize:

| What | Required? | Default |
|---|---|---|
| Workspace directory | yes | current directory |
| Environment name | yes | `dev` |
| Iteration cap per run | yes | `4` |
| How to run agents | yes | SWE attached, DevOps/Tester spawned |
| Work source (Jira/Trello/none) | no | none |
| Jira URL, email, token | only if Jira | — |
| Trello board, key, token | only if Trello | — |
| GitHub token | no | auto-detect via `gh` |

Pressing enter through all questions produces a working config. The wizard is re-runnable and backs up your old config first.

For a silent config file without prompts: `metis init`.

### 2. Discover — see what Metis understands about your code

```bash
metis discover
```

Scans your repo(s) and outputs:
- Every service/package to work on
- Build commands it inferred
- Capabilities it found (databases, caches, etc.) with confidence scores
- Test coverage map
- Lock keys — how agents can operate safely

### 3. Install safety hooks

```bash
metis install-hooks
```

Merges Metis hooks into your project's Claude settings. Refuses if `metis` is not on your PATH (prevents silent failures).

### 4. Start your first run

```bash
metis init-run --requirement "Add a health-check endpoint"
```

Creates a run and waits for agents to pick up work.

### 5. Open agent terminals

Open four terminals in the project directory:

**Terminal 1 — the ledger:**
```bash
metis watch
```

Streams the event log as it grows. Lets you see every decision.

**Terminal 2 — the Coder:**
```bash
METIS_ROLE=swe claude
```

**Terminal 3 — the Builder:**
```bash
METIS_ROLE=devops claude
```

**Terminal 4 — the Tester:**
```bash
METIS_ROLE=tester claude
```

### Notes

- Without `METIS_ROLE`, hooks stay inert — your normal Claude sessions are unaffected
- The agents will wake each other via the ledger; keep `metis watch` running to see the coordination
- Full setup docs: [SETUP.md](SETUP.md)

### Customize agent prompts

The three agent prompts ship inside the package, so a fresh install works with no files to create. To customize one:

```bash
metis roles --eject
```

Copies the prompts to your project. Project copies win over packaged ones.

### Multi-repository workspaces

The workspace is your scope. Point it at one repo, or at a parent holding several:

```bash
metis setup --workspace ~/projects/my-platform
```

Discovery treats each git root as its own target with its own lock. Agents can work on different repositories simultaneously — never on the same one at the same time.

Full details: [SETUP.md](SETUP.md#choosing-what-agents-work-on).

## The philosophy

A Claude session is already an agentic loop — **reason, act, observe, retry**. That works.

What a single agent lacks is the ability to work alongside others on the same code. Add three agents, and you need: communication (so they know what the others did), shared truth (so they agree on what happened), and safety (so their independence doesn't become chaos).

The substrate provides exactly those three things and nothing more. It records events, delivers them, grants leases, and enforces a stop condition. It decides nothing. It judges nothing. It only says: "you may act" or "you may not, because someone else is".

Everything above the substrate — reasoning, strategy, repair — lives in the agents, where it's flexible and intelligent. Everything below — the ledger, the locks, the rules — is deterministic and predictable. That boundary is the design.

## Discovery — learning the shape of the system

```bash
metis discover /path/to/repo
```

Accepts a git URL just as well. Scans the repo and produces:

- Every target (each git root and built artifact)
- Build commands, derived from manifests and CI config
- Capabilities the code actually uses (S3, databases, caches, auth systems, etc)
- Deploy shape — container registries, orchestration, load balancer health paths
- Test-to-target mapping — which tests cover which services
- **Lock keys** — derived from all of it, so agents can't collide

**Nothing is guessed.** A capability only becomes actionable when the evidence justifies it. The bar:

| Evidence | Is it real? |
|---|---|
| Dependency declared in a manifest | Maybe declared but not used — not actionable |
| + source actually imports it | Now it's real. Actionable. |
| + a config key names a concrete resource | Even stronger. |
| + IaC creates it and grants permissions | Definitive. The platform agrees. |

A result like this is typical:

```
✓ redis [high]      declared + imported + configured
  └ used in cache layer, config has REDIS_HOST set

skip postgres [medium]  declared, configured, but no source import
  └ dependency present, config ready, code doesn't exercise it
  
skip dynamodb [low]  imported in test fixtures only
  └ strong signal but test-only, won't load in production
```

That rule — **decline what you cannot justify** — is what makes unattended operation defensible. No guessing. No wishful thinking. IaC never creates a finding on its own; it enriches one. Permissions are bounds: an agent granted `s3:GetObject` gets a read-only probe, not a write that was always going to fail.

## The ledger protocol

The ledger is the substrate. Agents interact with it via CLI:

```bash
# Claim exclusive access to a target
metis claim worktree:api@main --ttl 900

# Record an event
metis post --type code_ready --target api --rationale "renamed the schema"

# Wait for an event matching a pattern
metis await --type build_passed --target api --timeout 600

# Stream all events
metis tail --agent swe
```

**Exit codes are part of the contract:** agents branch on them:
- `0` — allowed, or event found
- `1` — refused (safety rule blocked it, or resource held elsewhere)
- `2` — the run is over

## Running the agents — attached or spawned

Each agent can run **attached** (you open a terminal, the agent stays in it, woken by `metis tail`) or **spawned** (each event triggers a new claude session with context, the session runs, then exits).

**Recommended:**
- **SWE attached** — code changes need iteration, human oversight
- **DevOps spawned** — builds are deterministic, run fast and exit
- **Tester spawned** — tests are deterministic, run fast and exit

Configure it during setup. One hook config serves all three roles — the hooks read `$METIS_ROLE` at runtime.

## Safety — enforced before a prompt is written

Rules are not advice. They are enforced by hooks before the tool runs. An agent cannot talk itself around them because the check happens *before* the attempt, not after.

| Principle | Rule | Enforced by |
|---|---|---|
| **Separation of concerns** | DevOps cannot edit source code | `PreToolUse` hook |
| | SWE and Tester cannot deploy or push | `PreToolUse` hook |
| **Signal integrity** | Nobody edits the test that caught them | ledger-derived, tracked by hook |
| **Approval boundary** | Only a human can post `approved` | bus refuses otherwise |
| **Atomicity on cross-repo changes** | No repo in a change set is pushed until all build | `PreToolUse` hook |
| **Lease enforcement** | An agent must hold every lock its action claims | lease broker |
| **Iteration limits** | Past the cap, new claims are refused | lease broker |
| **Observability safety** | Deep probes never attach to LB-polled paths | probe policy |
| **Credential hygiene** | Secrets resolved, then redacted, never reversed | discovery |

**Agents never see raw tokens.** The substrate holds credentials and acts on their behalf. An agent cannot leak what it was never handed. Secrets are stored in the OS keychain, resolved at runtime only when needed, and redacted from every log before it's written.

## Intake — from issue tracker to work

```bash
metis setup jira
# or
metis setup trello
```

```bash
metis intake --dry-run      # preview what would be picked up
metis intake                # pull ready cards into the run
```

**Issue text is untrusted input.** A Jira description or Trello card could contain anything — including instructions trying to trick an agent into doing something unsafe. Metis:

- Wraps issue bodies between `<<<UNTRUSTED-ISSUE-TEXT` and `>>>` markers
- Flags instruction-shaped phrasing (e.g., "edit this file", "run this command") rather than stripping it
- Prevents a ticket from naming a target (repo, service, environment) outside the workspace — the card can only work on what discovery found
- Validates credentials before storing them — if you mistype your Jira URL or API token, the wizard catches it before it's too late

## Audit — see everything, trust what can be verified

```bash
metis why 42               # why did event #42 happen? trace back to what caused it
metis trace 1              # what did event #1 lead to? follow it forward
metis watch                # stream the ledger as it grows
metis report --out run.md  # timeline of every decision, every pause, every fault
```

Two tiers of evidence, kept strictly apart:

- **Ground truth** — commands that ran, files that changed, leases acquired and released. Written by hooks. Cannot be faked because they're hooks into the OS.
- **Testimony** — what an agent *claims* it did, why it stopped, what it tried. Written by agents. Useful but fallible.

When they disagree, believe the hooks. A hook says "your test ran and failed" is stronger than an agent saying "the test passed".

Every event carries a `caused_by` field — the event that triggered it. Follow the chain backward and you see the decision tree. Follow it forward and you see the ripples. No guessing. No lost time wondering what happened and why.

## Codebase layout

```
metis/
  bus/
    ├── schema.sql           SQLite ledger: events, leases, runs, cursors, changesets
    ├── store.py            transactional store with write safety
    ├── events.py           event posting, waiting, tailing, cursors
    ├── leases.py           exclusive locks with TTL and all-or-nothing acquisition
    ├── changesets.py       cross-repo change sets and the push gate
    ├── context.py          build up the prompt a turn-based agent needs
    ├── audit.py            trace, why, timeline, replay, run_checks
    └── commands.py         CLI: claim, post, await, tail, etc.

  discovery/
    ├── scan.py             walk repos, find git roots, manifests
    ├── capabilities.py     map source usage to capabilities (S3, databases, etc.)
    ├── iac.py              parse Terraform, CloudFormation, AWS CDK
    ├── deployment.py       find orchestration (ECS, K8s, Lambda, etc.)
    ├── testing.py          map tests to targets, discover coverage
    └── detectors/          per-language plugins for build/test commands

  intake/
    ├── jira.py             fetch, comment, transition in Jira
    ├── trello.py           fetch, comment, move in Trello
    └── sync.py             pull issues as requirements, push events as comments

  hooks/                     Pre/Post tool-use enforcement
    ├── __init__.py         dispatch, role-based checks
    └── pre_tool_use.py     refusals before a tool runs (safety)

  enforcement.py             what each role may write and run
  secrets.py                 OS keychain-backed, never logged
  dispatcher.py              spawned mode: one Claude session per event
```

**Design decisions:**
- Hooks and role prompts live inside the package so `metis hook pre` works everywhere `metis` is on PATH — no broken references after an install or move
- Stack support is a plugin file (`discovery/detectors/lang.py`); capability signatures and probe families are data, so adding a backing service is an edit, not a release
- Everything is testable offline; no external services, no network calls in tests

## Testing

```bash
pytest -q
```

210+ tests covering all modules, running against synthetic fixtures. Designed to run anywhere with no external services, no network, no secrets.
