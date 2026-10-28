"""Physical producer/validator contracts against original source generations."""

import hashlib
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow.frontend.yosys import import_yosys_json
from emuflow.physical.xilinx_netlist import emit_xilinx_mapped_index
from emuflow.physical.rapidwright.xilinx_rwroute import validate_xilinx_route_db
from tests.native_build import emuflow_native


def stream_route(root):
    from tests.test_xilinx_rwroute import XilinxRWRouteTest

    return XilinxRWRouteTest()._stream_route(root)


@pytest.fixture(autouse=True)
def native_runtime(monkeypatch):
    monkeypatch.setenv("EMUFLOW_NATIVE_ROOT", str(emuflow_native().parent.parent))


def mapped_source(root):
    from tests.test_xilinx_netlist import XilinxMappedNetlistTests

    raw, compact = root / "input.json", root / "input.emuir.sqlite"
    raw.write_text(json.dumps(XilinxMappedNetlistTests._fixture()))
    import_yosys_json(raw, clocks=("clk",)).save(compact)
    return compact


def test_mapped_owner_never_imports_python_graph_or_sql_publisher(tmp_path):
    import emuflow.physical.xilinx_netlist as reader

    source = mapped_source(tmp_path)
    assert not hasattr(reader, "_emit_compact_xilinx_mapped_index")
    assert not hasattr(reader, "_emit_native_xilinx_mapped_index_sql")
    assert not hasattr(reader, "_lower_instance")
    with patch(
        "emuflow.ir.emuir_compact.compact_instances",
        side_effect=AssertionError("Python cells"),
    ):
        result = emit_xilinx_mapped_index(source, tmp_path / "mapped.sqlite")
    assert result["implementation_id"] == "native-xilinx-mapped-index-v1"
    assert result["cells"] == 2


@pytest.mark.parametrize("failure", ["generation", "request", "wal", "payload"])
def test_mapped_native_rejection_preserves_prior_published_owner(tmp_path, failure):
    from emuflow.physical import mapped_index_native as host

    source = mapped_source(tmp_path)
    output = tmp_path / "mapped.sqlite"
    emit_xilinx_mapped_index(source, output)
    original = output.read_bytes()
    write = host._write_mapped_request

    def reject(path, sources):
        write(path, sources)
        if failure == "generation":
            source.chmod(source.stat().st_mode ^ 0o100)
        elif failure == "request":
            path.write_bytes(path.read_bytes()[:-1])

    if failure == "wal":
        Path(str(source) + "-wal").write_bytes(b"active writer")
    elif failure == "payload":
        with sqlite3.connect(source) as db:
            db.execute("UPDATE cell_records SET constants_seq=999999")
    with (
        patch.object(host, "_write_mapped_request", reject),
        pytest.raises(ValidationError),
    ):
        emit_xilinx_mapped_index(source, output)
    assert output.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_mapped_report_must_not_alias_any_original_owner(tmp_path):
    source = mapped_source(tmp_path)
    prior = source.read_bytes()
    with pytest.raises(ValidationError):
        emit_xilinx_mapped_index(source, tmp_path / "mapped.sqlite", source)
    assert source.read_bytes() == prior
    assert not (tmp_path / "mapped.sqlite").exists()


@pytest.mark.parametrize(
    "corruption", ["cycle", "reverse", "cross-net-pip", "unbound-pip", "generation"]
)
def test_native_route_rejects_corruption_without_promoting_or_mutating_evidence(
    tmp_path, corruption
):
    from emuflow.physical.rapidwright import route_validation as host

    route, nets, pips = stream_route(tmp_path)
    value = json.loads(route.read_text())
    if corruption in {"cycle", "reverse"}:
        rows = [json.loads(line) for line in pips.read_text().splitlines()]
        # Leave the sink C unreachable; reversed flags cannot reverse the
        # explicitly certified start/end adjacency.
        if corruption == "cycle":
            rows[-1][4:6] = ["B", "A"]
        else:
            rows[-1][4:6] = ["C", "A"]
            rows[-1][7] = True
        pips.write_text("".join(json.dumps(r) + "\n" for r in rows))
    elif corruption == "cross-net-pip":
        records = [json.loads(line) for line in nets.read_text().splitlines()]
        records.append(dict(records[0], net="other"))
        nets.write_text("".join(json.dumps(r) + "\n" for r in records))
        rows = [json.loads(line) for line in pips.read_text().splitlines()]
        rows += [["other", *r[1:]] for r in rows]
        # Physical PIP ownership is independent of direction and net labels.
        rows[-1][2], rows[-1][3] = rows[-1][3], rows[-1][2]
        pips.write_text("".join(json.dumps(r) + "\n" for r in rows))
    elif corruption == "unbound-pip":
        pips.write_text(pips.read_text() + pips.read_text().splitlines()[0] + "\n")
    for name, path in [("nets", nets), ("pips", pips)]:
        value["payloads"][name].update(
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            records=len(path.read_text().splitlines()),
        )
    route.write_text(json.dumps(value))
    original = {p: p.read_bytes() for p in (route, nets, pips)}
    write = host._write_route_request

    def mutate(path, sources, config):
        write(path, sources, config)
        if corruption == "generation":
            route.chmod(route.stat().st_mode ^ 0o100)

    with (
        patch.object(host, "_write_route_request", mutate),
        pytest.raises(ValidationError),
    ):
        validate_xilinx_route_db(route)
    assert all(p.read_bytes() == b for p, b in original.items())


