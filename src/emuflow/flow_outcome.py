"""Keep completed analysis separate from its timing/coverage outcome."""

LEGACY_FLOW_SCHEMA = "emuflow.multi-fpga-flow/v2"
ANALYSIS_FLOW_SCHEMA = "emuflow.multi-fpga-flow/v3"
OUTCOMES = frozenset({"pass", "fail", "incomplete"})


def native_analysis_completed(runtime):
    return (
        isinstance(runtime, dict)
        and runtime.get("schema") == "emuflow.phase7c-report/v3"
        and runtime.get("increment") == "native-word-system-timing"
        and runtime.get("execution_status") == "pass"
        and isinstance(runtime.get("status"), str)
        and runtime.get("status") in OUTCOMES
    )


def flow_execution_completed(report):
    """A non-passing analysis stays non-passing after terminal publication."""
    if not isinstance(report, dict):
        return False
    if report.get("schema") == LEGACY_FLOW_SCHEMA:
        return report.get("status") == "pass"
    runtime = report.get("runtime")
    return (
        report.get("schema") == ANALYSIS_FLOW_SCHEMA
        and report.get("execution_status") == "pass"
        and native_analysis_completed(runtime)
        and report.get("status") == runtime["status"]
    )
