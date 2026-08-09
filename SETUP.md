# Setup

Install, point it at your repositories, connect a tracker, and check it works.

- [Install](#install)
- [First run](#first-run)
- [Choosing what agents work on](#choosing-what-agents-work-on)
- [Connecting Jira](#connecting-jira)
- [Connecting Trello](#connecting-trello)
- [Git hosting](#git-hosting)
- [Installing the hooks](#installing-the-hooks)
- [Checking it works](#checking-it-works)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)

---

## Install

Requires Python 3.11+. Use a tool installer so `metis` lands on your PATH in its
own environment.

```bash
uv tool install git+https://github.com/rayhimanshu/metis.git
```

```bash
pipx install git+https://github.com/rayhimanshu/metis.git
```

```bash
metis --version
```

**`metis` must be on your PATH**, not just importable. The Claude Code hooks
invoke `metis hook pre`, so if the command cannot be found the safety rails
silently never fire — the worst way for a safety layer to fail.

### Developing on Metis itself

An editable install makes the CLI *be* your checkout, so changes take effect
with no reinstall:

```bash
git clone https://github.com/rayhimanshu/metis.git && cd metis && uv tool install --force --editable .
```

One consequence worth knowing: the CLI then follows whichever branch is checked
out. Behaviour can change because you switched branches, not because you edited
anything.

---

## First run

Run this **inside the directory you want agents to work in**:

```bash
metis setup
```

Or name the directory instead of moving to it:

```bash
metis setup --workspace ~/projects/my-platform
```

Pressing enter through every question produces a working configuration. Only
three things are ever required, and all three have defaults:

| Question | Required | Default |
|---|---|---|
| Workspace | yes | current directory |
| Environment name | yes | `dev` |
| Iteration cap | yes | `4` |
| Agent modes | yes | swe attached, devops + tester spawned |
| Work source | no | none — start runs by hand |
| Jira / Trello credentials | only if you choose that source | — |
| Git token | no | uses `gh` if authenticated |

Choose no work source and you are never asked for a credential. It is
re-runnable, offers previous answers as defaults, and backs up an existing
`metis.yaml` before rewriting.

Prefer a config file with no questions:

```bash
metis init
```

---

## Choosing what agents work on

**The workspace directory is the scope.** Whatever repositories live inside it
are what agents can see and touch. This is set by `run.workspace` in
`metis.yaml`.

### A single repository

```yaml
run:
  workspace: ~/code/my-service
```

### Several repositories

Point at the parent directory:

```yaml
run:
  workspace: ~/code          # contains service-a/, service-b/, service-c/
```

`metis discover` finds each as a separate target, because a different git root
is always a separate project no matter how the directories nest. Each gets its
own lock key:

```
worktree:service-a@main
worktree:service-b@main
```

So two agents can work on different repositories at the same time, and never on
the same one.

```bash
metis discover
```

### A change spanning repositories

Claim every key the change needs, in one call:

```bash
metis claim worktree:service-a@main worktree:service-b@main --ttl 900
```

All-or-nothing, acquired in sorted order. If the second key is held, the first
is handed back rather than sat on — holding half a set while waiting for the
rest is how two otherwise correct agents deadlock. Sorting is the cheapest total
order every process agrees on without coordinating.

### The limitation, stated plainly

**There is no include or exclude list.** If `~/code` holds ten repositories and
you only want agents near two of them, the workspace must be a directory
containing just those two — symlinks work fine:

```bash
mkdir -p ~/metis-scope && ln -s ~/code/service-a ~/code/service-b ~/metis-scope
```

Better to know this now than to discover it when an agent takes a lease on
something you did not mean to expose.

### Remote repositories

Discovery accepts a git URL and clones into `~/.metis/work`:

```bash
metis discover https://github.com/org/service.git
```

That is for inspection. Agents work on the local workspace.

---

## Connecting Jira

Three values. Two go in `metis.yaml`; the token goes in your keychain.

**1. API token** — create one at
<https://id.atlassian.com/manage-profile/security/api-tokens>.

**2. Your account email** — the address you sign in to Atlassian with, not your
display name.

**3. A JQL query** selecting work to pick up. Start narrow:

```
project = ENG AND status = "Ready for Dev" AND labels = metis
```

Then:

```bash
metis setup
```

Choose **Jira**. It asks for the URL, email, JQL, and the two status
transitions, prompts for the token, and **verifies against the live API before
finishing** — a token that silently lacks permission fails here rather than
three days later mid-run.

Resulting config:

```yaml
intake:
  jira:
    url: https://acme.atlassian.net
    email: you@acme.com
    jql: 'project = ENG AND status = "Ready for Dev" AND labels = metis'
    poll_seconds: 120
    on_start: In Progress
    on_done: In Review
```

`on_start` and `on_done` must be transitions that exist from the issue's current
status, spelled exactly.

---

## Connecting Trello

**1. API key** — go to <https://trello.com/power-ups/admin>, create a Power-Up
(any name), then open its **API key** tab. Trello moved this from the old
`trello.com/app-key` page, which now redirects here.

**2. Token** — from the same page, or visit this with your key substituted in:

```
https://trello.com/1/authorize?expiration=never&scope=read,write&response_type=token&name=Metis&key=YOUR_KEY_HERE
```

`scope=read,write` is required. Read fetches cards; write posts comments and
moves them. A read-only token pulls work fine and then silently fails every
attempt to report progress back.

**3. Board ID** — from the board URL
`https://trello.com/b/`**`abc123XY`**`/my-board`, take the bold part.

```bash
metis setup
```

Choose **Trello**, then supply the board ID, which list to pull from, and the
two list transitions. Resulting config:

```yaml
intake:
  trello:
    board_id: abc123XY
    list_name: Ready for Dev
    poll_seconds: 120
    on_start: In Progress
    on_done: Done
```

**Trello has no workflow states**, so a "transition" is a card moving to a
different list. `In Progress` and `Done` must already exist on that board,
spelled exactly. If a list is missing, intake still ingests the card and logs a
warning rather than failing — a tracker that will not move a card should not
stop the work.

### Before trusting either tracker

```bash
metis work --list
```

Shows exactly what Metis can see on your board, and flags any issue whose text
reads like instructions rather than a description. Writes nothing, moves
nothing -- looking is always free.

---

## Git hosting

Only needed if agents will push. Agents can build and test without it.

If `gh` is already authenticated, nothing to do — Metis uses it and stores
nothing. Asking for a second token to sit in a second place is how credentials
proliferate.

```bash
gh auth status
```

Otherwise:

```bash
metis setup git
```

---

## Installing the hooks

The hooks are what make role boundaries real rather than advisory. Run this in
each project agents will work on:

```bash
metis install-hooks
```

It **merges** into an existing `.claude/settings.json` rather than overwriting,
is idempotent, and refuses if `metis` is not on your PATH.

One hook configuration serves all three roles — they read `$METIS_ROLE` at
runtime, so there is no way to launch a session with the wrong hook set. Without
`METIS_ROLE` the hooks stay inert, so your ordinary Claude sessions are
unaffected.

### Customising the role prompts

The three prompts ship with the package, so a fresh install works with no files
to create. To change one, copy them into your project — project copies win:

```bash
metis roles --eject
```

---

## Checking it works

```bash
metis doctor
```

Reports config, workspace, agents, integrations, and — once a run exists — its
health. Add `--verify` to make live calls to your tracker.

### Picking up work

```bash
metis work
```

Fetches what is ready on your tracker, shows anything already in flight, and
lets you choose what to start. Taking one posts a `requirement` event and moves
that card to your in-progress list.

It starts a run if there is not one already, so nothing has to be named up
front.

Without a tracker, hand the agents a requirement directly:

```bash
metis init-run --requirement "Add a health-check endpoint"
```

Then open a session per agent:

```bash
METIS_ROLE=swe claude
```

At any point, ask what is happening:

```bash
metis
```

That reads the local ledger -- what is in flight, what finished, who holds a
lease. No network, so it answers instantly.

To keep the queue fed without being asked:

```bash
metis work --auto
```

It **feeds work in; it does not run agents.** Your sessions stay sessions you
can watch and interrupt. It also stops handing out new work while any task
needs a human, because burying that under three more is how unattended systems
fail quietly.

There is **no bus process to start**. The bus is a SQLite file at
`.metis/bus.db`; there is no daemon.

A complete four-terminal walkthrough lives in
[examples/calc-demo](examples/calc-demo/README.md).

---

## Troubleshooting

**Nothing is happening.** The most common failure is silence, not a crash.

```bash
metis doctor
```

It checks, in the order these usually bite: an event type no agent wakes on (a
typo in `wake_on` produces a system where every process is healthy and nothing
happens, forever); an agent whose cursor is behind because its tail died; a
lease held by something that is no longer running; and whether the iteration cap
has been reached.

**Hooks are not blocking anything.** Confirm `metis` is on the PATH of the shell
that launched the session, and that `METIS_ROLE` is set. Test directly:

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"git push"}}' | METIS_ROLE=swe metis hook pre
```

Exit code 2 with a `[metis] blocked:` message means they are working.

**An agent will not take a lease.** Someone else holds it, or the run is over:

```bash
metis leases
```

Exit codes are part of the contract: `0` granted, `1` refused, `2` the run has
ended.

**Reconstructing what happened.**

```bash
metis why 42
```

Walks backwards from an event through what caused it. `metis trace 42` walks
forwards, and `metis report` summarises the whole run. Commands and diffs in
that report were written by hooks rather than by agents — when an agent's
account disagrees with them, believe the hooks.

---

## Uninstall

```bash
uv tool uninstall metis-agents
```

```bash
rm -rf ~/.metis
```

Stored credentials are not removed with the package. On macOS:

```bash
for k in jira.api_token trello.key trello.token git.token; do security delete-generic-password -s metis -a "$k" 2>/dev/null; done; echo "keychain cleared"
```

On Linux, replace with `secret-tool clear service metis account <name>`.

Per-project files (`metis.yaml`, `.metis/`, and the hook entries in
`.claude/settings.json`) stay where they are; delete them per project if you
want them gone.
