"""Explicit congestion-guided post-DP LUT packing experiment.

The C++ mover consumes the complete qualified site domain. Its packing target
is a search objective, never a replacement for the device's eight-LUT capacity.
The candidate requires normal exact placement certification before routing.
"""
from pathlib import Path
import json
import os
import re
import subprocess
import tempfile

from .errors import ValidationError
from .architecture import ArchitectureDB
from .io import file_sha256
from .native_tools import resolve_native_executable
from .xilinx_openparf_atomic import load_xilinx_openparf_atomic_sites
from .xilinx_openparf_name_map_storage import read_xilinx_openparf_atomic_name_map


def _run_hot_site_transform(
    placement: Path, name_map_path: Path, output: Path, *,
    mapped_sha256: str, packed_sha256: str, architecture_path: Path, hot_clock_regions,
    executable_name, native_arguments, schema, provider, parameters,
    physical_slice_coordinates=False,
):
    """Publish a separately sealed candidate from a native hot-site experiment."""
    placement, name_map_path, output = map(Path, (placement, name_map_path, output))
    if output.resolve() == placement.resolve():
        raise ValidationError("LUT depopulation must publish a separate candidate")
    hot = set(hot_clock_regions)
    if not hot or any(not isinstance(cr, str) or not cr for cr in hot):
        raise ValidationError("explicit hot clock regions are required")
    executable = resolve_native_executable(executable_name)
    architecture_path = Path(architecture_path)
    architecture_sha256 = file_sha256(architecture_path)
    name_map = read_xilinx_openparf_atomic_name_map(
        name_map_path, mapped_sha256=mapped_sha256, packed_sha256=packed_sha256,
        architecture_sha256=architecture_sha256,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Neither the normalized site database nor the name-map header stores
        # per-site regions. Join by physical site identity to the source-sealed
        # ArchitectureDB; never infer region ownership from solver coordinates.
        architecture = ArchitectureDB.load(architecture_path)
        region_by_site = {site["name"]: (site.get("physical_region") or {}).get("clock_region")
                          for site in architecture.value["sites"]}
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
                    architecture_sha256=architecture_sha256,
                ):
                    if (site.get("resources", {}).get("LUT", 0) < 8
                            or len(site.get("physical_sites", {}).get("LUT", [])) != 1):
                        continue
                    physical_site = site["physical_sites"]["LUT"][0]
                    if physical_slice_coordinates and (
                        site.get("resources", {}).get("FF", 0) < 16
                        or site.get("physical_sites", {}).get("FF") != [physical_site]
                    ):
                        raise ValidationError("slice spreading requires qualified LUT/FF sites")
                    x, y = site["dense_x"], site["dense_y"]
                    cr = region_by_site.get(physical_site)
                    if not isinstance(cr, str) or not cr:
                        raise ValidationError("qualified LUT site has no ArchitectureDB clock region")
                    if cr in hot:
                        matched.add(cr)
                    if physical_slice_coordinates:
                        match = re.fullmatch(r"SLICE_X([0-9]+)Y([0-9]+)", physical_site)
                        if match is None:
                            raise ValidationError("slice spreading requires physical SLICE coordinates")
                        f.write(f"{x} {y} {match[1]} {match[2]} {int(cr in hot)}\n")
                    else:
                        f.write(f"{x} {y} {int(cr in hot)}\n")
            if matched != hot:
                raise ValidationError("hot region is absent from qualified LUT site domain")
            candidate = root / "candidate.pl"
            completed = subprocess.run(
                [executable, str(site_path), str(atom_path), str(placement), str(candidate),
                 *map(str, native_arguments)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )
            if completed.returncode:
                raise ValidationError("native hot-site transform failed: " + completed.stderr[-4000:])
            counters = json.loads(completed.stdout)
            before = file_sha256(placement)
            with candidate.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(candidate, output)
            return {
                "schema": schema, "status": "candidate", "provider": provider,
                "placement_before_sha256": before, "placement_after_sha256": file_sha256(output),
                "mapped_sha256": mapped_sha256, "packed_sha256": packed_sha256,
                "architecture_sha256": architecture_sha256,
                "hot_clock_regions": sorted(hot), **parameters, "counters": counters,
                "requires_exact_placement_validation": True,
            }
    finally:
        close = getattr(name_map, "close", None)
        if callable(close):
            close()


def depopulate_hot_lut_sites(
    placement: Path, name_map_path: Path, output: Path, *,
    mapped_sha256: str, packed_sha256: str, architecture_path: Path, hot_clock_regions,
    maximum_luts: int = 6, maximum_radius: int = 128,
):
    """Move only singleton LUTs out of explicitly selected hot clock regions."""
    if (isinstance(maximum_luts, bool) or not isinstance(maximum_luts, int)
            or not 1 <= maximum_luts <= 8 or isinstance(maximum_radius, bool)
            or not isinstance(maximum_radius, int) or not 0 <= maximum_radius <= 4096):
        raise ValidationError("invalid LUT packing target or displacement bound")
    return _run_hot_site_transform(
        placement, name_map_path, output, mapped_sha256=mapped_sha256,
        packed_sha256=packed_sha256, architecture_path=architecture_path,
        hot_clock_regions=hot_clock_regions, executable_name="emuflow_xilinx_lut_depopulation",
        native_arguments=[maximum_luts, maximum_radius],
        schema="emuflow.xilinx-lut-depopulation/v1", provider="native-nearest-free-singleton-lut-v1",
        parameters=dict(maximum_luts=maximum_luts, maximum_radius=maximum_radius),
    )


def spread_hot_slice_sites(
    placement: Path, name_map_path: Path, output: Path, *,
    mapped_sha256: str, packed_sha256: str, architecture_path: Path, hot_clock_regions,
    scale_numerator: int = 11, scale_denominator: int = 10, maximum_target_distance: int = 16,
):
    """Dilate ordinary occupied slices while retaining each slice's packing."""
    if (any(isinstance(v, bool) or not isinstance(v, int)
            for v in (scale_numerator, scale_denominator, maximum_target_distance))
            or not 1 <= scale_denominator <= 1024
            or not scale_denominator < scale_numerator <= 2 * scale_denominator
            or not 0 <= maximum_target_distance <= 64):
        raise ValidationError("invalid slice dilation ratio or target search radius")
    return _run_hot_site_transform(
        placement, name_map_path, output, mapped_sha256=mapped_sha256,
        packed_sha256=packed_sha256, architecture_path=architecture_path,
        hot_clock_regions=hot_clock_regions, executable_name="emuflow_xilinx_slice_spreading",
        native_arguments=[scale_numerator, scale_denominator, maximum_target_distance],
        schema="emuflow.xilinx-slice-spreading/v1", provider="native-rigid-slice-dilation-v1",
        parameters=dict(scale_numerator=scale_numerator, scale_denominator=scale_denominator,
                        maximum_target_distance=maximum_target_distance), physical_slice_coordinates=True,
    )
