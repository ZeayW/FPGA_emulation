"""Load the calling EmuFlow package under OpenPARF's own interpreter."""

import os
import sys

# A directly executed package file puts this directory first on sys.path.
# Its openparf.py and platform.py must not shadow the runtime's dependencies.
package_dir = os.path.dirname(os.path.realpath(__file__))
sys.path[:] = [
    path for path in sys.path
    if os.path.realpath(path or os.curdir) != package_dir
]

import importlib.util
import runpy


def main():
    drivers = {"openparf_native_driver", "openparf_continuous_driver"}
    if len(sys.argv) < 2 or sys.argv[1] not in drivers:
        raise SystemExit("expected an EmuFlow OpenPARF driver name")
    driver = sys.argv[1]
    spec = importlib.util.spec_from_file_location(
        "emuflow", os.path.join(package_dir, "__init__.py"),
        submodule_search_locations=[package_dir],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the calling EmuFlow package")
    package = importlib.util.module_from_spec(spec)
    sys.modules["emuflow"] = package
    spec.loader.exec_module(package)
    sys.argv = [driver, *sys.argv[2:]]
    runpy.run_module("emuflow." + driver, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
