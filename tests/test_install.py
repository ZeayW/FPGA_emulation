import hashlib
import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "emuflow_source_installer", ROOT / "scripts/install.py"
)
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


def test_extract_rapidwright_runtime_verifies_and_installs_members(tmp_path, monkeypatch):
    intersite = b"intersite"
    intrasite = b"intrasite"
    members = {
        "timing/ultrascaleplus/intersite_delay_terms.txt": hashlib.sha256(
            intersite
        ).hexdigest(),
        "timing/ultrascaleplus/intrasite_delay_terms.txt": hashlib.sha256(
            intrasite
        ).hexdigest(),
    }
    monkeypatch.setattr(INSTALLER, "TIMING_MEMBERS", members)
    jar = tmp_path / "rapidwright.jar"
    with ZipFile(jar, "w") as archive:
        archive.writestr("data/parts.db", b"parts")
        archive.writestr(next(iter(members)), intersite)
        archive.writestr(list(members)[1], intrasite)
    root = tmp_path / "runtime"
    timing = INSTALLER.extract_rapidwright_runtime(jar, root)
    assert (root / "data/parts.db").read_bytes() == b"parts"
    assert (timing / "intersite_delay_terms.txt").read_bytes() == intersite
    assert (timing / "intrasite_delay_terms.txt").read_bytes() == intrasite


def test_verified_rapidwright_download_is_reused(tmp_path, monkeypatch):
    target = tmp_path / "rapidwright.jar"
    target.write_bytes(b"verified")
    monkeypatch.setattr(
        INSTALLER, "RAPIDWRIGHT_SHA256", hashlib.sha256(b"verified").hexdigest()
    )
    INSTALLER.download_rapidwright(target)
    assert target.read_bytes() == b"verified"


def test_launcher_links_point_at_installation(tmp_path, monkeypatch):
    installation = tmp_path / "install"
    (installation / "bin").mkdir(parents=True)
    for name in ("emuflow", "emuflow-native"):
        (installation / "bin" / name).write_text(name)
    monkeypatch.setattr(INSTALLER, "INSTALL", installation)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "home/.local/bin"))
    INSTALLER.link_launchers()
    for name in ("emuflow", "emuflow-native"):
        assert (tmp_path / "home/.local/bin" / name).resolve() == (
            installation / "bin" / name
        ).resolve()


def test_device_contract_generation_runs_the_complete_pipeline(tmp_path, monkeypatch):
    installation = tmp_path / "install"
    build = tmp_path / "build"
    provider_source = tmp_path / "provider.json"
    provider_source.write_text("{}")
    (installation / "bin").mkdir(parents=True)
    (installation / "bin/emuflow").write_text("launcher")
    monkeypatch.setattr(INSTALLER, "INSTALL", installation)
    monkeypatch.setattr(INSTALLER, "BUILD", build)
    monkeypatch.setattr(INSTALLER, "PROVIDER_SOURCE", provider_source)
    calls = []

    def fake_command(arguments, *, cwd=INSTALLER.ROOT, environment=None):
        arguments = [str(item) for item in arguments]
        calls.append(arguments)
        if any(item.endswith("DeviceResourcesExample") for item in arguments):
            (cwd / "xcvu19p.device").write_text("device")
        if "--output" in arguments:
            Path(arguments[arguments.index("--output") + 1]).write_text("output")
        elif "org.python.util.jython" in arguments:
            Path(arguments[-1]).write_text("regions")

    monkeypatch.setattr(INSTALLER, "command", fake_command)
    provider, architecture, constraints = INSTALLER.generate_device_contract(
        python=Path("/python"),
        java=Path("/java"),
        javac=Path("/javac"),
        jar=Path("/rapidwright.jar"),
        rapidwright_root=tmp_path / "rapidwright",
        java_heap="4g",
    )
    assert provider.read_text() == "{}"
    assert architecture.is_file() and constraints.is_file()
    assert any(
        any(item.endswith("DeviceResourcesExample") for item in call)
        for call in calls
    )
    assert any("import-rapidwright-device" in call for call in calls)
    assert any("merge-physical-regions" in call for call in calls)


def test_build_runtime_uses_one_python_for_launcher_and_openparf(monkeypatch):
    calls = []
    monkeypatch.setattr(
        INSTALLER,
        "command",
        lambda arguments, **kwargs: calls.append([str(item) for item in arguments]),
    )
    INSTALLER.build_runtime(Path("/runtime/python"), 3)
    assert "-DPython3_EXECUTABLE=/runtime/python" in calls[0]
    assert "-DEMUFLOW_OPENPARF_PYTHON=/runtime/python" in calls[0]
    assert calls[1][-2:] == ["--parallel", "3"]


def test_environment_contains_and_checks_both_available_backends(tmp_path, monkeypatch):
    installation = tmp_path / "install"
    (installation / "bin").mkdir(parents=True)
    (installation / "share/emuflow/device").mkdir(parents=True)
    (installation / "bin/emuflow").write_text("launcher")
    monkeypatch.setattr(INSTALLER, "INSTALL", installation)
    real_which = INSTALLER.shutil.which
    monkeypatch.setattr(
        INSTALLER.shutil,
        "which",
        lambda name: "/tools/vivado" if name == "vivado" else real_which(name),
    )
    calls = []

    def fake_command(arguments, *, cwd=INSTALLER.ROOT, environment=None):
        arguments = [str(item) for item in arguments]
        calls.append(arguments)
        if "build-xilinx-preplacement-timing" in arguments:
            Path(arguments[arguments.index("--output") + 1]).write_text("timing")

    monkeypatch.setattr(INSTALLER, "command", fake_command)
    runtime = tmp_path / "rapidwright"
    timing = runtime / "data/timing"
    timing.mkdir(parents=True)
    inputs = {}
    for name in ("provider", "architecture", "constraints", "rapidwright.jar"):
        inputs[name] = tmp_path / name
        inputs[name].write_text(name)
    manifest = INSTALLER.publish_environment(
        python=Path("/python"),
        java=Path("/java"),
        jar=inputs["rapidwright.jar"],
        rapidwright_root=runtime,
        timing_data=timing,
        provider=inputs["provider"],
        architecture=inputs["architecture"],
        constraints=inputs["constraints"],
    )
    profile = json.loads(manifest.read_text())
    assert set(profile["backends"]) == {"rapidwright", "vivado"}
    checks = [call for call in calls if "environment" in call and "check" in call]
    assert {call[-1] for call in checks} == {"rapidwright", "vivado"}
