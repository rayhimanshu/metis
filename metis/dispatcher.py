"""Spawned mode: wake a cold agent per event.

Attached agents pull -- they tail the bus and react. Spawned agents are pushed:
this process watches the bus and starts `claude -p` with a prompt built from
`metis context`.

Same bus, same roles, same hooks. Only the delivery differs, which is why the
mode is a config value rather than a fork in the design.

Three things this has to get right, all of which only show up under load:

* **Single flight.** One process per agent at a time. Two cold agents working
  the same target would each take a worktree lease, and the loser would sit
  burning tokens on a claim it will never get.
* **Debounce.** A burst of matching events must produce one spawn, not five.
  Cold agents are expensive and the last event supersedes the earlier ones
  anyway.
* **Release on exit.** A spawned process that dies holding a lease would
  otherwise wedge that resource until its TTL expires.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .bus import context as context_mod
from .bus import events as ev
from .bus import leases
from .bus.store import Store
from .config import AgentConfig, Config

POLL_SECONDS = 1.0
DEFAULT_DEBOUNCE = 3.0
DEFAULT_TIMEOUT = 1800


@dataclass
class Spawn:
    agent: str
    event_id: int
    process: subprocess.Popen
    started_at: float
    log_path: Path
    bus_event_id: int | None = None


@dataclass
class Pending:
    """The newest matching event an agent has not been woken for yet."""

    event_id: int
    first_seen: float
    count: int = 1


@dataclass
class Dispatcher:
    store: Store
    cfg: Config
    run_id: str
    dry_run: bool = False
    timeout: int = DEFAULT_TIMEOUT
    debounce: float = DEFAULT_DEBOUNCE
    only: list[str] | None = None

    running: dict[str, Spawn] = field(default_factory=dict)
    pending: dict[str, Pending] = field(default_factory=dict)
    log_dir: Path | None = None

    def __post_init__(self) -> None:
        self.log_dir = self.cfg.root / ".metis" / "spawns"
        self.log_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ agents

    def agents(self) -> dict[str, AgentConfig]:
        chosen = {
            name: agent for name, agent in self.cfg.agents.items()
            if agent.mode == "spawned"
        }
        if self.only:
            chosen = {n: a for n, a in chosen.items() if n in self.only}
        return chosen

    # ------------------------------------------------------------- cycle

    def collect(self) -> None:
        """Advance each agent's cursor and note the newest event worth waking for."""
        for name, agent in self.agents().items():
            cursor = ev.get_cursor(self.store, self.run_id, name)
            rows = ev.read_since(self.store, self.run_id, cursor, types=agent.wake_on)

            newest = None
            matched = 0
            for row in rows:
                # Never wake an agent for something it posted itself. Otherwise
                # an agent that both posts and wakes on a type spins forever.
                if row["agent"] == name:
                    ev.set_cursor(self.store, self.run_id, name, int(row["id"]))
                    continue
                newest = int(row["id"])
                matched += 1
                ev.set_cursor(self.store, self.run_id, name, newest)

            if newest is None:
                continue

            if name in self.pending:
                self.pending[name].event_id = newest
                self.pending[name].count += matched
            else:
                # `count` is what a whole burst folded into, not how many polls
                # saw it. Under-reporting hides how much was coalesced away.
                self.pending[name] = Pending(
                    event_id=newest, first_seen=time.monotonic(), count=matched
                )

    def reap(self) -> None:
        """Collect finished spawns and hand back anything they still hold."""
        for name, spawn in list(self.running.items()):
            code = spawn.process.poll()
            if code is None:
                if time.monotonic() - spawn.started_at > self.timeout:
                    spawn.process.kill()
                    code = -9
                else:
                    continue

            released = leases.release_all(self.store, self.run_id, name)
            del self.running[name]

            ev.post(
                self.store, self.run_id, "agent_exited", agent=name,
                caused_by=spawn.bus_event_id,
                payload={"exit": code, "released_leases": released,
                         "log": str(spawn.log_path), "seconds": round(time.monotonic() - spawn.started_at)},
                rationale=f"spawned run for event #{spawn.event_id} finished",
            )
            status = "ok" if code == 0 else f"exit {code}"
            extra = f", released {released} lease(s)" if released else ""
            print(f"  {name}: finished ({status}){extra} -> {spawn.log_path}", flush=True)

    def ready(self) -> list[tuple[str, Pending]]:
        now = time.monotonic()
        return [
            (name, pending) for name, pending in self.pending.items()
            if name not in self.running and now - pending.first_seen >= self.debounce
        ]

    def spawn(self, name: str, pending: Pending) -> None:
        ctx = context_mod.build(self.store, self.cfg, name, event_id=pending.event_id)
        prompt = context_mod.render(ctx)

        coalesced = f" (coalesced {pending.count} events)" if pending.count > 1 else ""
        if self.dry_run:
            print(f"  would spawn {name} for event #{pending.event_id}{coalesced} "
                  f"({len(prompt)} char prompt)", flush=True)
            del self.pending[name]
            return

        claude = shutil.which("claude")
        if not claude:
            print("  claude CLI not found on PATH -- cannot spawn", flush=True)
            del self.pending[name]
            return

        log_path = self.log_dir / f"{name}-{pending.event_id}.log"
        env = {
            **_base_env(),
            "METIS_ROLE": name,
            "METIS_HOME": str(self.cfg.root),
            "METIS_RUN_ID": self.run_id,
        }

        bus_event_id = ev.post(
            self.store, self.run_id, "agent_spawned", agent=name,
            caused_by=pending.event_id,
            payload={"mode": "spawned", "trigger": pending.event_id,
                     "coalesced": pending.count, "log": str(log_path)},
            rationale=f"woken by event #{pending.event_id}",
        )

        handle = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            [claude, "-p", "--permission-mode", "acceptEdits"],
            stdin=subprocess.PIPE, stdout=handle, stderr=subprocess.STDOUT,
            cwd=self.cfg.workspace, env=env, text=True,
        )
        process.stdin.write(prompt)
        process.stdin.close()

        self.running[name] = Spawn(
            agent=name, event_id=pending.event_id, process=process,
            started_at=time.monotonic(), log_path=log_path, bus_event_id=bus_event_id,
        )
        del self.pending[name]
        print(f"  {name}: spawned for event #{pending.event_id}{coalesced} -> {log_path}",
              flush=True)

    def tick(self) -> None:
        self.reap()
        self.collect()
        for name, pending in self.ready():
            self.spawn(name, pending)

    def run(self) -> int:
        names = ", ".join(self.agents()) or "(none)"
        print(f"dispatching for: {names}", flush=True)
        print(f"debounce {self.debounce}s · timeout {self.timeout}s"
              f"{' · dry run' if self.dry_run else ''}\n", flush=True)

        try:
            while True:
                run = self.store.get_run(self.run_id)
                if run and run["status"] != "RUNNING":
                    self.reap()
                    print(f"run is {run['status']} -- stopping", flush=True)
                    return 0
                self.tick()
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            print("\nstopping; waiting for in-flight agents", flush=True)
            for spawn in self.running.values():
                spawn.process.terminate()
            self.reap()
            return 0


def _base_env() -> dict[str, str]:
    import os

    # Pass the environment through rather than a minimal set: `claude` needs
    # PATH, HOME, and its own credentials to run at all.
    return dict(os.environ)
