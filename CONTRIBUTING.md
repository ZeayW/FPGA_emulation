# Contributing to EmuFlow

Contributions should preserve circuit semantics, make provider assumptions
explicit, and keep the normal flow practical on large designs.

## Change scope

- Keep changes focused and describe the affected interface, default, or
  algorithm in the pull request.
- Preserve unrelated work and upstream source/licensing notices.
- Do not publish personal environment paths, raw tool reports, credentials,
  chat transcripts, experiment histories, or private board/IP data.
- Update README when the project entry points or scope change. Detailed
  commands and contracts belong in the user and architecture documentation,
  not a running development diary.

## Building and testing

Create an isolated environment for regression dependencies:

```bash
python3 -m venv .venv-tests
. .venv-tests/bin/activate
```

For Linux CPU tests, install the CPU PyTorch wheel before the test extra:
`python -m pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cpu`.
CI uses Python 3.12 and this CPU configuration.

Then install the test extra:

```bash
python -m pip install -e '.[test]'
```

The complete build is the root `release` preset. Use the test environment's
interpreter for CMake so CTest can run the same dependencies:

```bash
cmake --preset release -DPython3_EXECUTABLE="$(command -v python)"
cmake --build --preset release --parallel 2
ctest --preset release
```

CTest runs the complete pytest suite, including the unittest classes. To run
only the Python regression entry directly:

```bash
python -m pytest -q -W error::pytest.PytestUnraisableExceptionWarning
```

Verify the distributable package independently of the source checkout:

```bash
python -m pip wheel --no-deps --wheel-dir build/wheels .
python tests/check_python_distribution.py . build/wheels/emuflow-*.whl
```

The source audit checks that implementation sources and provenance are present:

```bash
python3 scripts/check_source_complete.py .
git diff --check
```

A focused test is appropriate during development. State exactly which tests
were run and which native or optional-provider gates were not exercised.
Documentation-only edits require command, link, and consistency checks;
they do not require repeating synthesis or physical implementation.

## APIs, artifacts, and defaults

A current producer has one documented default across CLI, API, and configuration
entry points. Expose maintained algorithms and remove replaced implementations
and selectors. Do not infer historical behavior from omitted parameters.

Keep source/device, instance, net, route, schedule-entry, and original-path
identities consistent between phases. Introduce a new schema or optional field
when a contract changes, and test unsupported versions and mixed inputs.
The ability to interpret a historical result is not a reason to keep an
obsolete producer or ambiguous default.

Use one canonical owner for each large data population. Downstream consumers
read the records they need instead of copying graphs into reports. Storage-only
changes must preserve logical data and timing, with round-trip and malformed-
input regressions. Add a realistically sized test before claiming a
performance improvement.

## Algorithm changes

Distinguish direct upstream integration, faithful paper reproduction, and
project-specific extensions. Do not describe a familiar heuristic as a
state-of-the-art reproduction without supporting evidence.

Partitioning enforces structural constraints such as resources, fixed/group
ownership, reachability, and hop limits. Predicted congestion or transport
delay can be optimization costs, but they are not downstream scheduling or
physical-correctness certificates. Routing, TDM, transport binding, and physical
implementation own their respective decisions.

Use small exact or exhaustive references to qualify algorithms where feasible.
Do not put full optimizer replay, candidate enumeration, or detailed search
traces in a production large-design hot path.

## Validation and comparisons

Follow [the validation requirements](docs/VALIDATION.md). A provider or default
change needs the gates appropriate to its claim, not just an intermediate
objective or test-double result. Performance and QoR comparisons must state
their workload, platform, source/tool versions, configuration, seed, coverage,
and metric definition.

Do not create feature evidence by forcing an assignment or fabricating physical
data. A small functional fixture is not evidence of large-design QoR, and a
zero-combinational-cut flow does not exercise Static Exact.

## Experiment outputs

Keep machine-local execution data outside the source repository. Run experiments
in isolated directories without overwriting another task's inputs or outputs.
Treat intermediates as active-run scratch, not a permanent checkpoint archive.
Retain compact terminal summaries outside the repository after validation.

Before deleting scratch, confirm that its recorded process tree and consumers
have stopped. A disconnected observer is not proof that a job failed.
Published reports must distinguish supported behavior, incomplete validation,
model-level assumptions, and measured hardware evidence.
