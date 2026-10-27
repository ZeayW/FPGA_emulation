"""Small current typed Static Exact inputs for publication failure tests."""
from pathlib import Path

from tests.native_route_seal_fixture import write_route_store_fixture
from tests.phase3_store_fixture import write_current_assignment_fixture
from tests.test_combinational_cut import (
    PLATFORM_PATH, StaticExactCombinationalCutPartitionTest,
)


def write_native_phase5_fixture(root: Path, *, dependent_return=False, prefix=""):
    case = StaticExactCombinationalCutPartitionTest()
    case.setUp()
    _, assignment = case._exact_artifacts(dependent_return=dependent_return)
    routes = case._persisted_routes(case._exact_routes(assignment))
    root = root / prefix.rstrip("-") if prefix else root
    root.mkdir(parents=True, exist_ok=True)
    routes_path = root / "routes.json"
    write_route_store_fixture(routes_path, routes)
    assignment_path = root / "assignment.json"
    write_current_assignment_fixture(assignment_path, case.ir, assignment)
    return routes_path, PLATFORM_PATH, assignment_path
