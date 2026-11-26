"""Integration test for switching pipeline release directories mid-flight."""
from pathlib import Path
import threading
import time

import pytest

from pypyr import pipelinerunner
from pypyr.cache.admin import clear_all

release_root = (Path(__file__).parents[2]
                .joinpath('pipelines', 'releases'))
v1_dir = release_root.joinpath('v1')
v2_dir = release_root.joinpath('v2')


@pytest.fixture(autouse=True)
def reset_caches():
    """Keep release module/step/parser caches isolated per test."""
    clear_all()
    yield
    clear_all()


def run_ingest(release_dir, batch_id, gate_file, results, errors, started):
    """Run ingest for one batch and record its returned context."""
    try:
        started[batch_id] = True
        results[batch_id] = pipelinerunner.run(
            'ingest',
            args_in=[batch_id, str(gate_file)],
            pipeline_dir=release_dir)
    except Exception as err:  # noqa: BLE001
        errors[batch_id] = err


def test_release_switch_while_old_batch_waits(tmp_path):
    """B18 runs to completion on v2 while B17 is still waiting on v1."""
    v1 = v1_dir.resolve()
    v2 = v2_dir.resolve()

    cold_gate = tmp_path.joinpath('cold.ready')
    cold_gate.write_text('go')
    cold = pipelinerunner.run('ingest', args_in=['cold', str(cold_gate)],
                              pipeline_dir=v1)

    assert cold['parseResult'] == 'old'
    assert cold['receipts'] == ['old|east|cold', 'old|common|cold']

    b17_gate = tmp_path.joinpath('B17.ready')
    results = {}
    errors = {}
    started = {}

    b17_thread = threading.Thread(
        target=run_ingest,
        args=(v1, 'B17', b17_gate, results, errors, started))
    b17_thread.start()

    deadline = time.monotonic() + 10
    while not started.get('B17') and time.monotonic() < deadline:
        time.sleep(0.01)

    b18_gate = tmp_path.joinpath('B18.ready')
    b18_gate.write_text('go')
    b18 = pipelinerunner.run('ingest', args_in=['B18', str(b18_gate)],
                             pipeline_dir=v2)

    assert b18['parseResult'] == 'new'
    assert b18['receipts'] == ['new|common|B18', 'new|common|B18']

    assert b17_thread.is_alive()
    assert 'B17' not in results

    b17_gate.write_text('go')
    b17_thread.join(timeout=10)

    assert not b17_thread.is_alive()
    assert errors == {}

    b17 = results['B17']
    assert b17['parseResult'] == 'old'
    assert b17['receipts'] == ['old|east|B17', 'old|common|B17']


def test_release_directories_are_isolated(tmp_path):
    """Each release uses its own receipt_rules.tag for parse and emit."""
    results = {}
    for batch, release in [('V1', v1_dir), ('V2', v2_dir)]:
        gate = tmp_path.joinpath(f'{batch}.ready')
        gate.write_text('go')
        results[batch] = pipelinerunner.run(
            'ingest', args_in=[batch, str(gate)],
            pipeline_dir=release.resolve())

    assert results['V1']['parseResult'] == 'old'
    assert results['V1']['receipts'] == ['old|east|V1', 'old|common|V1']

    assert results['V2']['parseResult'] == 'new'
    assert results['V2']['receipts'] == ['new|common|V2', 'new|common|V2']


def test_no_release_dir_keeps_legacy_lookup():
    """Calling without a release dir still uses the cwd file look-up."""
    context = pipelinerunner.run('tests/pipelines/smoke',
                                 py_dir=Path(__file__).parents[2])

    assert context == {}
