import shutil
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.physical_handoff_fixture import write_handoff_fixture
from emuflow.common.errors import ValidationError
from emuflow.common.json_io import file_sha256
from emuflow_tools.physical.contracts import PACKED_SITE_NETLIST_SCHEMA
from emuflow_tools.physical.xilinx_physical_handoff import open_xilinx_physical_handoff, validate_xilinx_physical_handoff, validate_xilinx_physical_handoff_pair
from emuflow_tools.physical.contracts import XILINX_PLACEMENT_SCHEMA


def _contracts(identity: str = "cell0"):
    packed = {
        "schema": PACKED_SITE_NETLIST_SCHEMA,
        "status": "pass",
        "mapping_profile": "test",
        "top": "top",
        "source": {"mapped_json_sha256": "a" * 64},
        "policy": {"provider": "test"},
        "clusters": [{
            "id": "openparf:SLICEL_X0Y0", "kind": "slice",
            "site_templates": ["SLICEL"], "control_set": None,
            "assignments": [{
                "instance": identity, "cell_type": "LUT1", "bel": "A6LUT",
            }],
        }],
        "cascade_chains": [{"cell_type": "CARRY8", "instances": [identity]}],
        "unplaced_constants": ["gnd"],
        "summary": {"clusters": 1},
    }
    placement = {
        "schema": XILINX_PLACEMENT_SCHEMA,
        "status": "pass", "part": "xcvu19p-test", "provider": "test",
        "policy": {"test": True},
        "source": {"packed_sha256": None, "architecture_sha256": "b" * 64},
        "clusters": [{
            "cluster": "openparf:SLICEL_X0Y0", "site": "SLICEL_X0Y0",
            "site_type": "SLICEL", "x": 0, "y": 0, "fixed": False,
            "physical_region": {"slr": "SLR0", "clock_region": "X0Y0"},
            "assignments": [{
                "instance": identity, "cell_type": "LUT1", "bel": "A6LUT",
                "placement_mode": "SLICEL", "site": "SLICEL_X0Y0",
            }],
        }],
        "summary": {"clusters": 1},
    }
    return packed, placement


