"""Raw differential fixtures enter the native qualification through typed stores."""
from contextlib import ExitStack, contextmanager
from pathlib import Path
import tempfile
from emuflow_tools.transport.chimew import chimew_qualification as production
from emuflow_tools.transport.chimew.chimew_bank_channel_storage import BankChannelInputView, write_chimew_bank_channel_storage
from emuflow_tools.transport.chimew.chimew_bank_channel_report_storage import BankChannelReportView
from tests.chimew_bank_report_fixture import write_chimew_bank_channel_report_storage


@contextmanager
def typed_pair(bank_input, report):
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
        root = Path(temporary)
        left = bank_input if isinstance(bank_input, BankChannelInputView) else stack.enter_context(
            write_chimew_bank_channel_storage(root / 'input.json', bank_input))
        right = report if isinstance(report, BankChannelReportView) else stack.enter_context(
            write_chimew_bank_channel_report_storage(root / 'report.json', report))
        yield left, right


def validate_chimew_bank_channel_report_artifact(bank_input, report):
    with typed_pair(bank_input, report) as (left, right):
        return production.validate_chimew_bank_channel_report_artifact(left, right)


@contextmanager
def chain_paths(documents):
    import json
    names = ("schedule", "crossings", "initial_grouping", "positions", "refined_grouping",
             "rudy_input", "rudy_report", "bank_channel_input", "bank_channel_report")
    if len(documents) != len(names):
        raise TypeError("all nine qualification artifacts are required")
    with tempfile.TemporaryDirectory() as temporary, typed_pair(documents[-2], documents[-1]) as (left, right):
        root = Path(temporary)
        paths = {"bank_channel_input": left.manifest_path, "bank_channel_report": right.manifest_path}
        for name, document in zip(names[:-2], documents[:-2]):
            existing = getattr(document, "manifest_path", None)
            if existing is not None:
                paths[name] = existing
            else:
                path = root / (name + ".json")
                path.write_text(json.dumps(document), encoding="utf-8")
                paths[name] = path
        yield paths, root / "qualification.json"


def build_chimew_phase6_qualification(*documents, source_binding=None):
    with chain_paths(documents) as (paths, output):
        return production.qualify_chimew_artifact_chain(paths, output, source_binding=source_binding)


def validate_chimew_phase6_qualification(certificate, *documents, source_binding=None):
    with chain_paths(documents) as (paths, output):
        return production.validate_chimew_phase6_qualification(certificate, paths, source_binding=source_binding)
