import os

from logikbench.apps.lb import present_value
from logikbench.tools.resources import ProcessRSS


def test_process_rss_is_nonzero():
    # Querying our own resident pages needs no privileged macOS USS access.
    value = ProcessRSS()._Task__collect_memory(os.getpid())
    assert value is not None and value > 0


def test_unmeasured_memory_is_missing():
    assert present_value('memory', 0) is None
    assert present_value('memory', 123_000_000) == 123.0


def test_scheduler_ignores_failed_memory_samples(tmp_path, monkeypatch):
    import json
    from logikbench.apps import lb
    results = tmp_path / 'syn' / 'asic'
    results.mkdir(parents=True)
    for target, memory in [('yosys_asap7', 0), ('yosys_sky130', 512)]:
        (results / f'{target}.json').write_text(json.dumps({
            'metrics': {'memory': {'basic': {'mux': memory, 'unknown': 0}}}}))
    monkeypatch.setattr(lb, 'find_results_tree', lambda: str(tmp_path))
    _, estimate, _ = lb._load_estimates(['yosys_asap7', 'yosys_sky130'])
    assert estimate('yosys_asap7', 'basic', 'mux') == 512
    assert estimate('yosys_asap7', 'basic', 'unknown') is None


def test_sc_memory_sampling_hook_is_supported():
    from siliconcompiler import Task
    assert '_Task__collect_memory' in Task.__dict__


def test_resume_accepts_deliberately_skipped_timing(tmp_path):
    import json
    from logikbench.common import is_complete
    path = tmp_path / 'design' / 'job0'
    path.mkdir(parents=True)
    statuses = {'synthesis': {'0': {'value': 'success'}},
                'timing': {'0': {'value': 'skipped'}}}
    manifest = path / 'design.pkg.json'
    manifest.write_text(json.dumps({'record': {'status': {'node': statuses}}}))
    assert is_complete('design', str(tmp_path))
    statuses['timing']['0']['value'] = 'pending'
    manifest.write_text(json.dumps({'record': {'status': {'node': statuses}}}))
    assert not is_complete('design', str(tmp_path))


def test_short_compiler_peak_survives_sparse_sc_samples(tmp_path):
    import json
    from logikbench.common import read_metrics
    job = tmp_path / 'short' / 'job0'
    reports = job / 'synthesis' / '0' / 'reports'
    reports.mkdir(parents=True)
    (job / 'short.pkg.json').write_text(json.dumps({
        'metric': {'memory': {'node': {'synthesis': {'0': {'value': 20_000_000}}}}}}))
    (reports / 'process_resources.json').write_text(json.dumps({'peak_child_rss_bytes': 123_000_000}))
    assert read_metrics('short', ['memory'], str(tmp_path))['memory'] == 123_000_000