class XilinxPhysicalHandoffTest(unittest.TestCase):
    def setUp(self):
        from tests.native_build import emuflow_native
        runtime=mock.patch.dict(os.environ,{'EMUFLOW_NATIVE_ROOT':str(emuflow_native().parent.parent)})
        runtime.start()
        self.addCleanup(runtime.stop)

    @staticmethod
    def _bridge_cluster(index: int):
        site = f"SLICEL_X{index}Y0"
        return {
            "id": f"openparf:{site}", "kind": "slice",
            "site_templates": ["SLICEL"], "control_set": None,
            "assignments": [{
                "instance": f"cell{index}", "cell_type": "LUT1",
                "bel": "A6LUT", "_placement_mode": "SLICEL",
                "_physical_site": site, "_placement_ordinal": 0,
            }],
            "_placement": {
                "site": site, "site_type": "SLICEL", "x": index,
                "y": 0, "fixed": False, "physical_region": None,
            },
        }

    def test_round_trip_exposes_lazy_equivalent_views(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "physical-handoff.sqlite"
            packed, placement = _contracts()
            write_handoff_fixture(path, packed, placement)
            report = validate_xilinx_physical_handoff(path)
            actual_packed, actual_placement = open_xilinx_physical_handoff(path)

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["assignments"], 1)
            self.assertNotIsInstance(actual_packed["clusters"], list)
            self.assertEqual(list(actual_packed["clusters"]), packed["clusters"])
            self.assertEqual(
                list(actual_placement["clusters"]), placement["clusters"]
            )
            self.assertEqual(actual_packed["cascade_chains"], packed["cascade_chains"])
            self.assertEqual(
                actual_placement["source"]["packed_sha256"], file_sha256(path)
            )

    def test_validated_digest_avoids_rehashing_published_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "physical-handoff.sqlite"
            write_handoff_fixture(path, *_contracts())
            digest = file_sha256(path)

            with mock.patch(
                "emuflow_tools.physical.xilinx_physical_handoff.file_sha256",
                side_effect=AssertionError("unexpected duplicate digest"),
            ):
                packed, placement = open_xilinx_physical_handoff(
                    path, validated_sha256=digest
                )

            self.assertEqual(
                packed["source"]["physical_handoff_sha256"], digest
            )
            self.assertEqual(placement["source"]["packed_sha256"], digest)

            with self.assertRaisesRegex(ValidationError, "digest is invalid"):
                open_xilinx_physical_handoff(path, validated_sha256="invalid")

    def test_lazy_reader_preserves_explicit_unmarked_current_row_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "physical-handoff.sqlite"
            first_packed, first_placement = _contracts("cell0")
            second_packed, second_placement = _contracts("cell1")
            second_packed["clusters"][0]["id"] = "openparf:SLICEL_X1Y0"
            second_placement["clusters"][0].update({
                "cluster": "openparf:SLICEL_X1Y0",
                "site": "SLICEL_X1Y0",
                "x": 1,
            })
            second_placement["clusters"][0]["assignments"][0]["site"] = (
                "SLICEL_X1Y0"
            )
            first_packed["clusters"].extend(second_packed["clusters"])
            first_packed["clusters"].reverse()
            first_placement["clusters"].extend(second_placement["clusters"])

            write_handoff_fixture(
                path, first_packed, first_placement
            )
            packed, placement = open_xilinx_physical_handoff(path)

            self.assertEqual(
                [cluster["id"] for cluster in packed["clusters"]],
                ["openparf:SLICEL_X1Y0", "openparf:SLICEL_X0Y0"],
            )
            self.assertEqual(
                [cluster["cluster"] for cluster in placement["clusters"]],
                ["openparf:SLICEL_X1Y0", "openparf:SLICEL_X0Y0"],
            )

    def test_corruption_and_truncation_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.sqlite"
            write_handoff_fixture(source, *_contracts())
            corrupt = root / "corrupt.sqlite"
            shutil.copyfile(source, corrupt)
            with corrupt.open("r+b") as stream:
                stream.seek(0)
                stream.write(b"not-a-sqlite-db!")
            with self.assertRaises(ValidationError):
                validate_xilinx_physical_handoff(
                    corrupt, integrity_check=True
                )

            truncated = root / "truncated.sqlite"
            data = source.read_bytes()
            truncated.write_bytes(data[: max(64, len(data) // 2)])
            with self.assertRaises(ValidationError):
                validate_xilinx_physical_handoff(
                    truncated, integrity_check=True
                )

    def test_relocation_and_mixed_run_protection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "first" / "physical-handoff.sqlite"
            write_handoff_fixture(original, *_contracts("cell0"))
            relocated = root / "second" / original.name
            relocated.parent.mkdir()
            original.replace(relocated)
            packed, placement = open_xilinx_physical_handoff(relocated)
            self.assertEqual(
                list(packed["clusters"])[0]["assignments"][0]["instance"],
                "cell0",
            )
            self.assertEqual(
                placement["source"]["packed_sha256"], file_sha256(relocated)
            )

            other = root / "other.sqlite"
            write_handoff_fixture(other, *_contracts("cell-other"))
            packed_first, _ = open_xilinx_physical_handoff(relocated)
            _, placement_other = open_xilinx_physical_handoff(other)
            with self.assertRaisesRegex(ValidationError, "runs disagree"):
                validate_xilinx_physical_handoff_pair(
                    packed_first, placement_other
                )

    def test_current_lazy_reader_keeps_large_repeated_cluster_population(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'physical.sqlite'
            packed,placement=_contracts()
            packed['clusters']=[]
            packed['cascade_chains']=[]
            packed['unplaced_constants']=[]
            placement['clusters']=[]
            for i in reversed(range(2048)):
                cluster=self._bridge_cluster(i)
                packed['clusters'].append({key:value for key,value in cluster.items() if key!='_placement'})
                packed['clusters'][-1]['assignments']=[{key:row[key] for key in ('instance','cell_type','bel')}
                                                      for row in cluster['assignments']]
                placement['clusters'].append(dict(cluster['_placement'],cluster=cluster['id'],physical_region=None,
                    assignments=[dict(instance=row['instance'],cell_type=row['cell_type'],bel=row['bel'],
                        placement_mode=row['_placement_mode'],site=row['_physical_site']) for row in cluster['assignments']]))
            write_handoff_fixture(path,packed,placement,canonical=True)
            report=validate_xilinx_physical_handoff(path)
            actual_packed,actual_placement=open_xilinx_physical_handoff(path)
            self.assertEqual(report['packed_clusters'],2048)
            self.assertEqual(report['assignments'],2048)
            self.assertEqual(len(actual_packed['clusters']),2048)
            self.assertEqual(actual_packed['clusters'][0]['id'],'openparf:SLICEL_X0Y0')
            self.assertEqual(actual_placement['clusters'][0]['cluster'],'openparf:SLICEL_X0Y0')

    def test_python_handoff_producer_routes_are_retired(self):
        import emuflow_tools.physical.xilinx_physical_handoff as handoff
        self.assertFalse(hasattr(handoff,'XilinxPhysicalHandoffBuilder'))
        self.assertFalse(hasattr(handoff,'write_xilinx_physical_handoff'))

    @staticmethod
    def _repair_contracts():
        packed, placement = _contracts('capture')
        packed['cascade_chains'] = []
        packed['unplaced_constants'] = []
        packed['clusters'][0]['assignments'][0].update(cell_type='FDRE', bel='CFF')
        placement['clusters'][0]['assignments'][0].update(cell_type='FDRE', bel='CFF')
        repairs = [
            {'cluster': packed['clusters'][0]['id'], 'instance': 'capture', 'from_bel': 'AFF', 'to_bel': 'BFF'},
            {'cluster': packed['clusters'][0]['id'], 'instance': 'capture', 'from_bel': 'BFF', 'to_bel': 'CFF'},
        ]
        return packed, placement, repairs

    def test_typed_repairs_are_ordered_source_bound_lazy_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'physical.sqlite'
            packed, placement, repairs = self._repair_contracts()
            write_handoff_fixture(path, packed, placement, repairs=repairs)
            self.assertEqual(validate_xilinx_physical_handoff(path)['status'], 'pass')
            actual, _ = open_xilinx_physical_handoff(path)
            records = actual['policy']['site_pin_legalization']['repairs']
            self.assertNotIsInstance(records, list)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0], repairs[0])
            self.assertEqual(records[-1], repairs[-1])
            self.assertEqual(list(records), repairs)
            with sqlite3.connect(path) as db:
                db.execute("UPDATE site_pin_repair SET from_bel='DFF' WHERE seq=0")
            with self.assertRaisesRegex(ValidationError, 'generation changed'):
                list(records)

    def test_typed_repair_seals_chains_and_feature_contract_fail_closed(self):
        mutations = (
            "UPDATE site_pin_repair SET from_bel='DFF' WHERE seq=0",
            "UPDATE site_pin_repair SET from_bel='DFF' WHERE seq=1",
            "UPDATE site_pin_repair SET to_bel='DFF' WHERE seq=1",
            "UPDATE site_pin_repair SET seq=5 WHERE seq=1",
            "DELETE FROM site_pin_repair WHERE seq=1",
            "DELETE FROM feature WHERE name='typed-site-pin-repairs-v1'",
            "INSERT INTO feature VALUES('unknown-feature-v99')",
        )
        for sql in mutations:
            with self.subTest(sql=sql), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'physical.sqlite'
                packed, placement, repairs = self._repair_contracts()
                write_handoff_fixture(path, packed, placement, repairs=repairs)
                with sqlite3.connect(path) as db:
                    db.execute(sql)
                with self.assertRaises(ValidationError):
                    validate_xilinx_physical_handoff(path)

    def test_empty_typed_repairs_keep_feature_and_empty_lazy_sequence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'physical.sqlite'
            write_handoff_fixture(path, *_contracts(), repairs=[])
            self.assertEqual(validate_xilinx_physical_handoff(path)['status'], 'pass')
            packed, _ = open_xilinx_physical_handoff(path)
            records = packed['policy']['site_pin_legalization']['repairs']
            self.assertNotIsInstance(records, list)
            self.assertEqual(list(records), [])
            with self.assertRaises(IndexError):
                records[0]

    def test_lazy_reader_rejects_unknown_features_and_unmarked_repair_references(self):
        for mutation in (
            "INSERT INTO feature VALUES('unknown-feature-v99')",
            "DELETE FROM feature WHERE name='typed-site-pin-repairs-v1'; DROP TABLE site_pin_repair",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'physical.sqlite'
                write_handoff_fixture(path, *_contracts(), repairs=[])
                with sqlite3.connect(path) as db:
                    db.executescript(mutation)
                with self.assertRaisesRegex(ValidationError, 'feature'):
                    open_xilinx_physical_handoff(path)

    def test_native_structure_gate_rejects_row_counts_owners_and_sequence_corruption(self):
        import sqlite3
        for sql in ("UPDATE manifest SET assignments=99", "UPDATE assignment SET cluster_ordinal=99",
                    "UPDATE assignment SET packed_ordinal=99", "UPDATE document SET value_json='{}' WHERE name='packed'"):
            with self.subTest(sql=sql),tempfile.TemporaryDirectory() as temporary:
                path=Path(temporary)/'physical.sqlite'
                write_handoff_fixture(path,*_contracts())
                with sqlite3.connect(path) as db:db.execute(sql)
                with self.assertRaises(ValidationError):validate_xilinx_physical_handoff(path)


if __name__ == "__main__":
    unittest.main()
