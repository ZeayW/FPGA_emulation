"""Bounded stage wall time and sampled process-tree RSS measurements."""

import os
from pathlib import Path
import resource
import threading
import time

from .io import write_json


def _tree_rss_kib():
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
    if not total:
        high_water = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes; Linux reports KiB.
        import sys
        total = high_water // 1024 if sys.platform == "darwin" else high_water
    return total


def measure_stage(output_dir, stage, function, *args, **kwargs):
    done = threading.Event()
    peak = [_tree_rss_kib()]

    def sample():
        while not done.wait(0.5):
            peak[0] = max(peak[0], _tree_rss_kib())

    worker = threading.Thread(target=sample, daemon=True)
    started = time.monotonic()
    worker.start()
    status = "failed"
    try:
        result = function(*args, **kwargs)
        status = "pass"
        return result
    finally:
        peak[0] = max(peak[0], _tree_rss_kib())
        done.set()
        worker.join()
        record = {"stage": stage, "status": status,
                  "wall_seconds": time.monotonic() - started,
                  "peak_process_tree_rss_kib": peak[0],
                  "rss_measurement": "sampled-at-0.5s"}
        write_json(Path(output_dir) / "stage-metrics" / f"{stage}.json", record)
        print(f"STAGE_COMPLETE {stage} {status} {record['wall_seconds']:.3f}s RSS={peak[0]}KiB", flush=True)
