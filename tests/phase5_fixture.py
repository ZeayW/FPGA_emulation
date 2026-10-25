"""Small current typed Static Exact inputs for publication failure tests."""
import json
from pathlib import Path

from emuflow.system_route_storage import write_system_route_storage
from tests.test_combinational_cut import (
    PLATFORM_PATH, StaticExactCombinationalCutPartitionTest,
)


def write_native_phase5_fixture(root: Path, *, dependent_return=False, prefix=""):
    case = StaticExactCombinationalCutPartitionTest()
    case.setUp()
    _, assignment = case._exact_artifacts(dependent_return=dependent_return)
    routes = case._persisted_routes(case._exact_routes(assignment))
    routes_path = root / f"{prefix}routes.json"
    write_system_route_storage(routes_path, routes)
    assignment_path = root / f"{prefix}assignment.json"
    assignment_path.write_text(json.dumps(assignment), encoding="utf-8")
    return routes_path, PLATFORM_PATH, assignment_path
