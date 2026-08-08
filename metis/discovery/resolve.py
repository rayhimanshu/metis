"""Normalise a source into a working tree.

Everything downstream must not care whether the user gave a local path or a git
URL, so that distinction ends here and nowhere else.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The forms git itself accepts: scp-style (git@host:org/repo), any scheme:// URL,
# or a path ending in .git.
_GIT_URL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*://|[\w.-]+@[\w.-]+:)|\.git/?$")


@dataclass
class ResolvedSource:
    root: Path
    kind: str  # local | git
    url: str | None = None
    ref: str | None = None


def looks_like_git_url(source: str) -> bool:
    """A directory on disk always wins over a URL-shaped guess.

    Without this, a local directory literally named `something.git` would be
    cloned from itself.
    """
    if Path(source).expanduser().is_dir():
        return False
    return bool(_GIT_URL.search(source))


def repo_name_from_url(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def resolve(source: str, work_dir: Path, ref: str | None = None) -> ResolvedSource:
    if not looks_like_git_url(source):
        root = Path(source).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"not a directory: {root}")
        return ResolvedSource(root=root, kind="local")

    work_dir.mkdir(parents=True, exist_ok=True)
    dest = work_dir / repo_name_from_url(source)

    if (dest / ".git").is_dir():
        # Refresh rather than re-clone, so repeated runs stay cheap.
        _run(["git", "fetch", "--all", "--prune"], cwd=dest)
        if ref:
            checkout = _run(["git", "checkout", ref], cwd=dest)
            if checkout.returncode != 0:
                raise RuntimeError(f"git checkout {ref} failed: {checkout.stderr.strip()}")
            _run(["git", "reset", "--hard", f"origin/{ref}"], cwd=dest)
    else:
        args = ["git", "clone"]
        if ref:
            args += ["--branch", ref]
        args += [source, str(dest)]
        clone = _run(args)
        if clone.returncode != 0:
            raise RuntimeError(f"git clone failed: {clone.stderr.strip()}")

    return ResolvedSource(root=dest.resolve(), kind="git", url=source, ref=ref)


def default_work_dir() -> Path:
    return Path(os.environ.get("METIS_WORK_DIR", Path.home() / ".metis" / "work"))
