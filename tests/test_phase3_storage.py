from tests.phase3_store_fixture import assignment_selector_fixture, cluster_document_fixture, write_cluster_fixture
from contextlib import closing
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow_tools.document_io import read_json
from emuflow.common.json_io import write_json
from emuflow.partition.phase3_storage import (CLUSTERS_SQLITE_APPLICATION_ID, INDEXED_CLUSTERS_SCHEMA, read_phase3_assignment_view, read_phase3_cut_timing_view)


def _large_artifacts(count: int = 1000):
    clusters = []
    cluster_assignment = {}
    instance_assignment = {}
    for index in range(count):
        cluster_id = f"c{index:06d}"
        instances = [f"top/logic_block_{index}/cell_{lane}" for lane in range(2)]
        clusters.append(
            {
                "id": cluster_id,
                "instances": instances,
                "resources": {"cells": 2, "lut": 1},
                "fixed_fpga": None,
                "groups": [],
            }
        )
        part = f"fpga{index % 4}"
        cluster_assignment[cluster_id] = part
        instance_assignment.update({instance: part for instance in instances})
    cluster_artifact = {
        "schema": "emuflow.clusters/v1",
        "design": "large",
        "clusters": clusters,
        "instances": count * 2,
        "policy": {"legal_cut_classes": ["register_output"]},
    }
    partitions = []
    for part in [f"fpga{index}" for index in range(4)]:
        ids = sorted(
            cluster_id
            for cluster_id, assigned in cluster_assignment.items()
            if assigned == part
        )
        partitions.append(
            {
                "fpga": part,
                "clusters": ids,
                "cluster_count": len(ids),
                "instance_count": len(ids) * 2,
                "resources": {"cells": len(ids) * 2, "lut": len(ids)},
                "effective_capacity": {"cells": 10000, "lut": 10000},
                "utilization": {"cells": len(ids) / 5000, "lut": len(ids) / 10000},
            }
        )
    assignment = {
        "schema": "emuflow.partition-assignment/v1",
        "design": "large",
        "platform": "four",
        "provider": "test",
        "seed": 1,
        "constraints": {"balance_tolerance": 0.25},
        "cluster_assignment": cluster_assignment,
        "instance_assignment": instance_assignment,
        "partitions": partitions,
        "cut_nets": [],
        "metrics": {"instances": count * 2, "clusters": count},
    }
    return cluster_artifact, assignment


