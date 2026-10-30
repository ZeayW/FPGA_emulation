from tests.openparf_export_fixture import export_atomic_facts as export_xilinx_openparf_atomic
import json
import hashlib
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.openparf_bridge_fixture import materialize_bridge_fixture as materialize_xilinx_openparf_atomic_contract
from emuflow.common.errors import ValidationError
from emuflow.platform.architecture import ArchitectureDB
from emuflow.common.io import file_sha256
from emuflow.physical.xilinx_netlist import (
    emit_xilinx_mapped_index,
    open_xilinx_mapped_index,
)
from tests.openparf_placement_reference import (OPENPARF_ATOMIC_PLACEMENT_SCHEMA, OPENPARF_ATOMIC_PROVIDER, load_xilinx_openparf_atomic_placement_clusters, load_xilinx_openparf_atomic_sites, read_xilinx_openparf_atomic_name_map, validate_xilinx_openparf_atomic_placement, write_xilinx_openparf_atomic_placement_certificate)
from tests.physical_handoff_native_fixture import validate_certificate_facts
from tests.xilinx_site_reference import validate_xilinx_packing
from tests.xilinx_site_reference import validate_xilinx_placement
from emuflow.physical.rapidwright.xilinx_rwroute import export_rwroute_input
from tests.rwroute_reference import export_rwroute_reference
from tests.yosys_reference import import_yosys_json
from tests.openparf_runtime_fixture import write_openparf_runtime_fixture


def _inline_certificate(path: Path):
    value = json.loads(path.read_text(encoding="utf-8"))
    value["clusters"] = load_xilinx_openparf_atomic_placement_clusters(
        path, value
    )
    value.pop("cluster_storage", None)
    return value


def _rewrite_typed_certificate(path: Path, mutate):
    value = _inline_certificate(path)
    mutate(value)
    return write_xilinx_openparf_atomic_placement_certificate(path, value)


