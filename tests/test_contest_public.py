import json
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.contest_public import (
    PUBLIC_CONTEST_BOARDDB_REPORT_SCHEMA,
    PUBLIC_CONTEST_EVALUATION_REPORT_SCHEMA,
    PUBLIC_CONTEST_EVALUATION_REPORT_SCHEMA_V1,
    PUBLIC_CONTEST_FETCH_REPORT_SCHEMA,
    PUBLIC_CONTEST_IMPORT_REPORT_SCHEMA,
    evaluate_public_contest_case,
    fetch_public_contest_case,
    import_public_contest_case,
    materialize_public_contest_boarddb,
    validate_public_contest_evaluation,
)
from emuflow.contest_validation_matrix import canonical_matrix_sha256
from emuflow.cli import main
from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json


class PublicContestFetchTest(unittest.TestCase):
    def _semantic_fixture(self, root: Path, suite: str) -> tuple[Path, Path]:
        source = root / "source"
        source.mkdir()
        if suite == "eda2023":
            files = {
                "design.fpga.die": "FPGA0:Die0 Die1\nFPGA1:Die2 Die3\n",
                "design.die.position": "Die0:g0\nDie1:g1\nDie2:g2\nDie3:g3\n",
                "design.die.network": "0 2 0 0\n2 0 2 0\n0 2 0 2\n0 0 2 0\n",
                "design.net": "g0 s 1\ng2 l\ng1 s 1\ng3 l\n",
            }
        elif suite == "eda2024-repart":
            files = {
                "design.info": "F1 10 8 8 0 0 0 0 0 0\nF2 10 8 8 0 0 0 0 0 0\n",
                "design.are": "a 1 1 0 0 0 0 0 0\nb 1 1 0 0 0 0 0 0\n",
                "design.net": "a 3 b\n",
                "design.topo": "1\nF1 F2\n",
            }
        elif suite == "eda2025":
            files = {
                "design.info": "F1 2\nF2 2\n",
                "design.net": "a 1 b\n",
                "design.topo": "F1: 0,1\nF2: 1,0\n",
                "design.fpga.out": "F1: a\nF2: b\n",
            }
        else:
            raise AssertionError(suite)
        records = []
        for name, value in files.items():
            payload = value.encode("utf-8")
            (source / name).write_bytes(payload)
            records.append({
                "name": name,
                "bytes": len(payload),
                "git_blob_sha1": hashlib.sha1(
                    f"blob {len(payload)}\0".encode("ascii") + payload
                ).hexdigest(),
            })
        provenance = {
            "schema": "emuflow.public-benchmark-fetch/v1",
            "commit": "1" * 40,
            "case": "case1",
            "files": records,
        }
        write_json(source / "SOURCE.json", provenance)
        matrix = {
            "schema": "emuflow.contest-validation-matrix/v1",
            "cases": [{
                "id": f"{suite}.case1",
                "suite": suite,
                "case": "case1",
                "source": {
                    "fetcher": "scripts/fetch_fixture.py",
                    "revision_kind": "git-commit",
                    "revision": "1" * 40,
                },
                "input_bytes": sum(record["bytes"] for record in records),
                "tier": "smoke",
                "qualification": "catalogued",
                "target_gates": [
                    "fetch", "import", "evaluate", "materialize-boarddb"
                ],
                "evidence": [],
            }],
        }
        matrix_path = root / "matrix.json"
        write_json(matrix_path, matrix)
        return matrix_path, source

    def _fixture(self, root: Path, revision: str = "1" * 40) -> tuple[Path, Path]:
        scripts = root / "scripts"
        scripts.mkdir()
        fetcher = scripts / "fetch_fixture.py"
        fetcher.write_text(
            """import argparse, hashlib, json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--case', required=True)
p.add_argument('--out', type=Path, required=True)
a = p.parse_args()
a.out.mkdir(parents=True)
payload = b'abc'
(a.out / 'design.net').write_bytes(payload)
(blob := hashlib.sha1(f'blob {len(payload)}\\0'.encode('ascii') + payload).hexdigest())
(a.out / 'SOURCE.json').write_text(json.dumps({
    'schema': 'emuflow.public-benchmark-fetch/v1',
    'commit': '1111111111111111111111111111111111111111',
    'case': a.case,
    'files': [{'name': 'design.net', 'bytes': 3, 'git_blob_sha1': blob, 'status': 'downloaded'}],
}))
""",
            encoding="utf-8",
        )
        matrix = {
            "schema": "emuflow.contest-validation-matrix/v1",
            "cases": [
                {
                    "id": "eda2023.case1",
                    "suite": "eda2023",
                    "case": "case1",
                    "source": {
                        "fetcher": "scripts/fetch_fixture.py",
                        "revision_kind": "git-commit",
                        "revision": revision,
                    },
                    "input_bytes": 3,
                    "tier": "smoke",
                    "qualification": "catalogued",
                    "target_gates": ["fetch", "import"],
                    "evidence": [],
                }
            ],
        }
        matrix_path = root / "matrix.json"
        matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
        return matrix_path, root

    def test_fetch_runs_pinned_script_and_checks_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix, fetcher_root = self._fixture(root)
            report = fetch_public_contest_case(
                matrix,
                "eda2023.case1",
                root / "run",
                fetcher_root=fetcher_root,
            )
            self.assertEqual(report["schema"], PUBLIC_CONTEST_FETCH_REPORT_SCHEMA)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["validation"]["input_bytes"], 3)
            self.assertEqual(
                read_json(root / "run" / "fetch_report.json"), report
            )

    def test_fetch_rejects_revision_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix, fetcher_root = self._fixture(root, revision="2" * 40)
            with self.assertRaisesRegex(ValidationError, "revision"):
                fetch_public_contest_case(
                    matrix,
                    "eda2023.case1",
                    root / "run",
                    fetcher_root=fetcher_root,
                )



    def test_fetch_rejects_replaced_farm_bound_ssl_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix, fetcher_root = self._fixture(root)
            certificate = root / "ca-bundle.crt"
            certificate.write_bytes(b"trusted\n")
            environment = {
                "SSL_CERT_FILE": str(certificate.resolve()),
                "EMUFLOW_SSL_CERT_SHA256": hashlib.sha256(
                    certificate.read_bytes()
                ).hexdigest(),
            }
            certificate.write_bytes(b"replaced\n")
            with patch.dict("os.environ", environment, clear=False):
                with self.assertRaisesRegex(ValidationError, "SHA256 mismatch"):
                    fetch_public_contest_case(
                        matrix,
                        "eda2023.case1",
                        root / "run",
                        fetcher_root=fetcher_root,
                    )

    def test_fetch_records_valid_farm_bound_ssl_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix, fetcher_root = self._fixture(root)
            certificate = root / "ca-bundle.crt"
            certificate.write_bytes(b"trusted\n")
            digest = hashlib.sha256(certificate.read_bytes()).hexdigest()
            with patch.dict(
                "os.environ",
                {
                    "SSL_CERT_FILE": str(certificate.resolve()),
                    "EMUFLOW_SSL_CERT_SHA256": digest,
                },
                clear=False,
            ):
                report = fetch_public_contest_case(
                    matrix,
                    "eda2023.case1",
                    root / "run",
                    fetcher_root=fetcher_root,
                )
            self.assertEqual(
                report["transport_security"],
                {"provider": "farm-bound-ca-bundle", "sha256": digest},
            )

    def test_unified_import_dispatches_all_public_suite_adapters(self) -> None:
        for suite in ("eda2023", "eda2024-repart", "eda2025"):
            with self.subTest(suite=suite), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                matrix, source = self._semantic_fixture(root, suite)
                report = import_public_contest_case(
                    matrix, f"{suite}.case1", source, root / "normalized"
                )
                self.assertEqual(report["schema"], PUBLIC_CONTEST_IMPORT_REPORT_SCHEMA)
                self.assertEqual(report["status"], "pass")
                self.assertEqual(report["suite"], suite)
                self.assertEqual(report["evaluation_status"], "not-run")
                self.assertTrue(report["artifacts"])
                self.assertEqual(
                    read_json(root / "normalized" / "import_report.json"), report
                )

    def test_import_rejects_tampered_fetched_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix, source = self._semantic_fixture(root, "eda2024-repart")
            (source / "design.net").write_text("a 4 b\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "digest"):
                import_public_contest_case(
                    matrix, "eda2024-repart.case1", source, root / "normalized"
                )

    def test_eda2025_evaluation_bundle_is_replayable_and_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix, source = self._semantic_fixture(root, "eda2025")
            normalized = root / "normalized"
            import_public_contest_case(
                matrix, "eda2025.case1", source, normalized
            )
            routes = root / "routes.json"
            write_json(
                routes,
                {
                    "schema": "emuflow.system-routes/v1",
                    "routes": [
                        {
                            "net": "net_000001",
                            "source": "F1",
                            "sinks": ["F2"],
                            "tree_edges": [{"from": "F1", "to": "F2"}],
                        }
                    ],
                },
            )
            bundle = root / "evaluation"
            routes_sha256 = hashlib.sha256(routes.read_bytes()).hexdigest()
            report = evaluate_public_contest_case(
                matrix,
                "eda2025.case1",
                source,
                normalized,
                routes,
                bundle,
                runtime_seconds=2.5,
                expected_routes_sha256=routes_sha256,
            )
            self.assertEqual(
                report["schema"], PUBLIC_CONTEST_EVALUATION_REPORT_SCHEMA
            )
            self.assertEqual(report["metrics"]["routed_cut_nets"], 1)
            validation = validate_public_contest_evaluation(matrix, bundle)
            self.assertEqual(validation["status"], "pass")
            self.assertEqual(
                main(
                    [
                        "contest",
                        "validate-public-evaluation",
                        "--matrix",
                        str(matrix),
                        str(bundle),
                    ]
                ),
                0,
            )
            self.assertGreaterEqual(validation["artifacts_verified"], 10)
            self.assertTrue((bundle / "official" / "design.route.out").is_file())

            legacy = root / "legacy-evaluation"
            shutil.copytree(bundle, legacy)
            legacy_report = read_json(legacy / "evaluation_report.json")
            legacy_report["schema"] = PUBLIC_CONTEST_EVALUATION_REPORT_SCHEMA_V1
            legacy_report["candidate"] = {
                key: legacy_report["candidate"][key]
                for key in ("routes_sha256", "new_topology_sha256")
            }
            write_json(legacy / "evaluation_report.json", legacy_report)
            self.assertEqual(
                validate_public_contest_evaluation(matrix, legacy)["status"],
                "pass",
            )

            mixed = root / "mixed-evaluation"
            shutil.copytree(bundle, mixed)
            mixed_instance_path = mixed / "import" / "contest_instance.json"
            mixed_import_report_path = mixed / "import" / "import_report.json"
            mixed_instance = read_json(mixed_instance_path)
            mixed_instance["name"] = "cross-run-instance"
            write_json(mixed_instance_path, mixed_instance)
            mixed_import_report = read_json(mixed_import_report_path)
            instance_record = next(
                record
                for record in mixed_import_report["artifacts"]
                if record["path"] == "contest_instance.json"
            )
            instance_record["bytes"] = mixed_instance_path.stat().st_size
            instance_record["sha256"] = hashlib.sha256(
                mixed_instance_path.read_bytes()
            ).hexdigest()
            write_json(mixed_import_report_path, mixed_import_report)
            mixed_report_path = mixed / "evaluation_report.json"
            mixed_report = read_json(mixed_report_path)
            mixed_report["upstream"]["import_report_sha256"] = hashlib.sha256(
                mixed_import_report_path.read_bytes()
            ).hexdigest()
            for relative, path in {
                "import/contest_instance.json": mixed_instance_path,
                "import/import_report.json": mixed_import_report_path,
            }.items():
                record = next(
                    item
                    for item in mixed_report["artifacts"]
                    if item["path"] == relative
                )
                record["bytes"] = path.stat().st_size
                record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            write_json(mixed_report_path, mixed_report)
            with self.assertRaisesRegex(ValidationError, "semantic import replay"):
                validate_public_contest_evaluation(matrix, mixed)

            unsafe = root / "unsafe-evaluation"
            shutil.copytree(bundle, unsafe)
            unsafe_report = read_json(unsafe / "evaluation_report.json")
            unsafe_report["artifacts"][0]["path"] = "../outside"
            write_json(unsafe / "evaluation_report.json", unsafe_report)
            with self.assertRaisesRegex(ValidationError, "path is invalid"):
                validate_public_contest_evaluation(matrix, unsafe)

            file_link = root / "file-link-evaluation"
            shutil.copytree(bundle, file_link)
            outside_routes = root / "outside-routes.json"
            shutil.copy2(bundle / "candidate" / "routes.json", outside_routes)
            (file_link / "candidate" / "routes.json").unlink()
            (file_link / "candidate" / "routes.json").symlink_to(outside_routes)
            with self.assertRaisesRegex(ValidationError, "path is unsafe"):
                validate_public_contest_evaluation(matrix, file_link)

            directory_link = root / "directory-link-evaluation"
            shutil.copytree(bundle, directory_link)
            outside_candidate = root / "outside-candidate"
            shutil.copytree(bundle / "candidate", outside_candidate)
            shutil.rmtree(directory_link / "candidate")
            (directory_link / "candidate").symlink_to(
                outside_candidate, target_is_directory=True
            )
            with self.assertRaisesRegex(ValidationError, "path is unsafe"):
                validate_public_contest_evaluation(matrix, directory_link)

            with self.assertRaisesRegex(ValidationError, "overlaps"):
                evaluate_public_contest_case(
                    matrix,
                    "eda2025.case1",
                    source,
                    normalized,
                    routes,
                    normalized / "evaluation",
                )
            with self.assertRaisesRegex(ValidationError, "finite"):
                evaluate_public_contest_case(
                    matrix,
                    "eda2025.case1",
                    source,
                    normalized,
                    routes,
                    root / "nonfinite-evaluation",
                    runtime_seconds=float("nan"),
                )
            routes.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "frozen candidate"):
                evaluate_public_contest_case(
                    matrix,
                    "eda2025.case1",
                    source,
                    normalized,
                    routes,
                    root / "changed-candidate-evaluation",
                    expected_routes_sha256=routes_sha256,
                )

            (bundle / "candidate" / "routes.json").write_text(
                "{}\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "seal is broken"):
                validate_public_contest_evaluation(matrix, bundle)

    def test_eda2023_and_eda2024_use_the_same_sealed_evaluation_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            case23 = root / "case23"
            case23.mkdir()
            matrix23, source23 = self._semantic_fixture(case23, "eda2023")
            normalized23 = root / "case23" / "normalized"
            import_public_contest_case(
                matrix23, "eda2023.case1", source23, normalized23
            )
            routes23 = root / "case23" / "routes.json"
            write_json(
                routes23,
                {
                    "schema": "emuflow.system-routes/v1",
                    "routes": [
                        {
                            "net": "net_0000000",
                            "source": "Die0",
                            "sinks": ["Die2"],
                            "tree_edges": [
                                {
                                    "from": "Die0",
                                    "to": "Die1",
                                    "link": "die_link_000_001",
                                },
                                {
                                    "from": "Die1",
                                    "to": "Die2",
                                    "link": "die_link_001_002",
                                },
                            ],
                        },
                        {
                            "net": "net_0000002",
                            "source": "Die1",
                            "sinks": ["Die3"],
                            "tree_edges": [
                                {
                                    "from": "Die1",
                                    "to": "Die2",
                                    "link": "die_link_001_002",
                                },
                                {
                                    "from": "Die2",
                                    "to": "Die3",
                                    "link": "die_link_002_003",
                                },
                            ],
                        },
                    ],
                },
            )
            tdm23 = root / "case23" / "tdm_plan.json"
            write_json(
                tdm23,
                {
                    "schema": "emuflow.contest-eda2023-tdm/v1",
                    "instance": "eda2023-case1",
                    "provider": "cpp-lagrangian-kkt-direction-separated-v1",
                    "hops": [
                        {
                            "index": 0,
                            "net": "net_0000000",
                            "official_net_id": 0,
                            "link": "die_link_001_002",
                            "direction": 0,
                            "from": "Die1",
                            "to": "Die2",
                            "ratio": 4,
                            "lane": 0,
                        },
                        {
                            "index": 1,
                            "net": "net_0000002",
                            "official_net_id": 2,
                            "link": "die_link_001_002",
                            "direction": 0,
                            "from": "Die1",
                            "to": "Die2",
                            "ratio": 4,
                            "lane": 0,
                        },
                    ],
                },
            )
            bundle23 = root / "case23" / "evaluation"
            report23 = evaluate_public_contest_case(
                matrix23,
                "eda2023.case1",
                source23,
                normalized23,
                routes23,
                bundle23,
                tdm_plan_path=tdm23,
                expected_routes_sha256=hashlib.sha256(
                    routes23.read_bytes()
                ).hexdigest(),
                expected_tdm_plan_sha256=hashlib.sha256(
                    tdm23.read_bytes()
                ).hexdigest(),
            )
            self.assertEqual(report23["metrics"]["max_tdm_ratio"], 4)
            self.assertEqual(
                validate_public_contest_evaluation(matrix23, bundle23)["status"],
                "pass",
            )
            self.assertTrue(
                (bundle23 / "official" / "design.tdm.out").is_file()
            )

            case24 = root / "case24"
            case24.mkdir()
            matrix24, source24 = self._semantic_fixture(
                case24, "eda2024-repart"
            )
            normalized24 = root / "case24" / "normalized"
            import_public_contest_case(
                matrix24, "eda2024-repart.case1", source24, normalized24
            )
            solution24 = root / "case24" / "design.fpga.out"
            solution24.write_text("F1: a\nF2: b\n", encoding="utf-8")
            bundle24 = root / "case24" / "evaluation"
            report24 = evaluate_public_contest_case(
                matrix24,
                "eda2024-repart.case1",
                source24,
                normalized24,
                None,
                bundle24,
                solution_path=solution24,
                runtime_seconds=3.0,
                expected_solution_sha256=hashlib.sha256(
                    solution24.read_bytes()
                ).hexdigest(),
            )
            self.assertEqual(report24["metrics"]["total_hop_distance"], 3)
            self.assertEqual(
                validate_public_contest_evaluation(matrix24, bundle24)["status"],
                "pass",
            )
            (bundle24 / "candidate" / "design.fpga.out").write_text(
                "F1: a b\nF2:\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "seal is broken"):
                validate_public_contest_evaluation(matrix24, bundle24)

    def test_unified_boarddb_gate_revalidates_import_and_projects_topology(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix, source = self._semantic_fixture(root, "eda2024-repart")
            normalized = root / "normalized"
            import_public_contest_case(
                matrix, "eda2024-repart.case1", source, normalized
            )
            repository = Path(__file__).resolve().parents[1]
            report = materialize_public_contest_boarddb(
                matrix,
                "eda2024-repart.case1",
                source,
                normalized,
                repository / "platforms/virtual/academic_vtr_4fpga_mesh.json",
                root / "boarddb",
                unweighted_link_lanes=4,
            )
            self.assertEqual(report["schema"], PUBLIC_CONTEST_BOARDDB_REPORT_SCHEMA)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["qualification"], "academic-architecture-projection")
            self.assertEqual(report["phase3_status"], "not-run")
            boarddb = read_json(root / "boarddb" / "boarddb.json")
            self.assertEqual(
                [fpga["id"] for fpga in boarddb["fpgas"]], ["F1", "F2"]
            )
            self.assertEqual(boarddb["links"][0]["data_lanes_per_direction"], 4)
            self.assertTrue((root / "boarddb" / "route_constraints.json").is_file())

    def test_unified_boarddb_gate_projects_eda2023_physical_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix, source = self._semantic_fixture(root, "eda2023")
            normalized = root / "normalized"
            import_public_contest_case(
                matrix, "eda2023.case1", source, normalized
            )
            repository = Path(__file__).resolve().parents[1]
            report = materialize_public_contest_boarddb(
                matrix,
                "eda2023.case1",
                source,
                normalized,
                repository / "platforms/virtual/academic_vtr_4fpga_mesh.json",
                root / "boarddb",
            )
            self.assertEqual(report["status"], "pass")
            constraints = read_json(root / "boarddb" / "route_constraints.json")
            boarddb = read_json(root / "boarddb" / "boarddb.json")
            self.assertEqual(
                constraints["shared_capacity_links"],
                [link["id"] for link in boarddb["links"]],
            )
            self.assertEqual(constraints["max_route_hops"], 1)

    def test_unified_boarddb_gate_rejects_tampered_import_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix, source = self._semantic_fixture(root, "eda2024-repart")
            normalized = root / "normalized"
            import_public_contest_case(
                matrix, "eda2024-repart.case1", source, normalized
            )
            (normalized / "contest_instance.json").write_text("{}\n", encoding="utf-8")
            repository = Path(__file__).resolve().parents[1]
            with self.assertRaisesRegex(ValidationError, "seal"):
                materialize_public_contest_boarddb(
                    matrix,
                    "eda2024-repart.case1",
                    source,
                    normalized,
                    repository / "platforms/virtual/academic_vtr_4fpga_mesh.json",
                    root / "boarddb",
                )






if __name__ == "__main__":
    unittest.main()