class Phase3StorageTest(unittest.TestCase):
    def test_indexed_cluster_selector_validates_without_expanding_population(self) -> None:
        clusters, _ = _large_artifacts(32)
        clusters["design"] = "large-设计-µ"
        clusters["policy"]["eligible_combinational_cut_nets"] = [
            "top/net_β",
            "top/net_γ",
        ]
        clusters["clusters"][0]["qualification"] = {"kind": "fixture"}
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            selector_path = root / "clusters.json"
            selector = write_cluster_fixture(selector_path, clusters)
            stored = json.loads(selector_path.read_text())
            self.assertEqual(stored, selector)
            self.assertEqual(stored["schema"], INDEXED_CLUSTERS_SCHEMA)
            self.assertEqual(
                stored["application_id"], CLUSTERS_SQLITE_APPLICATION_ID
            )
            self.assertEqual(stored["counts"]["clusters"], 32)
            self.assertEqual(stored["counts"]["instances"], 64)
            self.assertEqual(stored["counts"]["eligible_nets"], 2)
            store = root / stored["artifact"]
            self.assertTrue(store.is_file())
            self.assertLess(selector_path.stat().st_size, 1024)
            with closing(sqlite3.connect(store)) as connection, connection:
                self.assertEqual(
                    connection.execute("PRAGMA application_id").fetchone()[0],
                    CLUSTERS_SQLITE_APPLICATION_ID,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT records FROM sections "
                        "WHERE section='member_names'"
                    ).fetchone()[0],
                    64,
                )
            self.assertEqual(read_json(selector_path), selector)
            self.assertNotIn('clusters', read_json(selector_path))

            with store.open("r+b") as stream:
                stream.seek(128)
                byte = stream.read(1)
                stream.seek(128)
                stream.write(bytes([byte[0] ^ 1]))
            with self.assertRaisesRegex(ValueError, "seal disagrees"):
                read_json(selector_path)


    def test_indexed_clusters_reject_resealed_section_count_corruption(self) -> None:
        clusters, _ = _large_artifacts(4)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            selector_path = root / "clusters.json"
            selector = write_cluster_fixture(selector_path, clusters)
            store = root / selector["artifact"]
            with closing(sqlite3.connect(store)) as connection, connection:
                connection.execute(
                    "UPDATE sections SET records=records+1 "
                    "WHERE section='cluster_ids'"
                )
            selector["store_sha256"] = hashlib.sha256(
                store.read_bytes()
            ).hexdigest()
            selector["store_bytes"] = store.stat().st_size
            renamed = store.with_name(f"clusters.{selector['store_sha256']}.sqlite")
            store.rename(renamed)
            selector["artifact"] = renamed.name
            selector_path.write_text(json.dumps(selector), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "section counts disagree"):
                read_json(selector_path)


    def _current_assignment(self, root, assignment=None, clusters=None):
        from types import SimpleNamespace
        from tests.phase3_store_fixture import write_assignment_index_fixture
        if assignment is None: clusters, assignment = _large_artifacts(4)
        ir = SimpleNamespace(value={'instances':[{'id':name} for name in assignment['instance_assignment']]})
        index = root/'assignment.sqlite'
        summary = write_assignment_index_fixture(index,ir,assignment)
        selector = assignment_selector_fixture(assignment,clusters,cut_nets_index={
            'artifact':index.name,'schema':'emuflow.phase3-cut-records/v1',
            'records':summary['cut_records'],'sha256':summary['cut_records_sha256']})
        path = root/'assignment.json';write_json(path,selector,compact=True)
        return path, selector, assignment

    def test_current_lazy_ownership_never_needs_sibling_clusters(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);path,selector,assignment=self._current_assignment(root)
            with mock.patch('emuflow.ir.ir.EmuIR.load',side_effect=AssertionError('graph load')):
                view=read_phase3_assignment_view(path,include_instance_assignment=True)
            self.assertNotIn('cluster_assignment',view)
            self.assertNotIn('semantic_contract',view)
            self.assertEqual(list(view['cut_nets']),[])
            self.assertFalse((root/'clusters.json').exists())
            owners=view['instance_assignment']
            self.assertEqual(dict(owners),assignment['instance_assignment'])
            self.assertIsNone(owners.get('absent'));owners.close()
            self.assertEqual(read_json(path)['schema'],assignment['schema'])

    def test_current_cut_view_preserves_order_and_unused_partitions(self):
        clusters,assignment=_large_artifacts(4)
        assignment['partitions'].append({'fpga':'fpga4','cluster_count':0,'instance_count':0,'resources':{}})
        assignment['cut_nets']=[{'net':'n0','cut_class':'register_output','source_fpgas':['fpga0'],
            'sink_fpgas':['fpga1'],'sink_endpoints':1}]
        with tempfile.TemporaryDirectory() as temporary:
            path,_,_=self._current_assignment(Path(temporary),assignment,clusters)
            view=read_phase3_cut_timing_view(path)
            self.assertEqual(list(view['cut_nets']),assignment['cut_nets'])
            self.assertEqual(view['partitions'][-1]['fpga'],'fpga4')
            self.assertNotIn('clusters',view['partitions'][-1])

    def test_current_selector_rejects_owner_mismatch_and_missing_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);path,selector,_=self._current_assignment(root)
            for field in ('design','platform','provider','seed','constraints'):
                changed=json.loads(json.dumps(selector));changed['document'][field]='mixed-source'
                write_json(path,changed,compact=True)
                with self.subTest(field=field),self.assertRaisesRegex(ValueError,'identity disagree'):
                    read_json(path)
            write_json(path,selector,compact=True);(root/'assignment.sqlite').unlink()
            with self.assertRaisesRegex(ValueError,'index is malformed'):read_json(path)

    def test_retired_assignment_and_cluster_storage_versions_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'selector.json'
            for family,versions in (('clusters',(1,2)),('assignment',(1,2,3))):
                for version in versions:
                    write_json(path,{'schema':f'emuflow.phase3-{family}-storage/v{version}'})
                    with self.subTest(family=family,version=version),self.assertRaisesRegex(ValueError,'retired or unsupported'):
                        read_json(path)
                    if family=='assignment':
                        with self.assertRaisesRegex(ValueError,'retired or unsupported'):
                            read_phase3_assignment_view(path)

    def test_plain_logical_assignment_is_not_a_production_reader_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'assignment.json'
            write_json(path,{'schema':'emuflow.partition-assignment/v1','cut_nets':[]})
            with self.assertRaisesRegex(ValueError,'current v4 selector required'):
                read_phase3_assignment_view(path)

    def test_current_reader_rejects_legacy_compressed_semantic_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            path,selector,_=self._current_assignment(Path(temporary))
            selector['semantic_contract']={'codec':'zlib-level-1-base64-canonical-json','data':'unused'}
            write_json(path,selector,compact=True)
            with self.assertRaisesRegex(ValueError,'header is malformed'):read_json(path)
            with self.assertRaisesRegex(ValueError,'materialization is retired'):
                read_phase3_assignment_view(path,include_semantic_contract=True)


if __name__ == "__main__":
    unittest.main()
