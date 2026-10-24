"""Differential tests of the production native ScheduleStore owner."""
import json
import sqlite3
import struct
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.native_tools import native_file_generation
from emuflow.tdm_schedule_storage import (
    FULLY_TYPED_DDL, TYPED_INDEX_DDL, read_tdm_schedule,
    tdm_schedule_store_path, write_tdm_schedule_storage,
)
from tests.native_build import emuflow_native


def test_unified_typed_route_producer_selects_native_store(tmp_path,monkeypatch):
    from tests.test_combinational_cut import StaticExactCombinationalCutPartitionTest
    from emuflow.system_route_storage import write_system_route_storage,read_system_routes
    from emuflow.tdm import build_tdm_schedule
    case=StaticExactCombinationalCutPartitionTest();case.setUp()
    _,assignment=case._exact_artifacts(dependent_return=True)
    routes=case._exact_routes(assignment)
    routes["semantic_contract"]={**routes["semantic_contract"],"indexed":True}
    reference=build_tdm_schedule(routes,case.platform)
    route_path=tmp_path/"routes.json";write_system_route_storage(route_path,routes)
    native_bin = tmp_path/"install"/"bin"
    native_bin.mkdir(parents=True)
    (native_bin/"emuflow-native").symlink_to(emuflow_native())
    monkeypatch.setenv("EMUFLOW_NATIVE_ROOT",str(native_bin.parent))
    # Exercise the real report-last Phase 5 boundary and independent reader,
    # rather than qualifying only the standalone store operation.
    from emuflow.phase5 import run_phase5, validate_phase5
    from tests.test_combinational_cut import PLATFORM_PATH
    persisted = case._persisted_routes(case._exact_routes(assignment))
    write_system_route_storage(route_path,persisted)
    assignment_path=tmp_path/"assignment.json"
    assignment_path.write_text(json.dumps(assignment),encoding="utf-8")
    output=tmp_path/"phase5"
    # Production must select the complete native producer from its indexed contract.
    with patch("emuflow.tdm_schedule_storage._publish_logical_schedule",side_effect=AssertionError("Python publisher used")), \
         patch("emuflow.phase5.build_tdm_schedule",side_effect=AssertionError("Python schedule/result import used")), \
         patch("emuflow.tdm._indexed_certificate",side_effect=AssertionError("Python producer certificate used")), \
         patch("emuflow.tdm._baseline_ratio_hops",side_effect=AssertionError("Python hop map rebuilt before validation")), \
         patch("emuflow.phase5.simulate_tdm_schedule",side_effect=AssertionError("graph simulation export in production")), \
         patch("emuflow.phase5.build_transport_manifest",side_effect=AssertionError("duplicate transport JSON in production")):
        report=run_phase5(route_path,PLATFORM_PATH,output,assignment_path,managed_storage=False)
    assert report["status"]=="pass"
    assert "simulation" not in report
    assert "transport_manifest" not in report["artifacts"]
    assert report["schedule_storage"]["implementation_id"]=="native-exact-schedule-store-v1"
    assert report["schedule_storage"]["producer_implementation_id"]=="native-exact-phase5-producer-v1"
    assert validate_phase5(route_path,PLATFORM_PATH,output/"schedule.json",assignment_path=assignment_path)["status"]=="pass"

    old_report=(output/"phase5_report.json").read_bytes()
    old_manifest=(output/"schedule.json").read_bytes()
    with patch("emuflow.tdm._validate_indexed_exact_tdm_schedule",side_effect=ValidationError("independent validator rejected")):
        with pytest.raises(ValidationError,match="independent validator rejected"):
            run_phase5(route_path,PLATFORM_PATH,output,assignment_path,managed_storage=True)
    assert (output/"phase5_report.json").read_bytes()==old_report
    assert (output/"schedule.json").read_bytes()==old_manifest
    assert not list(output.glob(".phase5-generation-*"))
