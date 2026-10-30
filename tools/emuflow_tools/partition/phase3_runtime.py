"""Low-overhead, nested runtime measurements for the Phase 3 data plane."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import inspect
import os
from pathlib import Path
import resource
import sys
import threading
import time
from typing import Any, Callable, Dict, Iterator, Optional, TypeVar

from emuflow.common.json_io import write_json


PHASE3_RUNTIME_SCHEMA = "emuflow.phase3-runtime/v1"
_SAMPLE_INTERVAL_SECONDS = 0.5
_ACTIVE_PROFILER: ContextVar[Optional["Phase3RuntimeProfiler"]] = ContextVar(
    "emuflow_phase3_runtime_profiler", default=None
)
_Result = TypeVar("_Result")


def _tree_rss_kib() -> int:
    pending, visited, total = [os.getpid()], set(), 0
    while pending:
        pid = pending.pop()
        if pid in visited:
            continue
        visited.add(pid)
        try:
            root = Path(f"/proc/{pid}")
            for line in (root / "status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1])
            for task in (root / "task").iterdir():
                pending.extend(map(int, (task / "children").read_text().split()))
        except (OSError, ValueError):
            pass
    if total:
        return total
    high_water = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return high_water // 1024 if sys.platform == "darwin" else high_water


def _cpu_seconds() -> tuple[float, float]:
    parent = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (
        parent.ru_utime + parent.ru_stime,
        children.ru_utime + children.ru_stime,
    )


def _process_io_bytes() -> Optional[tuple[int, int]]:
    try:
        values = {}
        for line in Path("/proc/self/io").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in {"read_bytes", "write_bytes"}:
                values[key] = int(value)
        if values.keys() == {"read_bytes", "write_bytes"}:
            return values["read_bytes"], values["write_bytes"]
    except (OSError, ValueError):
        pass
    return None


@dataclass
class _ActiveStep:
    name: str
    parent: Optional[str]
    started_wall: float
    started_parent_cpu: float
    started_child_cpu: float
    started_io: Optional[tuple[int, int]]
    peak_tree_rss_kib: int


@dataclass
class Phase3RuntimeProfiler:
    output_dir: Path
    metadata: Dict[str, Any]
    started_wall: float = field(default_factory=time.monotonic)
    started_parent_cpu: float = 0.0
    started_child_cpu: float = 0.0
    started_io: Optional[tuple[int, int]] = None
    peak_tree_rss_kib: int = 0
    _records: list[Dict[str, Any]] = field(default_factory=list)
    _observations: Dict[str, int] = field(default_factory=dict)
    _active: list[_ActiveStep] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _done: threading.Event = field(default_factory=threading.Event)
    _worker: Optional[threading.Thread] = None

    def start(self) -> None:
        self.started_parent_cpu, self.started_child_cpu = _cpu_seconds()
        self.started_io = _process_io_bytes()
        self.peak_tree_rss_kib = _tree_rss_kib()
        self._worker = threading.Thread(target=self._sample, daemon=True)
        self._worker.start()

    def _sample(self) -> None:
        while not self._done.wait(_SAMPLE_INTERVAL_SECONDS):
            rss = _tree_rss_kib()
            with self._lock:
                self.peak_tree_rss_kib = max(self.peak_tree_rss_kib, rss)
                for active in self._active:
                    active.peak_tree_rss_kib = max(active.peak_tree_rss_kib, rss)

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        parent_cpu, child_cpu = _cpu_seconds()
        with self._lock:
            active = _ActiveStep(
                name=name,
                parent=self._active[-1].name if self._active else None,
                started_wall=time.monotonic(),
                started_parent_cpu=parent_cpu,
                started_child_cpu=child_cpu,
                started_io=_process_io_bytes(),
                peak_tree_rss_kib=_tree_rss_kib(),
            )
            self._active.append(active)
        status = "failed"
        try:
            yield
            status = "pass"
        finally:
            ended_wall = time.monotonic()
            ended_parent_cpu, ended_child_cpu = _cpu_seconds()
            ended_io = _process_io_bytes()
            final_rss = _tree_rss_kib()
            record: Dict[str, Any] = {
                "name": name,
                "parent": active.parent,
                "status": status,
                "wall_seconds": ended_wall - active.started_wall,
                "parent_cpu_seconds": ended_parent_cpu - active.started_parent_cpu,
                "child_cpu_seconds": ended_child_cpu - active.started_child_cpu,
                "peak_process_tree_rss_kib": max(
                    active.peak_tree_rss_kib, final_rss
                ),
            }
            if active.started_io is not None and ended_io is not None:
                record["parent_read_bytes"] = max(
                    0, ended_io[0] - active.started_io[0]
                )
                record["parent_write_bytes"] = max(
                    0, ended_io[1] - active.started_io[1]
                )
            with self._lock:
                self._active.remove(active)
                self._records.append(record)

    def finish(self, status: str) -> Dict[str, Any]:
        final_rss = _tree_rss_kib()
        with self._lock:
            self.peak_tree_rss_kib = max(self.peak_tree_rss_kib, final_rss)
        self._done.set()
        if self._worker is not None:
            self._worker.join()
        ended_parent_cpu, ended_child_cpu = _cpu_seconds()
        ended_io = _process_io_bytes()
        result: Dict[str, Any] = {
            "schema": PHASE3_RUNTIME_SCHEMA,
            "status": status,
            **self.metadata,
            "wall_seconds": time.monotonic() - self.started_wall,
            "parent_cpu_seconds": ended_parent_cpu - self.started_parent_cpu,
            "child_cpu_seconds": ended_child_cpu - self.started_child_cpu,
            "peak_process_tree_rss_kib": self.peak_tree_rss_kib,
            "rss_measurement": "sampled-process-tree-at-0.5s",
            "io_measurement": "linux-parent-process-proc-io-if-available",
            "steps": list(self._records),
            "observations": dict(sorted(self._observations.items())),
        }
        if self.started_io is not None and ended_io is not None:
            result["parent_read_bytes"] = max(0, ended_io[0] - self.started_io[0])
            result["parent_write_bytes"] = max(0, ended_io[1] - self.started_io[1])
        return result


def measure_phase3_runtime(
    function: Callable[..., _Result],
) -> Callable[..., _Result]:
    signature = inspect.signature(function)

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> _Result:
        arguments = signature.bind(*args, **kwargs)
        arguments.apply_defaults()
        profiler = Phase3RuntimeProfiler(
            output_dir=Path(arguments.arguments["output_dir"]),
            metadata={
                "provider": arguments.arguments["provider"],
                "cut_mode": arguments.arguments["cut_mode"],
            },
        )
        prior_output_exists = profiler.output_dir.exists()
        result = None
        token = _ACTIVE_PROFILER.set(profiler)
        profiler.start()
        status = "failed"
        try:
            result = function(*args, **kwargs)
            status = "pass"
            return result
        finally:
            runtime = profiler.finish(status)
            try:
                if status == 'pass' and isinstance(result, dict) and result.get('stage_implementation_id') == 'native-phase3-v1':
                    import json
                    path = profiler.output_dir/'phase3-runtime.json'
                    if path.stat().st_size > 8*1024*1024:
                        raise ValueError('native Phase 3 runtime exceeds bound')
                    runtime['native_stage'] = json.loads(path.read_text())
                if status == 'pass' or not prior_output_exists:
                    write_json(profiler.output_dir/'phase3-runtime.json', runtime, compact=True)
            except Exception:
                # Preserve the original Phase 3 failure if measurement
                # publication also encounters a broken output directory.
                if status == "pass":
                    raise
            finally:
                _ACTIVE_PROFILER.reset(token)

    return wrapped


@contextmanager
def phase3_runtime_step(name: str) -> Iterator[None]:
    profiler = _ACTIVE_PROFILER.get()
    if profiler is None:
        yield
        return
    with profiler.step(name):
        yield


def phase3_runtime_call(
    name: str, function: Callable[..., _Result], *args: Any, **kwargs: Any
) -> _Result:
    with phase3_runtime_step(name):
        return function(*args, **kwargs)


def phase3_runtime_observe(**values: int) -> None:
    profiler = _ACTIVE_PROFILER.get()
    if profiler is None:
        return
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Phase 3 runtime observation {name!r} is invalid")
    with profiler._lock:
        profiler._observations.update(values)
