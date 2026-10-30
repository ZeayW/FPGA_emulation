"""Independent complete population checks; geometry/topology remain separate."""

import json
from pathlib import Path
import sqlite3
import subprocess
from contextlib import closing
from unittest.mock import patch

import pytest

from emuflow_tools.physical import physical_cells_native
from emuflow.common.errors import ValidationError
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import native_file_generation
from emuflow_tools.physical.xilinx_netlist import open_xilinx_mapped_index
from tests.native_build import emuflow_native
from tests.test_rwroute_native import fixture


def prepare(tmp_path):
    values = fixture(tmp_path)
    mapped, physical = values[-2:]
    with closing(sqlite3.connect(physical)) as db, db:
        row = db.execute(
            "SELECT value_json FROM document WHERE name='packed'"
        ).fetchone()
        document = json.loads(row[0])
        document["source"]["mapped_json_sha256"] = "1" * 64
        db.execute(
            "UPDATE document SET value_json=? WHERE name='packed'",
            (json.dumps(document),),
        )
    return values, mapped, physical


def validate(mapped, physical):
    with patch.object(
        physical_cells_native,
        "resolve_native_executable",
        return_value=str(emuflow_native()),
    ):
        return physical_cells_native.validate_native_physical_cells(
            mapped, physical, mapped_sha256="1" * 64, top="top"
        )


def test_native_checks_all_typed_pins_without_python_record_reconstruction(tmp_path):
    values, mapped, physical = prepare(tmp_path)
    with (
        patch(
            "emuflow_tools.physical.xilinx_netlist._IndexedCells.__getitem__",
            side_effect=AssertionError("Python cell lookup"),
        ),
        patch(
            "emuflow_tools.physical.xilinx_netlist._IndexedCells.items",
            side_effect=AssertionError("Python circuit scan"),
        ),
    ):
        result = validate(mapped, physical)
    cells = values[0]["modules"]["top"]["cells"]
    assert result["cells"] == len(cells)
    assert result["assignments"] == len(cells) - 1 and result["constants"] == 1
    assert result["pins"] == sum(
        len(bits) for cell in cells.values() for bits in cell["connections"].values()
    )
    assert result["driver_uniqueness"] == "complete-mapped-output-population"
    assert not list(tmp_path.glob(".physical-cells-*"))


@pytest.mark.parametrize(
    ("owner", "mutation", "message"),
    [
        (
            "physical",
            "UPDATE assignment SET instance='substituted' WHERE instance='a_lut'",
            "does not match source",
        ),
        (
            "physical",
            "DELETE FROM assignment WHERE instance='a_lut'",
            "ownership is incomplete",
        ),
        (
            "physical",
            "UPDATE assignment SET cell_type='LUT2' WHERE instance='a_lut'",
            "does not match source",
        ),
        ("physical", "UPDATE constant SET name='a_lut'", "constants are invalid"),
        ("physical", "DELETE FROM constant", "constants are incomplete"),
        (
            "physical",
            "UPDATE assignment SET cluster_ordinal=-1 WHERE instance='a_lut'",
            "orphan assignments",
        ),
        (
            "physical",
            "UPDATE assignment SET cluster_ordinal=(SELECT cluster_ordinal FROM assignment WHERE instance='z_ff'),packed_ordinal=99 WHERE instance='enable'",
            "reuses a BEL",
        ),
        (
            "physical",
            "UPDATE manifest SET assignments=assignments+1",
            "manifest coverage",
        ),
        (
            "mapped",
            "UPDATE metadata SET value='emuflow.xilinx-mapped-index/v1' WHERE key='schema'",
            "schema",
        ),
        (
            "mapped",
            "UPDATE cell_pins SET net_bit=777 WHERE cell_id IN ('a_lut','ground') AND direction=1",
            "multiple drivers",
        ),
        ("mapped", "UPDATE cells SET type='LUT3' WHERE id='a_lut'", "type projection"),
        (
            "mapped",
            "UPDATE cells SET record_json='[]' WHERE id='a_lut'",
            "type projection",
        ),
        (
            "mapped",
            "UPDATE cell_pins SET net_bit=8 WHERE cell_id='a_lut' AND direction=1",
            "multiple drivers",
        ),
        (
            "mapped",
            "UPDATE cell_pins SET cell_seq=9999 WHERE cell_id='a_lut'",
            "pin row is invalid",
        ),
        (
            "mapped",
            "DELETE FROM cell_pins WHERE cell_id='ram' AND port='DIADI' AND port_bit=1",
            "port is incomplete",
        ),
        (
            "mapped",
            "UPDATE cell_pins SET direction=2 WHERE cell_id='ram' AND port='DIADI' AND port_bit=1",
            "conflicting directions",
        ),
        (
            "mapped",
            "UPDATE cell_pins SET port_width=4 WHERE cell_id='ram' AND port='DIADI'",
            "port is incomplete",
        ),
        (
            "mapped",
            "UPDATE cell_pins SET net_bit=NULL WHERE cell_id='a_lut' AND direction=1",
            "pin row is invalid",
        ),
    ],
)
def test_native_population_rejects_corruption_without_changing_input(
    tmp_path, owner, mutation, message
):
    _values, mapped, physical = prepare(tmp_path)
    target = mapped if owner == "mapped" else physical
    with closing(sqlite3.connect(target)) as db, db:
        db.execute(mutation)
    before = physical.read_bytes()
    with pytest.raises(ValidationError, match=message):
        validate(mapped, physical)
    assert physical.read_bytes() == before
    assert not list(tmp_path.glob(".physical-cells-*"))


