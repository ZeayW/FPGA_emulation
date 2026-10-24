"""Explicit congestion-guided post-DP LUT packing experiment.

The C++ mover consumes the complete qualified site domain. Its packing target
is a search objective, never a replacement for the device's eight-LUT capacity.
The candidate requires normal exact placement certification before routing.
"""
from pathlib import Path
import json
import os
import subprocess
import tempfile

from .errors import ValidationError
from .io import file_sha256
from .native_tools import resolve_native_executable
from .xilinx_openparf_atomic import load_xilinx_openparf_atomic_sites
from .xilinx_openparf_name_map_storage import read_xilinx_openparf_atomic_name_map


def _hot_region_boxes(name_map, hot):
    """Use the sealed dense-grid region contract, not per-site metadata.

    The normalized site database intentionally stores geometry/resources only.
    Source names survive region reindexing as ``source_name`` in this contract.
    """
    contract = name_map.get("coordinate_system", {}).get("clock_regions")
    regions = contract.get("regions") if isinstance(contract, dict) else None
    if not isinstance(regions, list) or not regions:
        raise ValidationError("LUT depopulation requires sealed clock-region geometry")
    boxes, names = [], set()
    for region in regions:
        if not isinstance(region, dict):
            raise ValidationError("invalid sealed clock-region geometry")
        name = region.get("source_name", region.get("name"))
        box = region.get("bbox")
        if (not isinstance(name, str) or not name or name in names
                or not isinstance(box, list) or len(box) != 4
                or any(isinstance(v, bool) or not isinstance(v, int) for v in box)
                or not 0 <= box[0] <= box[2] or not 0 <= box[1] <= box[3]):
            raise ValidationError("invalid sealed clock-region geometry")
        names.add(name)
        if name in hot:
            boxes.append((name, box))
    if not hot <= names:
        raise ValidationError("hot region is absent from sealed clock-region geometry")
    for i, (_, a) in enumerate(boxes):
        for _, b in boxes[:i]:
            if max(a[0], b[0]) <= min(a[2], b[2]) and max(a[1], b[1]) <= min(a[3], b[3]):
                raise ValidationError("overlapping sealed hot clock-region geometry")
    return boxes


def depopulate_hot_lut_sites(
    placement: Path, name_map_path: Path, output: Path, *,
    mapped_sha256: str, packed_sha256: str, hot_clock_regions,
    maximum_luts: int = 6, maximum_radius: int = 128,
):
    """Move only singleton LUTs out of explicitly selected hot clock regions."""
    placement, name_map_path, output = map(Path, (placement, name_map_path, output))
    if output.resolve() == placement.resolve():
        raise ValidationError("LUT depopulation must publish a separate candidate")
    hot = set(hot_clock_regions)
    if not hot or any(not isinstance(cr, str) or not cr for cr in hot):
        raise ValidationError("explicit hot clock regions are required")
    if (isinstance(maximum_luts, bool) or not isinstance(maximum_luts, int)
            or not 1 <= maximum_luts <= 8 or isinstance(maximum_radius, bool)
            or not isinstance(maximum_radius, int) or not 0 <= maximum_radius <= 4096):
        raise ValidationError("invalid LUT packing target or displacement bound")
    executable = resolve_native_executable("emuflow_xilinx_lut_depopulation")
    name_map = read_xilinx_openparf_atomic_name_map(
        name_map_path, mapped_sha256=mapped_sha256, packed_sha256=packed_sha256,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        boxes = _hot_region_boxes(name_map, hot)
        with tempfile.TemporaryDirectory(prefix=".lut-depopulation-", dir=output.parent) as temp:
            root = Path(temp)
            atom_path, site_path = root / "atoms.tsv", root / "sites.tsv"
            with atom_path.open("w", encoding="utf-8") as f:
                for atom in name_map["atoms"]:
                    fields = [atom[k] for k in ("openparf", "source_cluster", "resource", "cell_type")]
                    # Names are normalized solver identifiers; source cluster
                    # identities may contain whitespace, so encode that field.
                    fields[1] = fields[1].encode("utf-8").hex()
                    f.write("\t".join(fields) + "\n")
            matched = set()
            with site_path.open("w", encoding="utf-8") as f:
                for site in load_xilinx_openparf_atomic_sites(
                    name_map_path, resources=["LUT"],
                    mapped_sha256=mapped_sha256, packed_sha256=packed_sha256,
                ):
                    if (site.get("resources", {}).get("LUT", 0) < 8
                            or len(site.get("physical_sites", {}).get("LUT", [])) != 1):
                        continue
                    x, y = site["dense_x"], site["dense_y"]
                    regions = [name for name, (xl, yl, xh, yh) in boxes
                               if xl <= x <= xh and yl <= y <= yh]
                    matched.update(regions)
                    f.write(f"{x} {y} {int(bool(regions))}\n")
            if matched != hot:
                raise ValidationError("hot region is absent from qualified LUT site domain")
            candidate = root / "candidate.pl"
            completed = subprocess.run(
                [executable, str(site_path), str(atom_path), str(placement), str(candidate),
                 str(maximum_luts), str(maximum_radius)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )
            if completed.returncode:
                raise ValidationError("native LUT depopulation failed: " + completed.stderr[-4000:])
            counters = json.loads(completed.stdout)
            before = file_sha256(placement)
            with candidate.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(candidate, output)
            return {
                "schema": "emuflow.xilinx-lut-depopulation/v1", "status": "candidate",
                "provider": "native-nearest-free-singleton-lut-v1",
                "placement_before_sha256": before, "placement_after_sha256": file_sha256(output),
                "mapped_sha256": mapped_sha256, "packed_sha256": packed_sha256,
                "hot_clock_regions": sorted(hot), "maximum_luts": maximum_luts,
                "maximum_radius": maximum_radius, "counters": counters,
                "requires_exact_placement_validation": True,
            }
    finally:
        close = getattr(name_map, "close", None)
        if callable(close):
            close()
