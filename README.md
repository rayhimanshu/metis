<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <img src="assets/logo.svg" alt="Metis" width="96" height="96">
  </picture>
</p>

<h1 align="center">Metis</h1>

<p align="center">
  <em>Autonomous coding agents. One shared ledger. No manager.</em>
</p>

<p align="center">

[![CI](https://github.com/rayhimanshu/metis/actions/workflows/ci.yml/badge.svg)](https://github.com/rayhimanshu/metis/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</p>

### SWE writes it. DevOps ships it and checks it.

Two Claude sessions, two terminals, one codebase — and nobody telling either of
them what to do. Work arrives from Jira or Trello. They leave notes for each
other in a shared ledger and get on with it.

```
#11  09:08:15 requirement      intake    [api]
       why: Trello LG-42: Add a health-check endpoint
#14  09:12:03 test_failed      devops    [api] <-#11
       why: endpoint 500s when the cache is cold
#16  09:19:02 code_ready       swe       [api] <-#14
       why: initialise the cache before the probe reads it
#18  09:20:44 build_passed     devops    [api] <-#16
#21  09:23:18 test_passed      devops    [api] <-#16
```

Fifteen minutes. A card became a fix, the regression in the middle repaired
itself, and nobody was in the loop.

That `<-#14` is a receipt. Every event names the one that caused it, so you can
always ask *why did this happen* and get an answer instead of a guess.

And when an agent has a clever idea — like quietly editing the test that just
caught it — it doesn't get to. The rules are code, not requests.

Metis is told nothing about your language, your cloud, or your services. It
reads the repo and works it out.

| | |
|---|---|
| **[SETUP.md](SETUP.md)** | install, pick your repos, connect Jira or Trello, go live |
| **[DESIGN.md](DESIGN.md)** | why a substrate, not an orchestrator — the boundary between dumb and smart |
| **[PROTOCOL.md](PROTOCOL.md)** | the ledger: schema, event types, locks, and how changes that span repos stay coherent |
| **[AUDIT.md](AUDIT.md)** | seeing every decision, every pause, every fault — and why each one happened |
| **[metis/roles/](metis/roles/)** | the prompt for each agent — SWE, DevOps, and an optional Tester |

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

To take them out again, leaving any hooks you added yourself alone:

```bash
metis install-hooks --remove
```

Merges Metis hooks into your project's Claude settings. Refuses if `metis` is not on your PATH (prevents silent failures).

### 4. Start your first run

```bash
metis init-run --requirement "Add a health-check endpoint"
```

Creates a run and waits for agents to pick up work.

### 5. Start the agents

```bash
metis start
```

Opens four terminal windows -- the ledger and all three agents, each already
briefed. Nothing clever happens: every window runs the command you would type
yourself.

```bash
metis start --print
```

shows exactly what will be invoked, and opens nothing:

```bash
#!/usr/bin/env bash
cd /your/project || exit 1
export METIS_ROLE=swe
exec claude 'You are the SWE agent in a Metis run. Run `metis context ...'
```

For one tmux window with panes instead of four windows -- better for recording:

```bash
metis start --tmux
```

It refuses to start when something would make the run quietly useless -- hooks
not installed, no run to work on, an agent still set to `spawned`, or `metis`
missing from PATH. That last one is the dangerous case: the hooks shell out to
`metis hook pre`, so without it **nothing is enforced and the run looks
identical to one that is.**

Detach with `ctrl-b d`. To do it by hand instead, four terminals:

```bash
metis watch
```

```bash
METIS_ROLE=swe claude
```

...and the same for `devops` and `tester`, each given the briefing that
`metis start` passes automatically.

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

The workspace is your scope. Point it at one repo, or at a parent holding
several:

```bash
metis setup --workspace ~/projects/my-platform
```

`metis.yaml` is written into that directory, beside the code it describes.
Without the flag the wizard asks, defaulting to where you are.

Discovery treats each git root as its own target with its own lock. Agents can work on different repositories simultaneously — never on the same one at the same time.

Full details: [SETUP.md](SETUP.md#choosing-what-agents-work-on).

### A third agent is optional

The default is two. A build fails, DevOps has the log, SWE fixes it — that loop
is two agents and a ledger, and adding a third to watch it is ceremony.

A Tester earns its place when verification is genuinely separate work: a suite
someone must run against a deployed environment, or an independent judgement
about whether a fix holds. Then configure one:

```yaml
agents:
  tester:
    mode: attached
    role: roles/tester.md
    wake_on: [deployed]
```

Without one, DevOps verifies its own work and posts `test_passed` itself. It is
not an independent judge, and the ledger records who posted what — but a stalled
run is worse than a self-reported pass, and `test_passed` is what the dashboard,
the tracker transition and the completion summary all key on. DevOps still
cannot edit source to make a test go green.

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

Real output — `+` is an actionable capability, `~` is a derived lock key:

```
source      /path/to/platform  (local)
targets     6  languages: java, javascript, python
deployable  maven-service   local-only: gradle-service, node-pnpm, python-suite
LB polls    /actuator/health   (deep probes must not attach here)

  maven-service  [java_maven]  deploy=aws_ecs {'cluster': 'demo-cluster', ...}
      + postgres        relational
      + s3              object_store    perms=read,write,delete,list
      ~ build  needs worktree:platform@main
      ~ deploy needs branch:platform@main, cluster:demo-cluster, schema:demodb
      ~ covered by:     python-suite
```

What it declines is reported too, with the reason it fell short:

```
skipped (reported, not acted on):
  - maven-service/firestore [medium] declared and configured, but no main-source
    usage found -- the dependency is present without evidence the code exercises it
```

That rule — **decline what you cannot justify** — is what makes unattended operation defensible. No guessing. No wishful thinking. IaC never creates a finding on its own; it enriches one. Permissions are bounds: an agent granted `s3:GetObject` gets a read-only probe, not a write that was always going to fail.

## The ledger protocol

The ledger is the substrate. Agents interact with it via CLI:

```bash
# Claim exclusive access to a target
metis claim worktree:api@main --ttl 900

# Record an event
metis post --type code_ready --target api --rationale "renamed the schema"

# Block until one of these event types appears
metis await --for build_passed,build_failed --timeout 600

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

## What agents may start alone

Most work is safe to hand over: a bug fix, a new endpoint, a failing test. The
blast radius is a diff, the tests judge it, and a mistake costs a rerun.

Some work is not. Adding a service, taking a new dependency, migrating a schema,
resizing infrastructure -- these commit money, lock in structure, and are
painful to walk back. **The tests still pass, so nothing downstream catches
them.**

So work is classified as it arrives, and the expensive kind stops at a gate:

```
refused: 'billing' is waiting on human review: LG-77 (changes a database
schema; has a cost or capacity implication). Run `metis groom` to review it.
Architectural work is not started unattended.
```

That is the lease broker refusing, not a prompt asking nicely. An agent may only
act while holding every key its action declares, so refusing the lock refuses
the work.

```bash
metis groom
```

Shows what is waiting and why, lets you refine the requirement, then approve or
reject. Only a human can clear a gate -- the bus refuses `approved` from
anything else.

### Two rules keep it honest

**The tracker is the authority.** Label a card `architecture`, `migration`, or
`needs-review` and it gates, whatever the text says.

**Cloud changes gate on what discovery can read.** Sizing knobs
(`desired_count`, `instance_class`, `multi_az`, node pools, instance families
like `db.r6g.2xlarge`) and managed resources that bill the moment they exist
(NAT gateways, load balancers, clusters, Aurora, CloudFront) all stop for
review. Grooming then shows what your IaC declares today, so you compare
against a real value:

```
   infrastructure declared in this workspace
     resources: aws_db_instance, aws_ecs_service
     currently provisioned:
       allocated_storage      500 (infra/main.tf:3)
       desired_count          6 (infra/main.tf:8)
       instance_class         db.r6g.2xlarge (infra/main.tf:2)
       multi_az               true (infra/main.tf:4)
     ^ read from your IaC. Metis attaches no prices -- check your
       provider's calculator before approving a sizing change.
```

**Metis never estimates a price.** Cloud pricing is regional, tiered,
usage-dependent and moves constantly; a figure generated here would be invented,
and would be trusted precisely because it looked precise. What it gives you is
the current value and the proposed change. The number comes from your provider.

**Schema work splits in two.** Adding a column, a table, or an index is
ordinary feature work and stays autonomous. Dropping, renaming, altering, or
migrating does not -- no test catches a dropped column, because the build is
green precisely because it is gone.

**Heuristics may only escalate.** Wording can raise a task to gated; nothing
can lower one. A classifier that quietly decides "just a bug fix" and is wrong
is the single failure a safety gate cannot have, so that direction is
impossible rather than unlikely. A ticket claiming to be pre-approved changes
nothing.

Ambiguity therefore costs you thirty seconds. The opposite mistake costs a
production migration nobody agreed to.

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

## Picking up work

```bash
metis
```

The dashboard, read straight from the local ledger — what is in flight, what
finished, what holds a lease. No network, so it answers instantly and works
offline.

```
run 20260809-115048   RUNNING   iteration 1/4   env=dev

in flight (1)
    LG-42        Add a health-check endpoint                  [api]
        waiting -- code ready, waiting on a build

finished (1)
    LG-43        Cache warmup                                 [api]
        done -- tests passed

leases (1)
    swe holds worktree:api@main (slot 0) until 06:30:49
```

To take something on:

```bash
metis work
```

Fetches what is ready on your board, shows it alongside anything already
running, and lets you choose:

```
ready to pick up (2)
 1. LG-44        Add retry to the payment webhook
 2. LG-45        Drop the legacy /v1 health route

Which? numbers like 1 or 1,3 -- 'a' for all, enter to skip
> 1
```

Looking is free: `metis work` posts nothing and moves no card until you pick.
Taking one posts a `requirement` event and moves that card to your in-progress
list.

### Without being asked

```bash
metis work --auto
```

Polls the tracker and keeps the queue fed — one task at a time by default
(`--max-in-flight`). It **feeds work in; it does not run agents.** Your three
sessions stay sessions you can watch and interrupt. And it stops handing out
new work the moment a task halts, because burying the thing that needs a human
under three more is how unattended systems fail quietly.

### Cloud credentials are checked, never stored

```bash
metis setup aws     # or gcp, azure, alicloud
```

```
Checking AWS
Metis stores no cloud credentials. Your CLI already resolves them
through SSO, assumed roles, or instance metadata, and DevOps runs
that CLI directly.

  ok  account 111122223333, region eu-west-1 as deployer

DevOps will deploy as this identity. Check it is the one you meant.
```

Nothing is asked for and nothing is written. Your cloud CLI already resolves
credentials through SSO sessions, assumed roles, and instance metadata — all
short-lived and rotating. Pasting a static access key so Metis could keep a
second copy would swap that for a long-lived secret and call it convenience.

What this catches is the silent case: **DevOps cannot deploy and nothing says
so** until a deploy fails halfway through. `metis doctor --verify` checks every
provider, and reports the identity rather than just a tick — a failing deploy is
far more often the wrong role than a missing credential.

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

## What lands back on the ticket

When work finishes, the card gets one substantial comment -- not a running
commentary. A comment per build attempt is what trains people to stop reading a
ticket, so the detail is spent on the moment someone actually looks.

```markdown
**Metis: LG-42 complete**

_Add a health-check endpoint_

**Approach (approved by human)**
Add /health-check, separate from the LB-polled /actuator/health,
so a cache blip cannot drain every task.

**Acceptance criteria, as written on this ticket**
- returns 200 when the cache is reachable
- returns 503 when it is not

**Changed** — 3 file(s), +75 −1
- `src/api/health.py` (+41 −0)
- `src/api/routes.py` (+6 −1)
- `tests/test_health.py` (+28 −0)

**Verified**
- build passed
- test passed — 31 checks

**Commands run** — 3, 1 failed
- `pytest -q` exited 1

---
_Written from the Metis ledger. File changes and commands are recorded by
hooks as they happen, not reported by the agent._
```

Every line of that is **ground truth**: file changes and commands were recorded
by hooks as the tool ran, so nothing can be embellished or quietly left out --
including the build that failed on the way. Acceptance criteria are lifted
**verbatim** from what you wrote; Metis has no way to know what "done" means for
your work, so it never invents them.

Autonomous tasks get the same treatment. Nobody has to ask what an agent did.

### Proposing before building

An agent can read code without holding a lease, so a gated task can still be
thought about -- the gate stops writing, not thinking. SWE posts
`design_proposed` with its approach, and `metis groom` shows it beside the
ticket, so one review covers both the work and the way it is going to be done.

## Audit — see everything, trust what can be verified

```bash
metis why 42               # why did event #42 happen? trace back to what caused it
metis trace 1              # what did event #1 lead to? follow it forward
metis watch                # stream the ledger as it grows
metis report --out run.md  # timeline of every decision, every pause, every fault
```

Two tiers of evidence, kept strictly apart:

- **Ground truth** — commands that ran, files that changed, leases acquired and released. Written by hooks as the tool call happens, so an agent cannot author, omit, or edit them.
- **Testimony** — what an agent *claims* it did, why it stopped, what it tried. Written by agents. Useful, and fallible.

When they disagree, believe the hooks. "The command ran and exited 1" outranks "the tests passed".

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
    └── detectors/          java_maven, java_gradle, node, python

  intake/
    ├── jira.py             fetch, comment, transition in Jira
    ├── trello.py           fetch, comment, move in Trello
    └── sync.py             pull issues as requirements, push events as comments

  hooks/
    ├── __init__.py             PreToolUse refusals, PostToolUse ground truth
    └── settings.template.json  the hook wiring metis install-hooks merges

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

227 tests, run against synthetic fixtures in `fixtures/`. No external services, no network, no credentials -- they run anywhere, including CI on a fresh clone.
