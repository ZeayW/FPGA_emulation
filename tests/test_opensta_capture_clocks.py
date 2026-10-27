import os

import pytest

from scripts.validation.qualify_opensta_capture_clocks import qualify


@pytest.mark.skipif(not os.environ.get('EMUFLOW_TEST_OPENSTA'), reason='real OpenSTA not configured')
def test_setup_arcs_and_clock_tags_cover_every_capture_clock_without_data_path_selection(tmp_path):
    report = qualify(os.environ['EMUFLOW_TEST_OPENSTA'], tmp_path/'capture-clocks')
    assert report['status'] == 'pass'
    assert report['endpoints'] == 7 and report['clock_facts'] == 8
    assert report['rejected_queries'] == 7
    assert report['selected_data_paths'] is False