def _native_certificate(
    root: Path, *, include_hard: bool, split_ff_control_sets: bool = False
):
    mapped, source_packed, architecture = write_openparf_runtime_fixture(
        root, include_hard=include_hard
    )
    if split_ff_control_sets:
        mapped_value = json.loads(mapped.read_text())
        mapped_value["modules"]["top"]["cells"]["ff_03"][
            "connections"
        ]["C"] = [1_000_001]
        mapped.write_text(json.dumps(mapped_value), encoding="utf-8")
    export_dir = root / "openparf"
    export_xilinx_openparf_atomic(
        mapped, source_packed, architecture, export_dir
    )
    name_map = read_xilinx_openparf_atomic_name_map(export_dir / "name_map.json")
    sites = load_xilinx_openparf_atomic_sites(export_dir / "name_map.json")
    slices = [item for item in sites if "LUT" in item["resources"]]
    hard_sites = {
        resource: next(
            item for item in sites if resource in item["resources"]
        )
        for resource in ("DSP48E2", "RAMB36E2", "URAM288")
        if any(resource in item["resources"] for item in sites)
    }
    indexes = {"LUT": 0, "FF": 0}
    rows = []
    for atom in name_map["atoms"]:
        resource = atom["resource"]
        if resource in indexes:
            index = indexes[resource]
            indexes[resource] += 1
            site = slices[index // 4]
            z = 2 * (index % 4) + (1 if resource == "LUT" else 0)
            if split_ff_control_sets and atom["instance"] == "ff_03":
                z = 8
        else:
            site = hard_sites[resource]
            z = 0
        rows.append(
            f"{atom['openparf']} {site['dense_x']} {site['dense_y']} {z}"
        )
    native_placement = export_dir / "native.pl"
    native_placement.write_text("\n".join(rows) + "\n", encoding="utf-8")
    certificate_path = root / "atomic-certificate.json"
    certificate = validate_xilinx_openparf_atomic_placement(
        native_placement, export_dir / "name_map.json", mapped,
        architecture, certificate_path,
    )
    certificate["runtime_validation"] = "native-openparf"
    write_xilinx_openparf_atomic_placement_certificate(
        certificate_path, certificate
    )
    return mapped, architecture, certificate_path


class XilinxOpenparfBridgeTest(unittest.TestCase):
    def test_bram_tile_group_certificate_is_independently_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            architecture_path = root / "architecture.json"
            architecture_path.write_text(json.dumps({
                "schema": "emuflow.archdb/v1", "part": "xcvu19p-test",
                "source": {"format": "unit-test/v1"},
                "policy": {"name": "unit-test"},
                "site_templates": {
                    "RAMB180": {
                        "bels": [{
                            "name": "RAMB18E2_L", "type": "RAMB18E2",
                            "z": 0, "compatible_cells": ["RAMB18E2"],
                        }], "alternative_templates": [],
                    },
                    "RAMB36": {
                        "bels": [{
                            "name": "RAMB36E2", "type": "RAMB36E2",
                            "z": 0, "compatible_cells": ["RAMB36E2"],
                        }], "alternative_templates": [],
                    },
                    "RAMB181": {
                        "bels": [
                            {
                                "name": "RAMB18E2_L", "type": "RAMB18E2",
                                "z": 0, "compatible_cells": ["RAMB18E2"],
                                "placement_mode": "RAMB180",
                            },
                            {
                                "name": "RAMB18E2_U", "type": "RAMB18E2",
                                "z": 1, "compatible_cells": ["RAMB18E2"],
                                "placement_mode": "RAMB181",
                            },
                            {
                                "name": "RAMB36E2", "type": "RAMB36E2",
                                "z": 0, "compatible_cells": ["RAMB36E2"],
                                "placement_mode": "RAMB36",
                            },
                        ],
                        "alternative_templates": ["RAMB180", "RAMB36"],
                    },
                },
                "sites": [{
                    "name": "RAMB18_X0Y1", "type": "RAMB181",
                    "x": 4, "y": 1,
                    "bels": [
                        {
                            "name": "RAMB18E2_L", "type": "RAMB18E2",
                            "z": 0, "compatible_cells": ["RAMB18E2"],
                            "placement_mode": "RAMB180",
                        },
                        {
                            "name": "RAMB18E2_U", "type": "RAMB18E2",
                            "z": 1, "compatible_cells": ["RAMB18E2"],
                            "placement_mode": "RAMB181",
                        },
                        {
                            "name": "RAMB36E2", "type": "RAMB36E2",
                            "z": 0, "compatible_cells": ["RAMB36E2"],
                            "placement_mode": "RAMB36",
                        },
                    ],
                }],
            }), encoding="utf-8")
            architecture = ArchitectureDB.load(architecture_path)
            mapped = {
                "modules": {"top": {
                    "attributes": {"top": "1"},
                    "cells": {
                        "lo": {"type": "RAMB18E2"},
                        "hi": {"type": "RAMB18E2"},
                    },
                }},
            }
            tile = "BRAM_X0Y0"
            native_group = {
                "anchor": "RAMB18_X0Y1", "tile": tile,
                "lower": {
                    "site": "RAMB18_X0Y0", "site_type": "RAMB180",
                    "bel": "RAMB18E2_L",
                },
                "upper": {
                    "site": "RAMB18_X0Y1", "site_type": "RAMB181",
                    "bel": "RAMB18E2_U",
                },
                "whole": {
                    "site": "RAMB36_X0Y0", "site_type": "RAMB36",
                    "bel": "RAMB36E2",
                },
            }

            def assignment(instance, role):
                view = native_group[role]
                return {
                    "instance": instance, "cell_type": "RAMB18E2",
                    "bel": view["bel"], "physical_site": view["site"],
                    "placement_mode": view["site_type"],
                    "source_cluster": "source:bram",
                    "bram_tile_group": {
                        "anchor": native_group["anchor"],
                        "claims": [f"bram:{tile}:{role}"],
                        "role": role, "tile": tile,
                    },
                }

            certificate = {
                "schema": OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
                "status": "pass", "part": architecture.part,
                "provider": OPENPARF_ATOMIC_PROVIDER,
                "runtime_validation": "native-openparf",
                "clusters": [{
                    "cluster": "openparf:RAMB18_X0Y1",
                    "site": "RAMB18_X0Y1", "site_type": "RAMB181",
                    "x": 4, "y": 1,
                    "assignments": [
                        assignment("lo", "lower"), assignment("hi", "upper"),
                    ],
                }],
                "summary": {
                    "atoms": 2, "occupied_sites": 1, "luts": 0, "ffs": 0,
                    "hard_resources": {"RAMB18E2": 2},
                },
            }
            native_groups = {native_group["anchor"]: native_group}
            validate_certificate_facts(
                mapped, certificate, architecture, "top",
                native_bram_groups=native_groups,
            )

            for path, replacement in (
                (("bram_tile_group", "role"), "upper"),
                (("bram_tile_group", "claims"), [f"bram:{tile}:upper"]),
                (("bram_tile_group", "tile"), "BRAM_WRONG"),
                (("physical_site",), "RAMB18_X0Y1"),
            ):
                with self.subTest(path=path):
                    broken = copy.deepcopy(certificate)
                    target = broken["clusters"][0]["assignments"][0]
                    if len(path) == 1:
                        target[path[0]] = replacement
                    else:
                        target[path[0]][path[1]] = replacement
                    with self.assertRaisesRegex(
                        ValidationError, "BRAM|physical site"
                    ):
                        validate_certificate_facts(
                            mapped, broken, architecture, "top",
                            native_bram_groups=native_groups,
                                )

            conflicting = copy.deepcopy(certificate)
            conflicting_mapped = copy.deepcopy(mapped)
            conflicting_mapped["modules"]["top"]["cells"]["whole"] = {
                "type": "RAMB36E2"
            }
            whole = native_group["whole"]
            conflicting["clusters"][0]["assignments"].append({
                "instance": "whole", "cell_type": "RAMB36E2",
                "bel": whole["bel"], "physical_site": whole["site"],
                "placement_mode": whole["site_type"],
                "source_cluster": "source:bram",
                "bram_tile_group": {
                    "anchor": native_group["anchor"],
                    "claims": [
                        f"bram:{tile}:lower", f"bram:{tile}:upper",
                    ],
                    "role": "whole", "tile": tile,
                },
            })
            conflicting["summary"] = {
                "atoms": 3, "occupied_sites": 1, "luts": 0, "ffs": 0,
                "hard_resources": {"RAMB18E2": 2, "RAMB36E2": 1},
            }
            with self.assertRaisesRegex(
                ValidationError, "hard site is not singleton"
            ):
                validate_certificate_facts(
                    conflicting_mapped, conflicting, architecture, "top",
                    native_bram_groups=native_groups,
                )

    def test_region_capacity_uses_all_sites_and_rejects_physical_overuse(self):
        from tests.physical_handoff_native_fixture import prepare_native_handoff, build_native_handoff
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); data=prepare_native_handoff(root)
            cert=_inline_certificate(data.certificate)
            used={row['site'] for row in cert['clusters']}
            value=json.loads(data.architecture.read_text())
            value['sites']=[row for row in value['sites'] if row['name'] in used]
            for row in value['sites']:row['physical_region']={'slr':'SLR0','clock_region':'X0Y0'}
            data.architecture.write_text(json.dumps(value))
            cert['source']['architecture_sha256']=file_sha256(data.architecture)
            write_xilinx_openparf_atomic_placement_certificate(data.certificate,cert)
            report=build_native_handoff(data,root/'out')
            from emuflow.physical.xilinx_physical_handoff import open_xilinx_physical_handoff
            _,placement=open_xilinx_physical_handoff(Path(report['physical_handoff']))
            self.assertEqual(placement['summary']['maximum_clock_region_site_utilization'],1.0)
            cert['clusters'][1].update({k:cert['clusters'][0][k] for k in ('site','site_type','x','y')})
            with self.assertRaises(ValidationError):
                write_xilinx_openparf_atomic_placement_certificate(data.certificate,cert)
                build_native_handoff(data,root/'bad')

    def test_public_bridge_only_accepts_one_output(self):
        from emuflow.physical.handoff import materialize_xilinx_openparf_atomic_contract as typed_bridge
        with self.assertRaises(TypeError):
            typed_bridge(Path('mapped'), Path('arch'), Path('cert'), Path('packed.json'), Path('placement.json'))

    def test_public_bridge_rejects_source_aliases_without_modification(self):
        from tests.physical_handoff_native_fixture import prepare_native_handoff
        from emuflow.physical.handoff import materialize_xilinx_openparf_atomic_contract as typed
        import os
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);data=prepare_native_handoff(root)
            sidecar=data.certificate.with_name(json.loads(data.certificate.read_text())['cluster_storage']['file'])
            alias=root/'mapped-alias.sqlite';os.link(data.mapped,alias)
            for target in (data.mapped,data.architecture,data.certificate,sidecar,alias):
                before=file_sha256(target)
                with self.subTest(target=target),self.assertRaisesRegex(ValidationError,'output aliases an input'):
                    typed(data.mapped,data.architecture,data.certificate,target,source_packed_path=data.source)
                self.assertEqual(file_sha256(target),before)

    def test_bridge_header_reuses_source_digests(self):
        from tests.physical_handoff_native_fixture import prepare_native_handoff,build_native_handoff
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);data=prepare_native_handoff(root)
            with mock.patch('emuflow.common.io.file_sha256',side_effect=AssertionError('Python circuit hash replay')):
                report=build_native_handoff(data,root/'out')
            self.assertEqual(report['source']['mapped_sha256'],file_sha256(data.mapped))
            self.assertEqual(report['source']['architecture_sha256'],file_sha256(data.architecture))
            self.assertEqual(report['source']['openparf_atomic_placement_sha256'],file_sha256(data.certificate))

    def test_source_generation_is_checked_immediately_before_publication(self):
        from tests.physical_handoff_native_fixture import prepare_native_handoff,build_native_handoff
        from emuflow.physical import handoff as host
        import os
        for role in ('mapped','certificate','sidecar'):
            with self.subTest(role=role),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);data=prepare_native_handoff(root)
                target=data.certificate.with_name(json.loads(data.certificate.read_text())['cluster_storage']['file']) if role=='sidecar' else getattr(data,role)
                output=root/'out';output.write_bytes(b'previous-complete-artifact')
                write=host._write_request
                def mutate(*args):
                    write(*args);st=target.stat();os.utime(target,ns=(st.st_atime_ns,st.st_mtime_ns+1000000))
                with mock.patch.object(host,'_write_request',side_effect=mutate),self.assertRaisesRegex(ValidationError,'source generation changed'):
                    build_native_handoff(data,output)
                self.assertEqual(output.read_bytes(),b'previous-complete-artifact')
                self.assertEqual(list(root.glob('out.generation.*')),[])

    def test_native_legal_multi_control_set_slice_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, certificate = _native_certificate(
                root, include_hard=False, split_ff_control_sets=True
            )
            packed = root / "native-packed.json"
            placement = root / "native-placement.json"
            materialize_xilinx_openparf_atomic_contract(
                mapped, architecture, certificate, packed, placement
            )
            check = validate_xilinx_packing(
                mapped, packed, architecture_path=architecture
            )
            self.assertEqual(check["status"], "pass")
            value = json.loads(packed.read_text())
            cluster = next(
                item for item in value["clusters"]
                if {assignment["instance"] for assignment in item["assignments"]}
                >= {"ff_00", "ff_03"}
            )
            self.assertIsNone(cluster["control_set"])
            self.assertEqual(len(cluster["control_sets"]), 2)

    def test_mixed_atomic_result_converts_and_feeds_rwroute(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, certificate = _native_certificate(
                root, include_hard=True
            )
            mapped_before = mapped.read_bytes()
            packed = root / "native-packed.json"
            placement = root / "native-placement.json"
            report = materialize_xilinx_openparf_atomic_contract(
                mapped, architecture, certificate, packed, placement
            )

            self.assertEqual(mapped.read_bytes(), mapped_before)
            self.assertEqual(report["placed_cells"], 131)
            self.assertEqual(report["clusters"], 19)
            self.assertEqual(report["runtime_validation"], "native-openparf")
            self.assertEqual(
                validate_xilinx_packing(
                    mapped, packed, architecture_path=architecture
                )["cells"],
                131,
            )
            self.assertEqual(
                validate_xilinx_placement(packed, architecture, placement)["cells"],
                131,
            )

            packed_value = json.loads(packed.read_text())
            placement_value = json.loads(placement.read_text())
            packed_instances = [
                assignment["instance"]
                for cluster in packed_value["clusters"]
                for assignment in cluster["assignments"]
            ]
            placed_instances = [
                assignment["instance"]
                for cluster in placement_value["clusters"]
                for assignment in cluster["assignments"]
            ]
            self.assertEqual(len(packed_instances), len(set(packed_instances)))
            self.assertEqual(set(packed_instances), set(placed_instances))
            self.assertEqual(
                {cluster["id"][len("openparf:"):]
                 for cluster in packed_value["clusters"]},
                {cluster["site"] for cluster in placement_value["clusters"]},
            )
            self.assertEqual(
                placement_value["provider"],
                "openparf-native-mcf-direct-lg-ism-atomic-bridge-v1",
            )
            rwroute = root / "rwroute.tsv"
            rwroute_report = export_rwroute_reference(
                mapped, packed, placement, rwroute
            )
            self.assertEqual(rwroute_report["logical_cells"], 131)
            self.assertEqual(rwroute_report["physical_cells"], 138)
            self.assertTrue(rwroute.is_file())

    def test_native_physical_handoff_replaces_both_large_json_outputs(self):
        from tests.physical_handoff_native_fixture import prepare_native_handoff,build_native_handoff
        from emuflow.physical.xilinx_physical_handoff import open_xilinx_physical_handoff
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);data=prepare_native_handoff(root)
            packed,placement={},{}
            from emuflow.physical.handoff import materialize_xilinx_openparf_atomic_contract as typed
            with open_xilinx_mapped_index(data.mapped) as mapped:
                with mock.patch.object(type(mapped.cells),'items',side_effect=AssertionError('Python cell population')), \
                     mock.patch('tests.xilinx_site_reference.validate_xilinx_packing',side_effect=AssertionError('Python packing validator')), \
                     mock.patch('tests.xilinx_site_reference.validate_xilinx_placement',side_effect=AssertionError('Python placement validator')):
                    report=typed(data.mapped,data.architecture,data.certificate,root/'physical',source_packed_path=data.source,
                                 mapped_value=mapped,packed_value_sink=packed,placement_value_sink=placement)
                handoff=Path(report['physical_handoff'])
                self.assertTrue(handoff.is_file())
                self.assertFalse((root/'placement.json').exists())
                self.assertFalse((root/'packed-sites.json').exists())
                self.assertEqual(file_sha256(handoff),report['physical_handoff_sha256'])
                self.assertEqual(report['placed_cells'],128)
                self.assertEqual(report['validation']['source_ownership']['ownership_snapshots'],0)
                self.assertEqual(len(packed['clusters']),len(placement['clusters']))
                self.assertEqual({a['instance'] for c in packed['clusters'] for a in c['assignments']},set(data.owners))
                native=root/'native.tsv';reference=root/'reference.tsv'
                export_rwroute_input(data.mapped,handoff,native)
                export_rwroute_reference(data.mapped,handoff,handoff,reference,mapped_value=mapped,packed_value=packed,placement_value=placement)
                self.assertEqual(native.read_bytes(),reference.read_bytes())
            repeated=build_native_handoff(data,root/'repeated')
            self.assertEqual(Path(repeated['physical_handoff']).read_bytes(),handoff.read_bytes())
            self.assertEqual(repeated['physical_handoff_sha256'],report['physical_handoff_sha256'])
            lazy,_=open_xilinx_physical_handoff(handoff)
            self.assertNotIsInstance(lazy['unplaced_constants'],list)
            self.assertNotIsInstance(lazy['cascade_chains'],list)

    def test_streamed_and_compatibility_validation_reject_lut_multidriver(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, certificate = _native_certificate(
                root, include_hard=False
            )
            mapped_value = json.loads(mapped.read_text(encoding="utf-8"))
            cells = mapped_value["modules"]["top"]["cells"]
            # This bit is outside every MUX/CARRY topology working set.  Both
            # validators must still apply the whole-netlist uniqueness rule.
            cells["lut_00"]["connections"]["O"] = [9_999_999]
            cells["lut_01"]["connections"]["O"] = [9_999_999]
            mapped.write_text(json.dumps(mapped_value), encoding="utf-8")
            _rewrite_typed_certificate(
                certificate,
                lambda value: value["source"].update({
                    "mapped_sha256": file_sha256(mapped)
                }),
            )

            for production in (False, True):
                with self.subTest(production=production):
                    handoff = root / "physical-handoff.sqlite"
                    with self.assertRaisesRegex(
                        ValidationError,
                        r"mapped net bit 9999999 has multiple drivers",
                    ):
                        materialize_xilinx_openparf_atomic_contract(
                            mapped, architecture, certificate,
                            root / "packed.json", root / "placement.json",
                            **(
                                {"physical_handoff_path": handoff}
                                if production else {}
                            ),
                        )
                    self.assertFalse(handoff.exists())
                    self.assertEqual(
                        list(root.glob(".physical-handoff.sqlite.*.tmp")), []
                    )

    def test_failed_independent_validation_does_not_publish_handoff(self):
        from tests.physical_handoff_native_fixture import prepare_native_handoff,build_native_handoff
        from emuflow.physical import handoff as host
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);data=prepare_native_handoff(root);output=root/'out'
            output.write_bytes(b'previous-complete-artifact')
            with mock.patch.object(host.NativeToolProcess,'finish',return_value=SimpleNamespace(returncode=7,stdout='independent rejection')):
                with self.assertRaisesRegex(ValidationError,'independent rejection'):build_native_handoff(data,output)
            self.assertEqual(output.read_bytes(),b'previous-complete-artifact')
            self.assertEqual(list(root.glob('out.generation.*')),[])

    def test_missing_atom_fails_without_publishing_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, certificate = _native_certificate(
                root, include_hard=False
            )
            _rewrite_typed_certificate(
                certificate,
                lambda value: value["clusters"][0]["assignments"].pop(),
            )
            packed = root / "converted-packed.json"
            placement = root / "converted-placement.json"
            with self.assertRaisesRegex(ValidationError, "coverage is incomplete"):
                materialize_xilinx_openparf_atomic_contract(
                    mapped, architecture, certificate, packed, placement
                )
            self.assertFalse(packed.exists())
            self.assertFalse(placement.exists())
            handoff = root / "physical-handoff.sqlite"
            with self.assertRaisesRegex(ValidationError, "coverage is incomplete"):
                materialize_xilinx_openparf_atomic_contract(
                    mapped, architecture, certificate,
                    root / "unused-packed.json",
                    root / "unused-placement.json",
                    physical_handoff_path=handoff,
                )
            self.assertFalse(handoff.exists())
            self.assertEqual(
                list(root.glob(f".{handoff.name}.*.tmp")), []
            )

    def test_bel_tampering_and_unsupported_primitive_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, certificate = _native_certificate(
                root, include_hard=False
            )
            value = _inline_certificate(certificate)
            assignment = value["clusters"][0]["assignments"][0]
            assignment["bel"] = "NOT_A_BEL"
            certificate.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "incompatible physical BEL"):
                materialize_xilinx_openparf_atomic_contract(
                    mapped, architecture, certificate,
                    root / "bad-packed.json", root / "bad-placement.json",
                )

            mapped_value = json.loads(mapped.read_text())
            mapped_value["modules"]["top"]["cells"]["lut_00"]["type"] = "BUFGCE"
            mapped.write_text(json.dumps(mapped_value), encoding="utf-8")
            value["source"]["mapped_sha256"] = hashlib.sha256(
                mapped.read_bytes()
            ).hexdigest()
            certificate.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "does not support primitives"):
                materialize_xilinx_openparf_atomic_contract(
                    mapped, architecture, certificate,
                    root / "unsupported-packed.json",
                    root / "unsupported-placement.json",
                )

    def test_mapped_connectivity_tampering_breaks_the_source_seal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, certificate = _native_certificate(
                root, include_hard=False
            )
            mapped_value = json.loads(mapped.read_text())
            mapped_value["modules"]["top"]["cells"]["lut_00"][
                "connections"
            ]["I0"] = [999_999]
            mapped.write_text(json.dumps(mapped_value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "source identity"):
                materialize_xilinx_openparf_atomic_contract(
                    mapped, architecture, certificate,
                    root / "packed.json", root / "placement.json",
                )

    def test_duplicate_site_group_rejected_by_typed_certificate_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, certificate = _native_certificate(
                root, include_hard=False
            )
            value = _inline_certificate(certificate)
            value["clusters"].append(dict(value["clusters"][0]))
            certificate.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "cluster cannot be stored"):
                materialize_xilinx_openparf_atomic_contract(
                    mapped, architecture, certificate,
                    root / "packed.json", root / "placement.json",
                )

    def test_cascade_certificate_requires_source_packing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, certificate = _native_certificate(
                root, include_hard=True
            )
            _rewrite_typed_certificate(
                certificate,
                lambda value: value["summary"].update({
                    "native_hardblock_edges": 1
                }),
            )
            with self.assertRaisesRegex(
                ValidationError, "summary invalid"
            ):
                materialize_xilinx_openparf_atomic_contract(
                    mapped, architecture, certificate,
                    root / "packed.json", root / "placement.json",
                )


if __name__ == "__main__":
    unittest.main()
