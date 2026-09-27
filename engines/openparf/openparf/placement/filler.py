#!/usr/bin/env python3
"""Numerically stable filler geometry for analytical placement."""

import math


def compute_filler_geometry(total_area, hint_size, max_fillers=None):
    """Return a filler count and size while preserving ``total_area`` exactly.

    OpenPARF historically emitted one filler per median-size movable cell.  A
    capacity-normalized FPGA model can represent sixteen LUT/FF slots in one
    site, making each logical cell only 1/16 site area and multiplying filler
    tensors by the same factor.  A cap changes only the sampling granularity:
    every filler is enlarged so their summed density remains unchanged.
    """

    total_area = float(total_area)
    width = float(hint_size[0])
    height = float(hint_size[1])
    if not math.isfinite(total_area) or total_area < 0:
        raise ValueError("filler area must be finite and non-negative")
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        raise ValueError("filler hint size must be finite and positive")
    if max_fillers is not None:
        if isinstance(max_fillers, bool) or not isinstance(max_fillers, int):
            raise ValueError("filler cap must be a positive integer")
        if max_fillers <= 0:
            raise ValueError("filler cap must be a positive integer")
    if total_area == 0:
        return 0, (0.0, 0.0)

    natural_count = max(int(total_area / (width * height)), 1)
    count = (
        natural_count
        if max_fillers is None
        else min(natural_count, max_fillers)
    )
    filler_area = total_area / count
    aspect_ratio = height / width
    return count, (
        math.sqrt(filler_area / aspect_ratio),
        math.sqrt(filler_area * aspect_ratio),
    )
