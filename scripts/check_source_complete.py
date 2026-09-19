#!/usr/bin/env python3

"""Reject opaque build artifacts and incomplete direct provider source trees."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional


REQUIRED_SOURCE_FILES = {
    "capnproto": (
        "CMakeLists.txt",
        "c++/src/capnp/schema.capnp",
        "c++/src/capnp/serialize.h",
        "LICENSE",
        "EMUFLOW_PROVENANCE.md",
    ),
    "fpga-interchange-schema": (
        "interchange/DeviceResources.capnp",
        "interchange/LogicalNetlist.capnp",
        "interchange/References.capnp",
        "third_party/capnproto-java/capnp/java.capnp",
        "third_party/capnproto-java/LICENSE",
        "LICENSE",
        "EMUFLOW_PROVENANCE.md",
    ),
    "cudd": (
        "configure",
        "cudd/cudd.h",
        "cudd/cuddAPI.c",
        "LICENSE",
        "EMUFLOW_PROVENANCE.md",
    ),
    "yosys": (
        "Makefile",
        "kernel/yosys.cc",
        "abc/Makefile",
        "libs/cxxopts/include/cxxopts.hpp",
        "COPYING",
        "EMUFLOW_PROVENANCE.md",
    ),
    "repart": (
        "RePart/partitioner.cpp",
        "RePart/solution_audit.h",
        "RePart/datastructure/hypergraph.h",
        "LICENSE",
        "EMUFLOW_PROVENANCE.md",
    ),
    "openroad": (
        "CMakeLists.txt",
        "src/par/src/TritonPart.cpp",
        "src/sta/CMakeLists.txt",
        "src/gui/resources/resource.qrc",
        "src/gui/resources/icon.png",
        "src/gui/resources/google_icons/LICENSE",
        "src/gui/resources/google_icons/round_zoom_in_black_36dp.png",
        "third-party/abc/CMakeLists.txt",
        "LICENSE",
        "EMUFLOW_PROVENANCE.md",
    ),
    "openparf": (
        "CMakeLists.txt",
        "cmake/Ccache.cmake/CMakeLists.txt",
        "cmake/Ccache.cmake/LICENSE",
        "openparf/placement/placer.py",
        "openparf/ops/electric_potential/src/electric_force.cpp",
        "thirdparty/blend2d/CMakeLists.txt",
        "thirdparty/blend2d/LICENSE.md",
        "thirdparty/googletest/CMakeLists.txt",
        "thirdparty/googletest/LICENSE",
        "thirdparty/pugixml/CMakeLists.txt",
        "thirdparty/pugixml/LICENSE.md",
        "thirdparty/pybind11/CMakeLists.txt",
        "thirdparty/pybind11/LICENSE",
        "thirdparty/lemon/cmake/version.cmake",
        "thirdparty/lemon/LICENSE",
        "thirdparty/rapidcsv/rapidcsv.h",
        "thirdparty/yaml-cpp/CMakeLists.txt",
        "thirdparty/yaml-cpp/LICENSE",
        "openparf/routing/fpga-router/3rdparty/clipp/clipp/clipp.h",
        "openparf/routing/fpga-router/3rdparty/gdstk/gdstk.h",
        "openparf/routing/fpga-router/3rdparty/lemon/LICENSE",
        "openparf/routing/fpga-router/3rdparty/pugixml/pugixml/pugixml.hpp",
        "openparf/routing/fpga-router/3rdparty/taskflow/taskflow/taskflow.hpp",
        "LICENSE",
        "EMUFLOW_PROVENANCE.md",
    ),
    "vtr": (
        "CMakeLists.txt",
        "vpr/src/main.cpp",
        "vpr/src/pack/pack.cpp",
        "vpr/src/route/route.cpp",
        "libs/libarchfpga/src/read_xml_arch_file.cpp",
        "libs/librrgraph/src/base/rr_graph_builder.cpp",
        "libs/EXTERNAL/libsdcparse/CMakeLists.txt",
        "libs/EXTERNAL/yaml-cpp/CMakeLists.txt",
        "LICENSE.md",
        "EMUFLOW_PROVENANCE.md",
    ),
}

REQUIRED_FIRST_PARTY_NATIVE_FILES = (
    "src/native/vtr_architecture_importer.cpp",
    "src/emuflow/vtr_architecture.py",
    "src/emuflow/vpr.py",
    "src/emuflow/open_physical_flow.py",
    "src/native/vpr_packed_netlist_importer.cpp",
    "src/emuflow/packed_netlist.py",
    "schemas/architecture-timing-db-v1.schema.json",
    "schemas/open-physical-flow-v1.schema.json",
    "schemas/vpr-packed-netlist-v1.schema.json",
    "resources/architectures/vtr/flagship-k6-n10-40nm.json",
    "examples/architecture/vtr_k6_heterogeneous_fixture.xml",
    "examples/physical/vpr_packed_fixture.net",
    "src/native/fpga_interchange_arch_importer.cpp",
    "src/emuflow/fpga_interchange.py",
    "src/emuflow/rapidwright_provider.py",
    "src/emuflow/physical_regions.py",
    "schemas/archdb-v1.schema.json",
    "schemas/rapidwright-device-provider-v1.schema.json",
    "schemas/rapidwright-route-resource-certificate-v1.schema.json",
    "schemas/physical-region-sidecar-v1.schema.json",
    "resources/rapidwright/xcvu19p-fsva3824-2-e.provider.json",
    "scripts/rapidwright/export_physical_regions.py",
    "scripts/rapidwright/EmuFlowRouteResourceCertificate.java",
    "scripts/rapidwright/export_route_resource_certificate.py",
    "src/native/tlr_router.cpp",
    "src/native/hop_partition_refiner.cpp",
    "src/emuflow/partition_hops.py",
    "src/native/mfspart_coarsener.cpp",
    "src/emuflow/mfspart.py",
    "src/native/mfspart_initializer.cpp",
    "src/emuflow/mfspart_initial.py",
    "src/native/mfspart_refiner.cpp",
    "src/native/mfspart_refiner_checker.cpp",
    "src/emuflow/mfspart_refine.py",
    "src/emuflow/mfspart_provider.py",
    "src/native/mfspart_legalizer.cpp",
    "src/emuflow/mfspart_legalize.py",
    "src/native/tdm_ratio_optimizer.cpp",
    "src/native/tdm_partition_feedback.cpp",
    "src/native/placement_aware_pin_planner.cpp",
    "src/native/chimew_signal_grouper.cpp",
    "src/native/chimew_position_refiner.cpp",
    "src/native/chimew_rudy.cpp",
    "src/native/chimew_bank_channel_assigner.cpp",
    "src/emuflow/chimew_grouping.py",
    "src/emuflow/chimew_refinement.py",
    "src/emuflow/chimew_rudy.py",
    "src/emuflow/chimew_bank_channel.py",
    "src/native/bsp_pin_solver.cpp",
    "schemas/tdm-ratio-plan-v1.schema.json",
    "schemas/sta-path-database-v1.schema.json",
    "schemas/fpga-timing-model-v1.schema.json",
    "schemas/fpga-timing-model-v2.schema.json",
    "schemas/partition-net-weights-v1.schema.json",
    "schemas/signal-position-hints-v1.schema.json",
    "schemas/placement-aware-pin-plan-v1.schema.json",
    "schemas/hardware-bsp-v1.schema.json",
    "schemas/package-pin-binding-v1.schema.json",
    "schemas/boarddb-v1.schema.json",
    "schemas/serial-wrapper-v1.schema.json",
    "schemas/serial-phy-provider-v1.schema.json",
    "schemas/serial-phy-provider-v2.schema.json",
    "schemas/serial-phy-elaboration-v1.schema.json",
    "schemas/serial-phy-recipe-v1.schema.json",
    "schemas/vivado-pin-site-map-v2.schema.json",
    "src/emuflow/board_arm_mps4.py",
    "src/emuflow/physical_pins.py",
    "src/emuflow/serial_wrapper.py",
    "src/emuflow/serial_contract.py",
    "src/emuflow/serial_phy_provider.py",
    "src/emuflow/serial_phy_elaboration.py",
    "src/emuflow/serial_phy_recipe.py",
    "providers/vivado_gty_10g/generate_gty_ip.tcl",
    "providers/vivado_gty_10g/recipe.json",
    "providers/vivado_gty_10g/LICENSE",
    "scripts/generate_synthetic_vu9p_bsp.py",
    "platforms/synthetic/xcvu9p_4fpga_mesh_bsp.json",
    "scripts/vivado/export_cut_timing_paths.tcl",
    "scripts/vivado/export_timing_path_database.tcl",
    "scripts/vivado/analyze_timing.tcl",
    "scripts/vivado/implement_partition.tcl",
    "scripts/opensta/export_timing_path_database.tcl",
    "scripts/yosys/logic_only_map.v",
    "scripts/yosys/vtr_models.v",
    "scripts/yosys/vtr_multiply_map.v",
    "scripts/yosys/vtr_memories.txt",
    "scripts/yosys/vtr_memory_map.v",
    "examples/rtl/vtr_hard_blocks.v",
    "tests/check_vtr_hard_block_synthesis.py",
    "tests/test_open_physical_flow.py",
    "resources/timing/ultrascaleplus-softlogic-v1.json",
    "src/emuflow/physical_backend.py",
    "src/emuflow/vivado_backend.py",
    "src/emuflow/vivado_netlist.py",
    "src/emuflow/contest_eda2025.py",
    "src/emuflow/contest_eda2024.py",
    "src/emuflow/contest_iccad2019.py",
    "schemas/contest-eda2024-evaluation-v1.schema.json",
    "schemas/contest-iccad2019-instance-v1.schema.json",
    "schemas/contest-iccad2019-evaluation-v1.schema.json",
    "scripts/fetch_repart_benchmarks.py",
    "schemas/contest-eda2025-instance-v1.schema.json",
    "schemas/contest-eda2025-evaluation-v1.schema.json",
    "schemas/physical-backend-v1.schema.json",
    "schemas/physical-partition-result-v1.schema.json",
)

OPAQUE_SUFFIXES = {
    ".a",
    ".bin",
    ".bit",
    ".dcp",
    ".dll",
    ".dylib",
    ".exe",
    ".ncd",
    ".ngc",
    ".o",
    ".obj",
    ".pdi",
    ".pyd",
    ".so",
    ".xpr",
    ".xsa",
}

OPAQUE_MAGICS = (
    b"\x7fELF",
    b"!<arch>\n",
    b"MZ",
    b"\xca\xfe\xba\xbe",
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",
)
GIT_LFS_POINTER = b"version https://git-lfs.github.com/spec/v1\n"

SOURCE_MANIFEST = "SOURCE_MANIFEST.json"
OPEN_SOURCE_COMPONENTS = "OPEN_SOURCE_COMPONENTS.json"
OPEN_SOURCE_COMPONENTS_DOCUMENT = "OPEN_SOURCE_COMPONENTS.md"
PINNED_ARCHITECTURE_SOURCES = (
    "resources/architectures/vtr/flagship-k6-n10-40nm.json",
)
RTL_CATALOG = "benchmarks/rtl_catalog.json"
ALLOWED_INTEGRATIONS = {
    "default-in-tree-build",
    "in-tree-python",
    "in-tree-python-baseline",
    "source-present-runner-pending",
    "source-present-ultrascale-plus-integration-pending",
}


def _load_json(
    repo_root: Path, relative_path: str, errors: list[str]
) -> dict:
    path = repo_root / relative_path
    if not path.is_file():
        errors.append(f"required JSON inventory is missing: {relative_path}")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"{relative_path} is not valid JSON: {error}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{relative_path} must contain a JSON object")
        return {}
    return value


def _audit_open_source_provenance(
    repo_root: Path, errors: list[str]
) -> None:
    inventory = _load_json(repo_root, OPEN_SOURCE_COMPONENTS, errors)
    if (
        inventory.get("schema")
        != "emuflow.open-source-components/v1"
    ):
        errors.append(
            "open-source component inventory has an unsupported or missing "
            "schema"
        )
    if not (repo_root / OPEN_SOURCE_COMPONENTS_DOCUMENT).is_file():
        errors.append(
            "human-readable open-source provenance is missing: "
            f"{OPEN_SOURCE_COMPONENTS_DOCUMENT}"
        )

    vendored_ids: set[str] = set()
    vendored_paths: set[str] = set()
    for component in inventory.get("vendored_sources", []):
        if not isinstance(component, dict):
            errors.append("vendored provenance entry is not an object")
            continue
        component_id = component.get("id")
        relative_path = component.get("path")
        upstream = component.get("upstream")
        revision = component.get("version_or_revision")
        license_name = component.get("license")
        if not isinstance(component_id, str) or not component_id:
            errors.append("vendored provenance entry has no stable id")
            continue
        if component_id in vendored_ids:
            errors.append(f"duplicate vendored provenance id: {component_id}")
        vendored_ids.add(component_id)
        if not isinstance(relative_path, str) or not relative_path:
            errors.append(f"{component_id}: no vendored source path")
        else:
            if relative_path in vendored_paths:
                errors.append(
                    f"{component_id}: duplicate vendored source path: "
                    f"{relative_path}"
                )
            vendored_paths.add(relative_path)
            if not (repo_root / relative_path).exists():
                errors.append(
                    f"{component_id}: vendored source path is missing: "
                    f"{relative_path}"
                )
        if not isinstance(upstream, str) or not upstream.startswith("https://"):
            errors.append(f"{component_id}: no HTTPS upstream source link")
        if not isinstance(revision, str) or not revision:
            errors.append(f"{component_id}: no source version or revision")
        if not isinstance(license_name, str) or not license_name:
            errors.append(f"{component_id}: no license attribution")

    engines_root = repo_root / "engines"
    if engines_root.is_dir():
        for engine in engines_root.iterdir():
            if engine.is_dir():
                relative_path = engine.relative_to(repo_root).as_posix()
                if relative_path not in vendored_paths:
                    errors.append(
                        "engine has no central provenance entry: "
                        f"{relative_path}"
                    )

    for category in ("external_dependencies", "ci_actions"):
        ids: set[str] = set()
        for component in inventory.get(category, []):
            if not isinstance(component, dict):
                errors.append(f"{category} provenance entry is not an object")
                continue
            component_id = component.get("id")
            upstream = component.get("upstream")
            if not isinstance(component_id, str) or not component_id:
                errors.append(f"{category} provenance entry has no stable id")
                continue
            if component_id in ids:
                errors.append(
                    f"duplicate {category} provenance id: {component_id}"
                )
            ids.add(component_id)
            if (
                not isinstance(upstream, str)
                or not upstream.startswith("https://")
            ):
                errors.append(
                    f"{component_id}: no HTTPS upstream source link"
                )

    catalog_path = inventory.get("benchmark_catalog")
    if catalog_path != RTL_CATALOG:
        errors.append(
            "open-source inventory must reference the canonical RTL catalog"
        )
    catalog = _load_json(repo_root, RTL_CATALOG, errors)
    if catalog.get("schema") != "emuflow.rtl-catalog/v1":
        errors.append("RTL benchmark catalog has an unsupported schema")
    architecture_sources = inventory.get("architecture_sources")
    if architecture_sources != list(PINNED_ARCHITECTURE_SOURCES):
        errors.append(
            "open-source inventory must list the pinned architecture sources"
        )
    for relative_path in PINNED_ARCHITECTURE_SOURCES:
        source = _load_json(repo_root, relative_path, errors)
        if (
            source.get("schema")
            != "emuflow.pinned-architecture-source/v1"
        ):
            errors.append(f"{relative_path}: unsupported source schema")
        if not str(source.get("upstream", "")).startswith("https://"):
            errors.append(f"{relative_path}: no HTTPS upstream source link")
        if not source.get("commit"):
            errors.append(f"{relative_path}: no pinned source revision")
        if not source.get("sha256"):
            errors.append(f"{relative_path}: no pinned source SHA-256")
        if not source.get("license"):
            errors.append(f"{relative_path}: no source license attribution")
    benchmark_ids: set[str] = set()
    for design in catalog.get("designs", []):
        if not isinstance(design, dict):
            errors.append("RTL benchmark catalog entry is not an object")
            continue
        design_id = design.get("id")
        if not isinstance(design_id, str) or not design_id:
            errors.append("RTL benchmark catalog entry has no stable id")
            continue
        if design_id in benchmark_ids:
            errors.append(f"duplicate RTL benchmark id: {design_id}")
        benchmark_ids.add(design_id)
        repository = design.get("repository")
        if (
            not isinstance(repository, str)
            or not repository.startswith("https://")
        ):
            errors.append(f"{design_id}: no HTTPS benchmark source link")
        if not design.get("revision"):
            errors.append(f"{design_id}: no pinned benchmark revision")
        if not design.get("license"):
            errors.append(f"{design_id}: no benchmark license attribution")


def _opaque_format(path: Path) -> Optional[str]:
    if path.suffix.lower() in OPAQUE_SUFFIXES:
        return "opaque build or bitstream suffix"
    try:
        with path.open("rb") as stream:
            prefix = stream.read(128)
    except OSError as error:
        return f"unreadable file: {error}"
    if prefix.startswith(GIT_LFS_POINTER):
        return "Git LFS pointer"
    if any(prefix.startswith(magic) for magic in OPAQUE_MAGICS):
        return "opaque executable/library format"
    return None


def audit(repo_root: Path) -> list[str]:
    errors: list[str] = []
    manifest_path = repo_root / SOURCE_MANIFEST
    if not manifest_path.is_file():
        errors.append(f"source manifest is missing: {SOURCE_MANIFEST}")
        manifest = {}
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"source manifest is not valid JSON: {error}")
            manifest = {}
    _audit_open_source_provenance(repo_root, errors)
    if manifest.get("schema") != "emuflow.source-manifest/v1":
        errors.append("source manifest has an unsupported or missing schema")
    component_ids: set[str] = set()
    for component in manifest.get("components", []):
        component_id = component.get("id")
        if not isinstance(component_id, str) or not component_id:
            errors.append("source manifest component has no stable id")
            continue
        if component_id in component_ids:
            errors.append(f"duplicate source manifest component: {component_id}")
        component_ids.add(component_id)
        integration = component.get("integration")
        if integration not in ALLOWED_INTEGRATIONS:
            errors.append(
                f"{component_id}: unsupported integration state {integration!r}"
            )
        source_paths = component.get("source_paths")
        if not isinstance(source_paths, list) or not source_paths:
            errors.append(f"{component_id}: no implementation source paths")
            continue
        for relative_path in source_paths:
            if not isinstance(relative_path, str):
                errors.append(f"{component_id}: non-string source path")
                continue
            source_path = repo_root / relative_path
            if not source_path.exists():
                errors.append(
                    f"{component_id}: implementation source is missing: "
                    f"{relative_path}"
                )
    for blocker in manifest.get("open_path_blockers", []):
        if blocker.get("status") == "complete":
            errors.append(
                f"completed blocker must be moved into components: "
                f"{blocker.get('id', '<unknown>')}"
            )

    readme_path = repo_root / "README.md"
    if readme_path.is_file():
        readme = readme_path.read_text(encoding="utf-8")
        try:
            quick_start = readme.split("## Quick start", 1)[1].split(
                "\n## ", 1
            )[0]
        except IndexError:
            errors.append("README has no bounded Quick start section")
        else:
            for disallowed in (
                "pip install",
                "python -m emuflow",
                "python3 -m emuflow",
            ):
                if disallowed in quick_start:
                    errors.append(
                        "README Quick start bypasses the root-build CLI with "
                        f"{disallowed!r}"
                    )
            if "build/native/install/bin" not in quick_start:
                errors.append(
                    "README Quick start does not expose the root-build CLI"
                )

    for relative_file in REQUIRED_FIRST_PARTY_NATIVE_FILES:
        if not (repo_root / relative_file).is_file():
            errors.append(
                f"first-party native source is missing: {relative_file}"
            )
    native_root = repo_root / "src" / "native"
    for path in native_root.rglob("*"):
        if path.is_file() and (reason := _opaque_format(path)) is not None:
            errors.append(
                "first-party native tree contains a non-source artifact "
                f"({reason}): {path.relative_to(repo_root)}"
            )
    engines = repo_root / "engines"
    for component, relative_files in REQUIRED_SOURCE_FILES.items():
        component_root = engines / component
        for relative_file in relative_files:
            path = component_root / relative_file
            if not path.is_file():
                errors.append(
                    f"{component}: required source file is missing: {relative_file}"
                )

        for path in component_root.rglob("*"):
            if path.is_file() and (reason := _opaque_format(path)) is not None:
                errors.append(
                    f"{component}: non-source artifact is present ({reason}): "
                    f"{path.relative_to(repo_root)}"
                )

    try:
        staged = subprocess.run(
            ["git", "-c", "core.quotePath=false", "ls-files", "--stage"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        errors.append(f"could not inspect tracked source entries: {error}")
    else:
        for line in staged.splitlines():
            mode, _object_id, _stage_and_path = line.split(maxsplit=2)
            relative_path = _stage_and_path.split("\t", 1)[-1]
            tracked_path = repo_root / relative_path
            if mode == "160000":
                errors.append(
                    "git submodules are not allowed in the source-complete "
                    f"tree: {relative_path}"
                )
                continue
            if mode == "120000":
                try:
                    target = tracked_path.resolve(strict=False)
                    target.relative_to(repo_root)
                except (OSError, ValueError):
                    errors.append(
                        "tracked symlink escapes the source-complete tree: "
                        f"{relative_path}"
                    )
                continue
            if not tracked_path.is_file():
                errors.append(f"tracked file is missing: {relative_path}")
                continue
            if (reason := _opaque_format(tracked_path)) is not None:
                errors.append(
                    f"tracked non-source artifact is not allowed ({reason}): "
                    f"{relative_path}"
                )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo_root", type=Path)
    args = parser.parse_args()

    errors = audit(args.repo_root.resolve())
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(
        "source audit passed: providers are editable source and provenance "
        "is complete"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
