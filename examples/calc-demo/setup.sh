#!/usr/bin/env bash
# Prepare the demo: a real repo with a working test suite, a Metis config, the
# safety hooks installed, and a run waiting to be picked up.
#
# Idempotent -- re-run it to reset and start over.
set -euo pipefail

cd "$(dirname "$0")"

REQUIREMENT="Add a divide(a, b) function to the calc package. It must raise \
ValueError with a clear message when b is zero. Cover it with tests."

# The demo needs to be its own repository. Agents key their worktree lease on
# the enclosing git root and use its HEAD as the rollback anchor, so running
# this inside the Metis checkout would have them leasing and resetting Metis
# itself.
if [ ! -d .git ] && git rev-parse --git-dir >/dev/null 2>&1; then
  echo "This directory sits inside another git repository." >&2
  echo >&2
  echo "Copy it out first, so the agents work on the demo and not on the" >&2
  echo "repository containing it:" >&2
  echo >&2
  echo "  cp -r \"$(pwd)\" ~/calc-demo && cd ~/calc-demo && ./setup.sh" >&2
  exit 1
fi

if ! command -v metis >/dev/null; then
  echo "metis is not on PATH." >&2
  echo >&2
  echo "The hooks invoke 'metis hook pre', so without it on PATH the safety" >&2
  echo "rails silently never fire. Install it first:" >&2
  echo >&2
  echo "  pipx install git+https://github.com/rayhimanshu/metis.git" >&2
  echo "  # or: uv tool install git+https://github.com/rayhimanshu/metis.git" >&2
  exit 1
fi

echo "==> Python environment"
python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt
./venv/bin/python -m pytest -q

# The demo needs its own git history: agents take a worktree lease keyed on the
# repository, and the rollback anchor is a commit.
if [ ! -d .git ]; then
  echo "==> git"
  git init -q
  git add -A
  git -c user.name=demo -c user.email=demo@local commit -qm "calc: add and subtract"
fi

echo "==> Metis"
rm -rf .metis
metis install-hooks --force >/dev/null
metis discover >/dev/null
metis init-run --requirement "$REQUIREMENT" >/dev/null

echo
metis
echo
echo "Ready. Open four terminals in this directory and follow README.md:"
echo "  1) metis watch"
echo "  2) METIS_ROLE=swe claude"
echo "  3) METIS_ROLE=devops claude"
echo "  4) METIS_ROLE=tester claude"
