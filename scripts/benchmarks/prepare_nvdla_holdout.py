#!/usr/bin/env python3

"""Prepare one canonical NVDLA RTL contract for PPro and EmuFlow.

The preparation is intentionally small: it references the pinned upstream RTL
in place and writes only three generated overlay files. Both flows must consume
the resulting benchmark-run contract, so frontend quirks cannot silently create
different designs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from emuflow.benchmark import BenchmarkRun
from emuflow.io import write_json
from scripts.benchmarks.nvdla_ram_stubs import (
    BLACKBOX_MEMORY_POLICY,
    MEMORY_POLICIES,
    PHYSICAL_MEMORY_POLICY,
    generate as generate_ram_wrappers,
)


ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "benchmarks" / "rtl_catalog.json"
COMPAT_PATH = ROOT / "scripts" / "yosys" / "nvdla_compat.v"
PREPARATION_SCHEMA = "emuflow.nvdla-preparation/v1"
GENERATOR_ID = "nvdla-shared-frontend-v2"
DEFINES = [
    "SYNTHESIS",
    "DESIGNWARE_NOEXIST",
    "NVDLA_BDMA_ENABLE",
    "NVDLA_CDP_ENABLE",
    "NVDLA_PDP_ENABLE",
    "NVDLA_RUBIK_ENABLE",
]
_CPP_DIRECTIVE = re.compile(r"^#(ifdef|ifndef|else|endif)", re.MULTILINE)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


def _catalog_entry(catalog_path: Path) -> Dict[str, Any]:
    value = json.loads(catalog_path.read_text(encoding="utf-8"))
    if value.get("schema") != "emuflow.rtl-catalog/v1":
        raise ValueError("RTL catalog schema is invalid")
    matches = [
        item for item in value.get("designs", []) if item.get("id") == "nvdla"
    ]
    if len(matches) != 1:
        raise ValueError("RTL catalog must contain exactly one NVDLA entry")
    return matches[0]


def _validate_pinned_source(
    source_root: Path, catalog: Dict[str, Any]
) -> Dict[str, Any]:
    stamp_path = source_root / ".emuflow-source.json"
    if not stamp_path.is_file():
        raise ValueError("NVDLA source lacks .emuflow-source.json")
    stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    expected = {
        "schema": "emuflow.source-archive/v1",
        "design_id": "nvdla",
        "revision": catalog["revision"],
        "archive_sha256": catalog["archive_sha256"],
    }
    if any(stamp.get(key) != value for key, value in expected.items()):
        raise ValueError("NVDLA source stamp disagrees with the pinned RTL catalog")
    return stamp


def _contained_relative(path: Path, root: Path) -> str:
    resolved = path.resolve()
    if root not in resolved.parents or not resolved.is_file():
        raise ValueError(f"NVDLA source escapes its pinned root: {path}")
    return resolved.relative_to(root).as_posix()


def prepare_nvdla_holdout(
    *,
    source_root: Path,
    generated_dir: Path,
    benchmark_path: Path,
    platform: str,
    memory_policy: str = BLACKBOX_MEMORY_POLICY,
    catalog_path: Path = CATALOG_PATH,
    compat_path: Path = COMPAT_PATH,
) -> Dict[str, Any]:
    """Write generated overlays and the shared NVDLA benchmark contract."""

    if memory_policy not in MEMORY_POLICIES:
        raise ValueError(f"unsupported NVDLA memory policy {memory_policy!r}")

    root = source_root.resolve()
    generated = generated_dir.resolve()
    if root not in generated.parents:
        raise ValueError("NVDLA generated overlay must stay below its source root")
    catalog = _catalog_entry(catalog_path.resolve())
    stamp = _validate_pinned_source(root, catalog)

    rtl_root = root / "vmod" / "nvdla"
    library_root = root / "vmod" / "vlibs"
    include_root = root / "vmod" / "include"
    ram_root = root / "vmod" / "rams" / "synth"
    rtl_sources = sorted(rtl_root.glob("*/*.v"))
    library_sources = sorted(library_root.glob("*.v"))
    if len(rtl_sources) < 250 or not include_root.is_dir():
        raise ValueError("NVDLA source tree is incomplete")

    partition_o = rtl_root / "top" / "NV_NVDLA_partition_o.v"
    designware_lsd = library_root / "NV_DW_lsd.v"
    if partition_o not in rtl_sources or designware_lsd not in library_sources:
        raise ValueError("NVDLA frontend substitutions are missing upstream inputs")
    if not compat_path.resolve().is_file():
        raise ValueError("NVDLA Yosys compatibility source does not exist")

    generated.mkdir(parents=True, exist_ok=True)
    normalized_partition = generated / "NV_NVDLA_partition_o.v"
    ram_wrappers = generated / "nvdla_ram_wrappers.v"
    compat_copy = generated / "nvdla_compat.v"

    original_partition = partition_o.read_text(encoding="utf-8")
    normalized_text, replacement_count = _CPP_DIRECTIVE.subn(
        lambda match: "`" + match.group(1), original_partition
    )
    if replacement_count == 0 or _CPP_DIRECTIVE.search(normalized_text):
        raise ValueError("NVDLA partition_o directive normalization did not complete")
    normalized_partition.write_text(normalized_text, encoding="utf-8")
    compat_copy.write_text(
        compat_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    wrapper_count, modeled_count = generate_ram_wrappers(
        ram_root, ram_wrappers, memory_policy
    )
    expected_modeled = wrapper_count if memory_policy == PHYSICAL_MEMORY_POLICY else 0
    if modeled_count != expected_modeled:
        raise ValueError("NVDLA RAM modeling coverage is incomplete")

    ordered_sources: List[Path] = [ram_wrappers]
    ordered_sources.extend(
        compat_copy if path == designware_lsd else path for path in library_sources
    )
    ordered_sources.extend(
        normalized_partition if path == partition_o else path for path in rtl_sources
    )
    if len({path.resolve() for path in ordered_sources}) != len(ordered_sources):
        raise ValueError("canonical NVDLA source list contains duplicate files")
    relative_sources = [_contained_relative(path, root) for path in ordered_sources]

    generated_records = [
        {"path": _contained_relative(path, root), "sha256": _sha256(path)}
        for path in (normalized_partition, ram_wrappers, compat_copy)
    ]
    preparation = {
        "schema": PREPARATION_SCHEMA,
        "generator_id": GENERATOR_ID,
        "upstream_revision": stamp["revision"],
        "upstream_archive_sha256": stamp["archive_sha256"],
        "memory_policy": memory_policy,
        "partition_directive_replacements": replacement_count,
        "ram_wrapper_count": wrapper_count,
        "ram_modeled_count": modeled_count,
        "generated_files": generated_records,
        "source_list_sha256": _canonical_sha256(relative_sources),
    }
    memory_variant = (
        "physical" if memory_policy == PHYSICAL_MEMORY_POLICY else "scale"
    )
    benchmark = {
        "schema": "emuflow.benchmark-run/v1",
        "id": f"nvdla_nvdlav1_l7_shared_frontend_{memory_variant}",
        "design_id": "nvdla",
        "top": "NV_nvdla",
        "sources": relative_sources,
        "clocks": ["dla_core_clk", "dla_csb_clk"],
        "clock_periods_ns": {"dla_core_clk": 10.0, "dla_csb_clk": 10.0},
        "platform": platform,
        "synthesis": {
            "family": "xcup",
            "policy": "native",
            "include_dirs": ["vmod/include"],
            "defines": DEFINES,
        },
        "preparation": preparation,
    }
    BenchmarkRun(benchmark)
    write_json(benchmark_path.resolve(), benchmark)
    write_json(generated / "preparation-manifest.json", preparation)
    return {
        "status": "pass",
        "benchmark": benchmark,
        "benchmark_path": str(benchmark_path.resolve()),
        "source_count": len(relative_sources),
        "generated_file_count": len(generated_records),
        "memory_policy": preparation["memory_policy"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare the shared PPro/EmuFlow NVDLA frontend contract."
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--generated-dir", type=Path, required=True)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument(
        "--memory-policy",
        choices=sorted(MEMORY_POLICIES),
        default=BLACKBOX_MEMORY_POLICY,
    )
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    parser.add_argument("--compat", type=Path, default=COMPAT_PATH)
    args = parser.parse_args()
    report = prepare_nvdla_holdout(
        source_root=args.source_root,
        generated_dir=args.generated_dir,
        benchmark_path=args.benchmark,
        platform=args.platform,
        memory_policy=args.memory_policy,
        catalog_path=args.catalog,
        compat_path=args.compat,
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
