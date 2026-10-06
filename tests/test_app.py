import os
import statistics

import pytest
from fastapi.testclient import TestClient
from backend.app import app, paired_statistics
from backend.engine import _window_units, summarize_rows

client = TestClient(app)


def test_health_and_native_metrics() -> None:
    assert client.get('/api/health').json()['native_windows'] is True
    payload = client.get('/api/metrics').json()
    assert payload['available_mb'] > 0
    assert payload['pressure']['state'] in {'nominal', 'elevated', 'critical'}


def test_capability_scan_and_exports() -> None:
    caps = client.get('/api/capabilities').json()
    assert caps['apis']['GlobalMemoryStatusEx'] is True
    assert caps['apis']['SetInformationJobObject'] is True
    for kind in ('csv', 'json', 'html', 'pdf'):
        assert client.get(f'/api/export/{kind}').status_code == 200
    assert client.get('/api/export/pdf').content.startswith(b'%PDF-1.4')


def test_presets_policies_and_input_validation() -> None:
    assert {p['id'] for p in client.get('/api/presets').json()} == {'idle', 'cpu', 'membw', 'io', 'mixed'}
    assert {'none', 'nice', 'static', 'adaptive', 'observe'} <= {p['id'] for p in client.get('/api/policies').json()}
    assert client.post('/api/experiments', json={'mode': 'baseline', 'preset': 'unknown'}).status_code == 400
    assert client.post('/api/experiments', json={'mode': 'unsafe'}).status_code == 400
    assert client.post('/api/experiments', json={'mode': 'optimized', 'policy': 'rm -rf'}).status_code == 400
    assert client.post('/api/paired-comparison', json={'policy': 'none'}).status_code == 400


def test_comparison_contract() -> None:
    c = client.get('/api/comparison').json()
    assert c['classification'] in {'IMPROVED', 'REGRESSED', 'APPROXIMATELY_UNCHANGED', 'INSUFFICIENT_DATA'}
    for key in ('absolute_difference_ms', 'percentage_difference', 'paired_repetitions', 'valid_paired_repetitions', 'paired_tests', 'action_summary', 'background_throughput_retained_median', 'preset', 'policy'):
        assert key in c


def test_paired_statistics_use_student_t_for_every_n() -> None:
    diffs = [-1.0, -0.8, -1.3, -0.9, -1.1, -0.7, -1.2, -1.0, -0.95, -1.05]
    result = paired_statistics(diffs)
    margin = 2.262 * statistics.stdev(diffs) / len(diffs) ** 0.5  # t(0.975, df=9)
    assert result['t_ci95_ms'][1] == pytest.approx(statistics.fmean(diffs) + margin, abs=1e-3)
    assert result['reliable'] and result['wilcoxon_p'] < 0.01
    assert paired_statistics([0.5, -0.4, 0.1, -0.2, 0.05])['reliable'] is False


def test_summary_quantiles_and_window_counting() -> None:
    rows = [{'sched': float(i), 'cpu': 0.0, 'mem': 0.0, 'io': 0.0} for i in range(1, 101)]
    s = summarize_rows(rows, 50.0, 5000.0)
    assert s['p50_ms'] == 50.0 and s['p95_ms'] == 95.0 and s['p99_ms'] == 99.0
    assert s['deadline_misses'] == 75 and s['skipped_beats'] == 0
    assert _window_units({'100': 3, '101': 4, '105': 9}, 10.0, 10.2) == 7


@pytest.mark.skipif(os.environ.get('SMARTSWAP_SLOW') != '1', reason='spawns real background load; set SMARTSWAP_SLOW=1')
def test_real_trial_verifies_and_reverts_every_action() -> None:
    from backend.engine import run_trial
    result = run_trial('cpu', 'static')
    assert not result['errors']
    assert result['action_count'] >= 2 and result['actions_verified'] == result['action_count']
    assert result['reverts_verified'] is True
    assert result['background_units_per_s']['cpu'] > 0


def test_exact_wilcoxon_matches_reference_values() -> None:
    from backend.stats import holm, t975, wilcoxon_signed_rank
    # All 10 differences negative: p = 2 / 2**10 exactly.
    assert wilcoxon_signed_rank([-float(i) for i in range(1, 11)]) == pytest.approx(2 / 1024)
    # n=9, only the smallest |d| is negative: W- = 1, and of the 2**9 sign patterns
    # exactly two ({} and {rank 1}) have W- <= 1, so p = 2 * 2 / 512.
    assert wilcoxon_signed_rank([-6, 8, 14, 16, 23, 24, 28, 29, 41]) == pytest.approx(4 / 512)
    # Ties: |d| = 1,1 share rank 1.5; result must stay a valid probability.
    assert 0 < wilcoxon_signed_rank([1, -1, 2, 3]) <= 1
    assert t975(9) == 2.262 and t975(1000) == 1.96
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