def test_route_validation_uses_no_python_route_or_pip_loader(tmp_path):
    import emuflow.physical.rapidwright.xilinx_rwroute as adapter

    route, _, _ = stream_route(tmp_path)
    assert not hasattr(adapter, "_PipOwnership")
    assert not hasattr(adapter, "iter_xilinx_route_nets")
    with patch('emuflow.common.io.read_json',side_effect=AssertionError('Python route tree')):
        checked = validate_xilinx_route_db(route)
    assert checked["implementation_id"] == "native-xilinx-route-validation-v1"
    assert (checked["nets"], checked["sinks"], checked["pips"]) == (1, 2, 3)


@pytest.mark.parametrize("integer_parameters", [False, True])
def test_native_generic_lut_and_negedge_ff_match_explicit_expected_records(
    tmp_path, integer_parameters
):
    from emuflow.physical.xilinx_netlist import open_xilinx_mapped_index

    source = mapped_source(tmp_path)
    with sqlite3.connect(source) as db:
        parameters = json.dumps(
            {
                "WIDTH": 1 if integer_parameters else "1",
                "LUT": 10 if integer_parameters else "10",
            }
        )
        db.execute("INSERT OR IGNORE INTO payloads(value) VALUES(?)", (parameters,))
        seq = db.execute(
            "SELECT seq FROM payloads WHERE value=?", (parameters,)
        ).fetchone()[0]
        db.execute(
            "UPDATE cell_records SET type='$lut',parameters_seq=? WHERE id='lut'",
            (seq,),
        )
        db.execute("UPDATE pin_names SET name='A' WHERE name='I0'")
        db.execute("UPDATE cell_records SET type='$_DFF_N_' WHERE id='ff'")
    first = emit_xilinx_mapped_index(source, tmp_path / "mapped.sqlite")
    repeated = emit_xilinx_mapped_index(source, tmp_path / "again.sqlite")
    assert first["index_sha256"] == repeated["index_sha256"]
    with open_xilinx_mapped_index(tmp_path / "mapped.sqlite") as mapped:
        lut = mapped.cells["lut"]
        ff = mapped.cells["ff"]
        assert lut["type"] == "LUT1" and lut["parameters"] == {"INIT": "10"}
        assert set(lut["connections"]) == {"I0", "O"}
        assert ff["type"] == "FDRE" and ff["parameters"] == {
            "INIT": "0",
            "IS_C_INVERTED": "1",
        }
        assert ff["connections"]["CE"] == ["1"] and ff["connections"]["R"] == ["0"]


def test_native_mapped_library_resource_contract_is_independently_checked(tmp_path):
    import emuflow.physical.xilinx_primitives as primitives

    source = mapped_source(tmp_path)
    output = tmp_path / "mapped.sqlite"
    emit_xilinx_mapped_index(source, output)
    prior = output.read_bytes()
    library = json.loads(primitives.DEFAULT_XILINX_PRIMITIVE_LIBRARY.read_text())
    library["cells"]["LUT1"]["resources"]["lut"] = 9
    library_path = tmp_path / "library.json"
    library_path.write_text(json.dumps(library))
    with (
        patch.object(primitives, "DEFAULT_XILINX_PRIMITIVE_LIBRARY", library_path),
        pytest.raises(ValidationError, match="resource contract disagrees"),
    ):
        emit_xilinx_mapped_index(source, output)
    assert output.read_bytes() == prior


def test_native_route_does_not_accept_cached_hashes_for_an_unsealed_source(tmp_path):
    from emuflow.physical.xilinx_netlist import open_xilinx_mapped_index

    source = mapped_source(tmp_path)
    mapped = tmp_path / "mapped.sqlite"
    emit_xilinx_mapped_index(source, mapped)
    route, _, _ = stream_route(tmp_path)
    packed = tmp_path / "packed.json"
    packed.write_text('{"top":"top"}')
    with open_xilinx_mapped_index(mapped) as view, pytest.raises(ValidationError):
        validate_xilinx_route_db(
            route,
            mapped_path=mapped,
            packed_path=packed,
            mapped_value=view,
            source_sha256={"mapped_sha256": "0" * 64, "packed_sha256": "0" * 64},
        )