@pytest.mark.parametrize(
    "corruption", ["version", "crc", "truncated", "generation", "source", "alias"]
)
def test_native_request_rejection_preserves_previous_summary(tmp_path, corruption):
    _values, mapped, physical = prepare(tmp_path)
    request = tmp_path / "request.bin"
    writer = CheckedRequestWriter(request)
    writer.put("QI", 0x31564350464D5545, 2 if corruption == "version" else 1)
    for source in (mapped, physical):
        generation = list(native_file_generation(source))
        if corruption == "generation" and source == mapped:
            generation[2] += 1
        writer.put("3Q4q", *generation)
    writer.text("0" * 64 if corruption == "source" else "1" * 64)
    writer.text("top")
    writer.finish()
    if corruption in {"crc", "truncated"}:
        data = bytearray(request.read_bytes())
        if corruption == "crc":
            data[-1] ^= 1
        else:
            data = data[:-1]
        request.write_bytes(data)
    summary = physical if corruption == "alias" else tmp_path / "summary.json"
    if corruption != "alias":
        summary.write_text("prior summary")
    before = summary.read_bytes()
    result = subprocess.run(
        [
            str(emuflow_native()),
            "phase7",
            "validate-physical-cells",
            str(mapped),
            str(physical),
            str(request),
            str(summary),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert summary.read_bytes() == before


def test_indexed_family_and_driver_queries_only_decode_the_requested_subgraph(tmp_path):
    values, mapped, _physical = prepare(tmp_path)
    expected = {
        name: value
        for name, value in values[0]["modules"]["top"]["cells"].items()
        if value["type"] in {"DSP48E2", "CARRY8"}
    }
    with open_xilinx_mapped_index(mapped) as view:
        with patch.object(
            type(view.cells), "items", side_effect=AssertionError("whole circuit scan")
        ):
            assert (
                dict(
                    view.cells.items_of_types(
                        expected_value["type"] for expected_value in expected.values()
                    )
                )
                == expected
            )
            assert view.cells.output_drivers({2, 8, 100}) == {2: "a_lut", 8: "z_ff"}


def test_indexed_bridge_uses_native_population_and_keeps_local_control_checks(tmp_path):
    from tests.test_xilinx_openparf_bridge import _native_certificate
    from tests.test_rwroute_native import write_mapped
    from emuflow_tools.physical.handoff import materialize_xilinx_openparf_atomic_contract

    from tests.physical_handoff_native_fixture import prepare_native_handoff
    data=prepare_native_handoff(tmp_path,split_ff_control_sets=True)
    index_path,architecture,certificate=data.mapped,data.architecture,data.certificate
    with (
        open_xilinx_mapped_index(index_path) as indexed,
        patch.object(
            physical_cells_native,
            "resolve_native_executable",
            return_value=str(emuflow_native()),
        ),
        patch.object(
            type(indexed.cells),
            "items",
            side_effect=AssertionError("complete mapped cell scan"),
        ),
        patch.object(
            type(indexed.cells),
            "values",
            side_effect=AssertionError("complete mapped cell scan"),
        ),
    ):
        result = materialize_xilinx_openparf_atomic_contract(
            index_path,
            architecture,
            certificate,
            tmp_path / "physical.sqlite",
            mapped_value=indexed, source_packed_path=data.source,
        )
    assert result["status"] == "pass"
    population = result["validation"]["packing"]["source_population_validation"]
    assert population["assignments"] == result["placed_cells"]
    assert (
        result["runtime_breakdown"]["dataflow"]["physical_contract_validation"]
        == "native-complete-source-and-device-audit-v1"
    )
    assert not list(tmp_path.glob(".*.drivers.*"))


def test_repeated_large_population_never_constructs_a_python_circuit(tmp_path):
    _values, mapped, physical = prepare(tmp_path)
    count = 20_000
    with closing(sqlite3.connect(mapped)) as db, db:
        db.executemany(
            "INSERT INTO cells VALUES (?,?,?,?)",
            (
                (f"repeated-{i}", i + 100, "LUT1", '{"type":"LUT1","parameters":{}}')
                for i in range(count)
            ),
        )
        db.executemany(
            "INSERT INTO cell_pins VALUES (?,?,?,?,?,?,?,?,?)",
            (
                (i + 100, f"repeated-{i}", "O", 0, 1, 1, 0, i + 1_000_000, None)
                for i in range(count)
            ),
        )
    with closing(sqlite3.connect(physical)) as db, db:
        db.executemany(
            "INSERT INTO cluster VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    i + 100,
                    f"repeated-{i}",
                    "slice",
                    '["SLICEL"]',
                    None,
                    None,
                    None,
                    f"SLICE_X99Y{i}",
                    "SLICEL",
                    99,
                    i,
                    0,
                    None,
                )
                for i in range(count)
            ),
        )
        db.executemany(
            "INSERT INTO assignment VALUES (?,?,?,?,?,?,?,?)",
            (
                (
                    i + 100,
                    f"repeated-{i}",
                    "LUT1",
                    "A6LUT",
                    "SLICEL",
                    f"SLICE_X99Y{i}",
                    0,
                    0,
                )
                for i in range(count)
            ),
        )
        db.execute(
            "UPDATE manifest SET assignments=assignments+?,packed_clusters=packed_clusters+?",
            (count, count),
        )
    with patch(
        "emuflow_tools.physical.xilinx_netlist.IndexedXilinxMappedNetlist.__init__",
        side_effect=AssertionError("Python graph"),
    ):
        result = validate(mapped, physical)
    assert result["assignments"] == count + 9


