"""Independent pinned NVDLA source inventory for small preparation fixtures."""
from pathlib import Path
from typing import List


def collect_nvdla_source_files(repo_root: Path) -> List[Path]:
    """Return every pinned upstream file that determines NVDLA synthesis."""

    repo = repo_root.resolve()
    source = repo / "third_party" / "rtl" / "nvdla"
    paths = {source / ".emuflow-source.json"}
    paths.update((source / "vmod" / "nvdla").glob("*/*.v"))
    paths.update((source / "vmod" / "vlibs").glob("*.v"))
    paths.update(
        path
        for path in (source / "vmod" / "include").rglob("*")
        if path.is_file()
    )
    paths.update((source / "vmod" / "rams" / "synth").glob("nv_ram_*.v"))
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f"missing NVDLA source dependency: {missing[0]}")
    rtl_count = sum(
        1
        for path in paths
        if source / "vmod" / "nvdla" in path.parents and path.suffix == ".v"
    )
    if rtl_count < 250:
        raise ValueError(
            f"incomplete NVDLA source tree: found only {rtl_count} RTL files"
        )
    return sorted(paths)
