"""OpenPARF driver that publishes continuous global-placement coordinates.

The upstream Bookshelf writer accepts only discrete legal site locations.  A
global-only OpenPARF run intentionally has not performed that legalization, so
using the upstream writer would either misrepresent the result or abort.  This
small driver keeps the upstream placement engine intact and replaces only its
final serializer with a finite-coordinate writer for EmuFlow's downstream
architecture-aware legalizer.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
from pathlib import Path
from typing import Any


def write_continuous_placement(engine: Any, output_path: Path) -> None:
    """Serialize every real instance from ``data_cls.pos`` without snapping."""

    positions = engine.data_cls.pos[0]
    instances = int(engine.data_cls.inst_locs_xyz.shape[0])
    if int(positions.shape[0]) < instances or int(positions.shape[1]) < 2:
        raise RuntimeError("OpenPARF global position tensor is incomplete")
    xy = positions[:instances, :2].detach().cpu().tolist()
    lines = ["# EmuFlow continuous OpenPARF global-placement guidance"]
    for index, coordinate in enumerate(xy):
        x, y = float(coordinate[0]), float(coordinate[1])
        if not math.isfinite(x) or not math.isfinite(y):
            raise RuntimeError(
                "OpenPARF global placement produced a non-finite coordinate "
                f"for {engine.placedb.instName(index)}"
            )
        lines.append(f"{engine.placedb.instName(index)} {x:.17g} {y:.17g} 0")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run OpenPARF and export continuous global coordinates"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--log", required=True)
    arguments = parser.parse_args()

    # Imported lazily so unit tests for the serializer do not require a built
    # OpenPARF runtime.
    from openparf.flow import place
    from openparf.params import Params
    from openparf.placement import placer

    params = Params()
    params.load(arguments.config)
    if params.legalize_flag or params.detailed_place_flag:
        raise RuntimeError(
            "continuous OpenPARF driver requires legalization and detailed "
            "placement to be disabled"
        )
    Path(arguments.log).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=arguments.log,
        filemode="w",
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )
    os.environ["OMP_NUM_THREADS"] = str(params.num_threads)

    def _write(engine: Any, filename: str) -> None:
        write_continuous_placement(engine, Path(filename))

    placer.Placer.write = _write
    output = Path(params.result_dir) / f"{params.design_name()}.pl"
    place(params, str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
