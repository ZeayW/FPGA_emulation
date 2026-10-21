"""Explicit, sealed completed native PATRON result handoff (not a phase pass)."""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path

from .errors import ValidationError
from .io import read_json

SCHEMA = "emuflow.completed-patron-result/v1"


def _require(condition, message):
    if not condition:
        raise ValidationError("completed PATRON result: " + message)


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def order_digest(values):
    digest = hashlib.sha256()
    for value in values:
        digest.update(json.dumps(value, ensure_ascii=True).encode() + b"\n")
    return digest.hexdigest()


def file_record(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": file_digest(path)}


def _file(record):
    _require(isinstance(record, dict) and isinstance(record.get("path"), str), "missing file binding")
    path = Path(record["path"])
    _require(path.is_absolute() and path.is_file(), "missing absolute file binding")
    _require(file_digest(path) == record.get("sha256"), "file digest mismatch: " + str(path))
    return path


def validate_mapping_producer(commit):
    """Keep the old producer's clustering/index/model dependencies unchanged."""
    root = Path(__file__).resolve().parents[2]
    names = subprocess.check_output(["git", "-C", str(root), "ls-tree", "-r", "--name-only",
                                     commit, "src/emuflow"], text=True).splitlines()
    names = [name for name in names if name.endswith(".py")]
    objects = subprocess.check_output(["git", "-C", str(root), "cat-file", "--batch"],
                                     input="".join(f"{commit}:{n}\n" for n in names).encode())
    permitted = {"src/emuflow/multi_fpga_flow.py": "run_multi_fpga_flow",
                 "src/emuflow/phase3.py": "run_phase3",
                 "src/emuflow/partition_pressure.py": "run_partition_pressure_native"}
    offset = 0
    for name in names:
        end = objects.index(b"\n", offset)
        size = int(objects[offset:end].split()[2])
        original = objects[end+1:end+1+size]
        offset = end+size+2
        current = (root/name).read_bytes()
        if current == original:
            continue
        def retained(source):
            tree = ast.parse(source)
            tree.body = [n for n in tree.body if not
                         (isinstance(n, ast.FunctionDef) and n.name == permitted.get(name))]
            return ast.dump(tree, include_attributes=False)
        _require(name in permitted and retained(original) == retained(current),
                 "mapping producer dependency changed: " + name)
    current_names = {str(p.relative_to(root)) for p in (root/'src/emuflow').rglob('*.py')}
    _require(current_names - set(names) <= {"src/emuflow/patron_result.py"},
             "mapping producer gained an unaudited dependency")


def validate_completed_result(path):
    value = read_json(path)
    _require(value.get("schema") == SCHEMA, "unsupported manifest")
    execution = value.get("execution", {})
    _require(execution.get("status") == "completed" and type(execution.get("exit_code")) is int
             and execution["exit_code"] == 0 and execution.get("termination_reason") is None,
             "native execution did not complete successfully")
    for key in ("input", "output", "binary", "native_source", "receipt"):
        _file(value.get(key))
    _require(Path(value["output"]["path"]).stat().st_size > 0, "empty native output")
    commit = value.get("producer_commit", "")
    _require(len(commit) == 40 and all(c in "0123456789abcdef" for c in commit), "invalid producer commit")
    _require(value.get("mapping", {}).get("policy") == "sorted-model-cluster-fpga/v1", "missing index mapping")
    for key in ("clusters_sha256", "parts_sha256"):
        digest = value["mapping"].get(key, "")
        _require(len(digest) == 64 and all(c in "0123456789abcdef" for c in digest), "invalid mapping digest")
    command = execution.get("command", [])
    _require(isinstance(command, list) and len(command) >= 3, "missing executed command")
    _require(Path(command[0]).resolve() == Path(value["binary"]["path"])
             and Path(command[-2]).resolve() == Path(value["input"]["path"])
             and Path(command[-1]).resolve() == Path(value["output"]["path"]), "executed command binding differs")
    _require(set(command[1:-2]) <= {"--progress", "--summary-output"}, "unsupported native command flags")
    receipt = read_json(Path(value["receipt"]["path"]))
    _require(type(receipt.get("exit_code")) is int and receipt["exit_code"] == 0
             and receipt.get("status") in {"pass", "completed"}
             and not receipt.get("termination_reason") and not receipt.get("error"),
             "receipt is partial or terminated")
    _require(receipt.get("command") == command, "receipt command differs")
    _require(isinstance(value.get("context"), dict) and value["context"], "missing input context")
    source = subprocess.run(["git", "-C", str(Path(__file__).resolve().parents[2]),
        "show", commit + ":src/native/patron_refiner.cpp"], capture_output=True, check=False)
    _require(source.returncode == 0 and hashlib.sha256(source.stdout).hexdigest()
             == value["native_source"]["sha256"], "native source does not match producer commit")
    validate_mapping_producer(commit)
    return value


def validate_context(value, **paths):
    checked = {key: _file(record) for key, record in value["context"].items()}
    if "timing" in paths:
        manifest = read_json(paths["timing"])
        for key, record in manifest.get("payloads", {}).items():
            paths["timing_payload_" + key] = Path(paths["timing"]).parent / record["path"]
    for key, path in paths.items():
        record = value["context"].get(key)
        _require(record is not None and (Path(path).resolve() == checked[key].resolve()
                 or file_digest(path) == record["sha256"]), "context differs: " + key)


def recover_initial_mapping(value, clusters, parts):
    clusters, parts = sorted(clusters), sorted(parts)
    _require(order_digest(clusters) == value["mapping"]["clusters_sha256"]
             and order_digest(parts) == value["mapping"]["parts_sha256"], "cluster/part order differs")
    result = {}
    with Path(value["input"]["path"]).open() as stream:
        for line in stream:
            fields = line.split()
            if fields and fields[0] == "CLUSTER":
                index, part = int(fields[1]), int(fields[2])
                _require(0 <= index < len(clusters) and 0 <= part < len(parts), "initial index out of bounds")
                _require(clusters[index] not in result, "duplicate initial cluster")
                result[clusters[index]] = parts[part]
    _require(len(result) == len(clusters), "incomplete initial mapping")
    return result


def verify_native_input(value, native_input, indexes):
    _require(file_digest(native_input) == value["input"]["sha256"], "regenerated native input differs")
    _require(order_digest(indexes["clusters"]) == value["mapping"]["clusters_sha256"]
             and order_digest(indexes["parts"]) == value["mapping"]["parts_sha256"], "native index mapping differs")
    _file(value["output"])


def result_provenance(path, value):
    return {"manifest": str(Path(path).resolve()), "manifest_sha256": file_digest(path),
            "producer_commit": value["producer_commit"],
            "binary_sha256": value["binary"]["sha256"],
            "native_source_sha256": value["native_source"]["sha256"],
            "input_sha256": value["input"]["sha256"], "output_sha256": value["output"]["sha256"],
            "mapping": value["mapping"], "qualification": "completed-native-result-only"}


def seal_completed_result(*, native_input, native_output, binary, native_source,
                          receipt, producer_commit, clusters, parts, context):
    """Publish a handoff after rebuilding index names from the sealed producer.

    The caller supplies the producer's generalized cluster names, not a final
    assignment. The consumer regenerates the full native input before reuse.
    """
    original = read_json(receipt)
    value = {"schema": SCHEMA, "producer_commit": producer_commit,
             "input": file_record(native_input), "output": file_record(native_output),
             "binary": file_record(binary), "native_source": file_record(native_source),
             "receipt": file_record(receipt),
             "execution": {"status": "completed", "exit_code": original.get("exit_code"),
                           "termination_reason": original.get("termination_reason"),
                           "command": original.get("command")},
             "mapping": {"policy": "sorted-model-cluster-fpga/v1",
                         "clusters_sha256": order_digest(sorted(clusters)),
                         "parts_sha256": order_digest(sorted(parts))},
             "context": {key: file_record(path) for key, path in context.items()}}
    return value
