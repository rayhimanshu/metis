#!/usr/bin/env bash
# Lay out a Metis run in one tmux window, ready to record.
#
# Four floating Terminal windows look chaotic on camera and cannot be captured
# by a single screen region. One window with four labelled panes can:
#
#     +----------------+---------------------------+
#     |                |  SWE - writes code        |
#     |    LEDGER      +---------------------------+
#     |  metis watch   |  DEVOPS - builds, deploys |
#     |                +---------------------------+
#     |                |  TESTER - verifies        |
#     +----------------+---------------------------+
#
# The ledger gets the tall left pane because it carries the story: every claim,
# every handoff, every refusal shows up there in order.
#
# Usage:  ./demo-layout.sh [session-name]
set -euo pipefail

SESSION="${1:-metis}"

command -v tmux >/dev/null || { echo "tmux is not installed: brew install tmux" >&2; exit 1; }
command -v metis >/dev/null || {
  echo "metis is not on PATH. The hooks invoke 'metis hook pre', so without it" >&2
  echo "the safety rails silently never fire -- which is the one thing worth" >&2
  echo "showing on camera. Install it first." >&2
  exit 1
}

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "Session '$SESSION' already exists. Attaching."
  exec tmux attach -t "$SESSION"
fi

# Plain hyphens in titles on purpose: tmux renders an em dash as an underscore.
tmux new-session -d -s "$SESSION" -x 240 -y 64 \; \
  send-keys 'metis watch' C-m \; select-pane -T ' LEDGER ' \; \
  split-window -h \; send-keys 'METIS_ROLE=swe claude' C-m \; \
    select-pane -T ' SWE - writes code ' \; \
  split-window -v \; send-keys 'METIS_ROLE=devops claude' C-m \; \
    select-pane -T ' DEVOPS - builds and deploys ' \; \
  split-window -v \; send-keys 'METIS_ROLE=tester claude' C-m \; \
    select-pane -T ' TESTER - verifies ' \; \
  select-layout main-vertical

tmux set -t "$SESSION" -g main-pane-width 96
tmux set -t "$SESSION" -g pane-border-status top
tmux set -t "$SESSION" -g pane-border-format '#[bold]#{pane_title}'
tmux set -t "$SESSION" -g status off          # one less thing on screen
tmux select-layout -t "$SESSION" main-vertical
tmux select-pane -t "$SESSION".0

cat <<'NOTES'
Before you record:

  * Font 16-18pt. Default terminal text is unreadable once a video is compressed.
  * export PS1="$ "   so paths and git branches do not clutter the frame.
  * Focus mode on, dock hidden, full screen.
  * Cmd+Shift+5 -> Record Selected Portion -> drag around the terminal only.

Paste into each agent pane as its first message, changing the role each time:

  You are the SWE agent in a Metis run. Run `metis context --agent swe` to get
  your role and the current state, then follow it. Arm a Monitor on
  `metis tail --agent swe` so events wake you, and keep working until the
  requirement is done or the iteration cap is reached.

Detach with ctrl-b d. Reset between takes with:

  tmux kill-session -t metis && rm -rf .metis && metis work

NOTES

exec tmux attach -t "$SESSION"