def test_bridge_binds_in_process_index_before_native_check_and_publication(tmp_path):
    from tests.test_xilinx_openparf_bridge import _native_certificate
    from tests.test_rwroute_native import write_mapped
    from emuflow_tools.physical.handoff import materialize_xilinx_openparf_atomic_contract

    from tests.physical_handoff_native_fixture import prepare_native_handoff
    data=prepare_native_handoff(tmp_path,split_ff_control_sets=True)
    index_path,architecture,certificate=data.mapped,data.architecture,data.certificate
    output = tmp_path / "physical.sqlite"
    output.write_bytes(b"prior publication")
    from emuflow_tools.physical import handoff as host
    original = host._write_request

    def change_after_check(*args, **kwargs):
        result = original(*args, **kwargs)
        with closing(sqlite3.connect(index_path)) as db, db:
            db.execute(
                "UPDATE cells SET record_json=json_set(record_json,'$.attributes.changed',1)"
            )
        return result

    with (
        open_xilinx_mapped_index(index_path) as indexed,
        patch.object(
            physical_cells_native,
            "resolve_native_executable",
            return_value=str(emuflow_native()),
        ),
        patch.object(
            host,
            "_write_request",
            side_effect=change_after_check,
        ),
    ):
        with pytest.raises(ValidationError, match="source generation changed"):
            materialize_xilinx_openparf_atomic_contract(
                index_path, architecture, certificate, output, mapped_value=indexed, source_packed_path=data.source
            )
    assert output.read_bytes() == b"prior publication"
    assert not list(tmp_path.glob(".physical.sqlite.*"))


def test_bridge_rejects_a_reader_leased_before_the_index_changed(tmp_path):
    from tests.test_xilinx_openparf_bridge import _native_certificate
    from tests.test_rwroute_native import write_mapped
    from emuflow_tools.physical.handoff import materialize_xilinx_openparf_atomic_contract

    from tests.physical_handoff_native_fixture import prepare_native_handoff
    data=prepare_native_handoff(tmp_path,split_ff_control_sets=True)
    index_path,architecture,certificate=data.mapped,data.architecture,data.certificate
    output = tmp_path / "physical.sqlite"
    output.write_bytes(b"prior publication")
    with open_xilinx_mapped_index(index_path) as indexed:
        with closing(sqlite3.connect(index_path)) as db, db:
            db.execute(
                "UPDATE cells SET record_json=json_set(record_json,'$.attributes.changed',1)"
            )
        with pytest.raises(ValidationError, match="source generation changed"):
            materialize_xilinx_openparf_atomic_contract(
                index_path, architecture, certificate, output, mapped_value=indexed, source_packed_path=data.source
            )
    assert output.read_bytes() == b"prior publication"
    assert not list(tmp_path.glob(".physical.sqlite.*"))
