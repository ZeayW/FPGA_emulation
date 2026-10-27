"""Differential tests of the production native ScheduleStore owner."""

from tests.schedule_store_fixture import write_tdm_schedule_storage
from tests.phase3_store_fixture import write_current_assignment_fixture
import json
import sqlite3
import struct
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.native_tools import native_file_generation
from emuflow.tdm_schedule_storage import (read_tdm_schedule, tdm_schedule_store_path)
from tests.native_build import emuflow_native


def test_unified_typed_route_producer_selects_native_store(tmp_path,monkeypatch):
    from tests.test_combinational_cut import StaticExactCombinationalCutPartitionTest
    from emuflow.system_route_storage import read_system_routes
    from tests.native_route_seal_fixture import write_route_store_fixture
    from tests.schedule_reference import build_tdm_schedule
    case=StaticExactCombinationalCutPartitionTest();case.setUp()
    _,assignment=case._exact_artifacts(dependent_return=True)
    routes=case._exact_routes(assignment)
    routes["semantic_contract"]={**routes["semantic_contract"],"indexed":True}
    reference=build_tdm_schedule(routes,case.platform)
    route_path=tmp_path/"routes.json";write_route_store_fixture(route_path,routes)
    native_bin = tmp_path/"install"/"bin"
    native_bin.mkdir(parents=True)
    (native_bin/"emuflow-native").symlink_to(emuflow_native())
    monkeypatch.setenv("EMUFLOW_NATIVE_ROOT",str(native_bin.parent))
    # Exercise the real report-last Phase 5 boundary and independent reader,
    # rather than qualifying only the standalone store operation.
    from emuflow.phase5 import run_phase5, validate_phase5
    from tests.test_combinational_cut import PLATFORM_PATH
    persisted = case._persisted_routes(case._exact_routes(assignment))
    write_route_store_fixture(route_path,persisted)
    assignment_path=tmp_path/"assignment.json"
    write_current_assignment_fixture(assignment_path, case.ir, assignment)
    output=tmp_path/"phase5"
    # Production must select the complete native producer from its indexed contract.
    with patch("tests.schedule_store_fixture._publish_fixture_records",side_effect=AssertionError("Python publisher used")), \
         patch("emuflow.tdm._indexed_certificate",side_effect=AssertionError("Python producer certificate used")), \
         patch("emuflow.tdm._baseline_ratio_hops",side_effect=AssertionError("Python hop map rebuilt before validation")):
        report=run_phase5(route_path,PLATFORM_PATH,output,assignment_path)
    assert report["status"]=="pass"
    assert "simulation" not in report
    assert "transport_manifest" not in report["artifacts"]
    assert report["schedule_storage"]["implementation_id"]=="native-exact-schedule-store-v1"
    assert report["schedule_storage"]["producer_implementation_id"]=="native-exact-phase5-producer-v1"
    assert validate_phase5(route_path,PLATFORM_PATH,output/"schedule.json",assignment_path=assignment_path)["status"]=="pass"

    old_report=(output/"phase5_report.json").read_bytes()
    old_manifest=(output/"schedule.json").read_bytes()
    from tests.phase5_stage_fixture import interrupt_native_phase5
    rejected = interrupt_native_phase5(route_path, PLATFORM_PATH, assignment_path, output, "scheduled", "corrupt")
    assert rejected.returncode == 71, rejected.stderr
    assert "schedule" in rejected.stderr.lower()
    assert (output/"phase5_report.json").read_bytes()==old_report
    assert (output/"schedule.json").read_bytes()==old_manifest
    assert not list(output.glob(".phase5-generation-*"))
