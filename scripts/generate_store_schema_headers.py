#!/usr/bin/env python3
"""Embed the installed SQL schema into native builds without a second DDL copy."""
import argparse
from pathlib import Path


def generate(directory):
    root = Path(__file__).resolve().parents[1]
    text = (root / "src/native/emuflow_core/assignment_schema.hpp.in").read_text(
        encoding="utf-8"
    )
    for name in ("core", "cuts"):
        ddl = (
            root / ("src/emuflow/data/schema/phase3_assignment_" + name + ".sql")
        ).read_text(encoding="utf-8")
        if ')EMUFLOW_SQL"' in ddl:
            raise ValueError("SQL schema contains the native raw-string delimiter")
        text = text.replace("@EMUFLOW_ASSIGNMENT_" + name + "_DDL@", ddl)
    if "@EMUFLOW_ASSIGNMENT_" in text:
        raise ValueError("native schema template has an unresolved field")
    output = Path(directory) / "assignment_schema.hpp"
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.exists() or output.read_text(encoding="utf-8") != text:
        output.write_text(text, encoding="utf-8")

    schedule = "#pragma once\nnamespace emuflow::core {\n"
    for symbol, filename in (
        ("kScheduleStoreDDL", "phase5_schedule.sql"),
        ("kScheduleStoreIndexDDL", "phase5_schedule_indexes.sql"),
    ):
        ddl = (root / "src/emuflow/data/schema" / filename).read_text(encoding="utf-8")
        if ')EMUFLOW_SQL"' in ddl:
            raise ValueError("SQL schema contains the native raw-string delimiter")
        schedule += f'inline constexpr const char* {symbol} = R"EMUFLOW_SQL({ddl})EMUFLOW_SQL";\n'
    schedule += "}\n"
    output = Path(directory) / "schedule_store_schema.hpp"
    if not output.exists() or output.read_text(encoding="utf-8") != schedule:
        output.write_text(schedule, encoding="utf-8")

    ddl = (root / "src/emuflow/data/schema/phase3_clusters.sql").read_text(
        encoding="utf-8"
    )
    if ')EMUFLOW_SQL"' in ddl:
        raise ValueError("SQL schema contains the native raw-string delimiter")
    catalog = (
        '#pragma once\nnamespace emuflow::core {\ninline constexpr const char* kClusterCatalogDDL = R"EMUFLOW_SQL('
        + ddl
        + ')EMUFLOW_SQL";\n}\n'
    )
    output = Path(directory) / "cluster_catalog_schema.hpp"
    if not output.exists() or output.read_text(encoding="utf-8") != catalog:
        output.write_text(catalog, encoding="utf-8")

    ddl = (root / "src/emuflow/data/schema/emuir_compact.sql").read_text(encoding="utf-8")
    if ')EMUFLOW_SQL"' in ddl:
        raise ValueError("SQL schema contains the native raw-string delimiter")
    compact = '#pragma once\nnamespace emuflow::core {\ninline constexpr const char* kEmuirCompactDDL = R"EMUFLOW_SQL(' + ddl + ')EMUFLOW_SQL";\n}\n'
    output = Path(directory) / "emuir_compact_schema.hpp"
    if not output.exists() or output.read_text(encoding="utf-8") != compact:
        output.write_text(compact, encoding="utf-8")

    ddl = (root / "src/emuflow/data/schema/sta_paths.sql").read_text(encoding="utf-8")
    if ')EMUFLOW_SQL"' in ddl:
        raise ValueError("SQL schema contains the native raw-string delimiter")
    paths = '#pragma once\nnamespace emuflow::core {\ninline constexpr const char* kStaPathStoreDDL = R"EMUFLOW_SQL(' + ddl + ')EMUFLOW_SQL";\n}\n'
    output = Path(directory) / "sta_path_schema.hpp"
    if not output.exists() or output.read_text(encoding="utf-8") != paths:
        output.write_text(paths, encoding="utf-8")

    ddl = (root / "src/emuflow/data/schema/sta_net_weights.sql").read_text(encoding="utf-8")
    if ')EMUFLOW_SQL"' in ddl:
        raise ValueError("SQL schema contains the native raw-string delimiter")
    weights = '#pragma once\nnamespace emuflow::core {\ninline constexpr const char* kStaNetWeightStoreDDL = R"EMUFLOW_SQL(' + ddl + ')EMUFLOW_SQL";\n}\n'
    output = Path(directory) / "sta_weight_schema.hpp"
    if not output.exists() or output.read_text(encoding="utf-8") != weights:
        output.write_text(weights, encoding="utf-8")

    ddl = (root / "src/emuflow/data/schema/yosys_hierarchy.sql").read_text(encoding="utf-8")
    hierarchy = '#pragma once\nnamespace emuflow::core {\ninline constexpr const char* kYosysHierarchyDDL = R"EMUFLOW_SQL(' + ddl + ')EMUFLOW_SQL";\n}\n'
    output = Path(directory) / "yosys_hierarchy_schema.hpp"
    if not output.exists() or output.read_text(encoding="utf-8") != hierarchy:
        output.write_text(hierarchy, encoding="utf-8")

    split = "#pragma once\nnamespace emuflow::core {\n"
    for symbol, filename in (
        ("kSplitOverlayDDL", "phase6_split_overlay.sql"),
        ("kSplitProjectionDDL", "phase6_split_projection.sql"),
        ("kSplitOverlayBuildSQL", "phase6_split_build.sql"),
    ):
        ddl = (root / "src/emuflow/data/schema" / filename).read_text(encoding="utf-8")
        if ')EMUFLOW_SQL"' in ddl:
            raise ValueError("SQL schema contains the native raw-string delimiter")
        split += f'inline constexpr const char* {symbol} = R"EMUFLOW_SQL({ddl})EMUFLOW_SQL";\n'
    split += "}\n"
    output = Path(directory) / "split_overlay_schema.hpp"
    if not output.exists() or output.read_text(encoding="utf-8") != split:
        output.write_text(split, encoding="utf-8")

    events = "#pragma once\nnamespace emuflow::core {\n"
    for symbol, filename in (
        ("kGlobalEventsDDL", "global_events.sql"),
        ("kGlobalEventIndexDDL", "global_event_index.sql"),
        ("kGlobalMeasurementsDDL", "global_measurements.sql"),
        ("kGlobalQorDDL", "qor_report.sql"),
        ("kBoundaryIdentityDDL", "boundary_identity.sql"),
        ("kBoundaryTimingDDL", "boundary_timing.sql"),
        ("kLogicSegmentTimingDDL", "logic_segment_timing.sql"),
        ("kLocalPathTimingDDL", "local_path_timing.sql"),
        ("kLogicSegmentIdentityDDL", "logic_segment_identity.sql"),
    ):
        ddl = (root / "src/emuflow/data/schema" / filename).read_text(encoding="utf-8")
        if ')EMUFLOW_SQL"' in ddl:
            raise ValueError("SQL schema contains the native raw-string delimiter")
        events += f'inline constexpr const char* {symbol} = R"EMUFLOW_SQL({ddl})EMUFLOW_SQL";\n'
    tcl = (root / "src/emuflow/data/schema/global_event_analyze.tcl").read_text(encoding="utf-8")
    if ')EMUFLOW_SQL"' in tcl:
        raise ValueError("Tcl template contains the native raw-string delimiter")
    events += 'inline constexpr const char* kGlobalEventAnalyzeTcl = R"EMUFLOW_SQL(' + tcl + ')EMUFLOW_SQL";\n'
    events += "}\n"
    output = Path(directory) / "global_event_schema.hpp"
    if not output.exists() or output.read_text(encoding="utf-8") != events:
        output.write_text(events, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    generate(parser.parse_args().directory)
