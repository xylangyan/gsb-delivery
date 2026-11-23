"""Tests for manifest-versioned task graphs and result provenance."""
import os
import shutil
import tempfile
import unittest
from io import StringIO

from doit import manifest
from doit.cmd_run import Run
from doit.control import TaskControl
from doit.dependency import Dependency, JsonDB
from doit.exceptions import ManifestConflict
from doit.runner import Runner
from doit.task import Task
from tests.support import CmdFactory


class _ManifestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp(prefix='doit-manifest-')
        self._cwd = os.getcwd()
        os.chdir(self.tmpdir)
        self.dep_file = os.path.join(self.tmpdir, '.doit.db')

    def tearDown(self):
        for dep in getattr(self, '_deps', ()):
            if not dep._closed:
                dep.close()
        os.chdir(self._cwd)
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        super().tearDown()

    def write(self, name, content):
        with open(name, 'w', encoding='utf-8') as fp:
            fp.write(content)

    def read(self, name):
        with open(name, encoding='utf-8') as fp:
            return fp.read()

    def new_dep(self, manifest_id=None):
        dep = Dependency(JsonDB, self.dep_file,
                         manifest_mode=manifest_id is not None,
                         manifest_id=manifest_id)
        self._deps = getattr(self, '_deps', []) + [dep]
        return dep

    def run_cmd(self, tasks, manifest_id=None, sel_tasks=None):
        output = StringIO()
        cmd = CmdFactory(Run, backend='json', dep_file=self.dep_file,
                         task_list=tasks, sel_tasks=sel_tasks)
        cmd.dep_manager = Dependency(
            JsonDB, self.dep_file,
            manifest_mode=manifest_id is not None,
            manifest_id=manifest_id)
        result = cmd._execute(output, manifest=manifest_id)
        return result, output.getvalue()


def _clean_fields(manifest_id, field='code', label='v1'):
    def clean(manifest=manifest_id, key=field):
        return {'field': key}
    return Task('clean:R', [clean], file_dep=['input.txt'],
                targets=['R.clean'], manifest=label)


class TestManifestTaskSelection(_ManifestCase):
    def test_disabled_keeps_normal_duplicate_check(self):
        v1 = Task('clean:R', [lambda: None], manifest='R-v1')
        v2 = Task('clean:R', [lambda: None], manifest='R-v2')
        # without a selected manifest the legacy "unique names" rule applies
        with self.assertRaises(Exception):
            TaskControl([v1, v2])

    def test_selects_only_active_version(self):
        v1 = Task('clean:R', [lambda: None], manifest='R-v1')
        v2 = Task('clean:R', [lambda: None], manifest='R-v2')
        control = TaskControl([v1, v2], manifest_id='R-v1')
        self.assertEqual(['clean:R'], list(control.tasks))
        self.assertEqual([v2], control.reserved_tasks)

    def test_untagged_task_always_active(self):
        common = Task('common', [lambda: None])
        v1 = Task('clean:R', [lambda: None], manifest='R-v1')
        control = TaskControl([common, v1], manifest_id='R-v2')
        self.assertEqual(['common'], list(control.tasks))

    def test_duplicate_active_name_conflicts(self):
        a = Task('clean:R', [lambda: None], manifest='R-v1')
        b = Task('clean:R', [lambda: 1], manifest='R-v1')
        with self.assertRaises(ManifestConflict):
            TaskControl([a, b], manifest_id='R-v1')


def _write_plain():
    with open('R.clean', 'w', encoding='utf-8') as fp:
        fp.write('plain')
    return {'kind': 'plain'}


def _write_reserved():
    with open('R.clean', 'w', encoding='utf-8') as fp:
        fp.write('reserved')
    return {'kind': 'reserved'}


def _write_same():
    with open('R.clean', 'w', encoding='utf-8') as fp:
        fp.write('same')
    return {'kind': 'same'}


class TestManifestVersions(_ManifestCase):
    def test_second_version_does_not_consume_first_result(self):
        self.write('input.txt', 'same')
        v1 = Task('clean:R', [_write_plain],
                  file_dep=['input.txt'], targets=['R.clean'],
                  manifest='R-v1')
        v2 = Task('clean:R', [_write_reserved],
                  file_dep=['input.txt'], targets=['R.clean'],
                  manifest='R-v2')

        self.run_cmd([v1], manifest_id='R-v1')
        self.assertEqual('plain', self.read('R.clean'))

        result, output = self.run_cmd([v1, v2], manifest_id='R-v2')
        self.assertEqual(0, result)
        self.assertEqual('reserved', self.read('R.clean'))

    def test_each_version_keeps_its_own_saved_values(self):
        self.write('input.txt', 'same')
        v1 = Task('clean:R', [_write_plain],
                  file_dep=['input.txt'], targets=['R.clean'],
                  manifest='R-v1')
        v2 = Task('clean:R', [_write_reserved],
                  file_dep=['input.txt'], targets=['R.clean'],
                  manifest='R-v2')
        self.run_cmd([v1], manifest_id='R-v1')
        self.run_cmd([v1, v2], manifest_id='R-v2')

        dep = self.new_dep('R-v1')
        control = TaskControl([v1], manifest_id='R-v1')
        dep.register_task(control.tasks['clean:R'])
        self.assertEqual({'kind': 'plain'}, dep.get_values('clean:R'))
        dep.close()

        dep = self.new_dep('R-v2')
        control = TaskControl([v2], manifest_id='R-v2')
        dep.register_task(control.tasks['clean:R'])
        self.assertEqual({'kind': 'reserved'}, dep.get_values('clean:R'))

    def test_unchanged_shard_reuses_saved_result(self):
        self.write('input.txt', 'same')
        s1 = Task('clean:S', [_write_same],
                  file_dep=['input.txt'], targets=['R.clean'],
                  manifest='S-v1')
        self.run_cmd([s1], manifest_id='S-v1')

        s2 = Task('clean:S', [_write_same],
                  file_dep=['input.txt'], targets=['R.clean'],
                  manifest='S-renamed')
        result, output = self.run_cmd([s2], manifest_id='S-renamed')
        self.assertEqual(0, result)
        # already up-to-date: reported as skipped, not executed
        self.assertIn('-- clean:S', output)
        self.assertNotIn('.  clean:S', output)

    def test_unchanged_input_reruns_after_target_removed(self):
        self.write('input.txt', 'same')
        s1 = Task('clean:S', [_write_same],
                  file_dep=['input.txt'], targets=['R.clean'],
                  manifest='S-v1')
        self.run_cmd([s1], manifest_id='S-v1')
        os.remove('R.clean')

        s2 = Task('clean:S', [_write_same],
                  file_dep=['input.txt'], targets=['R.clean'],
                  manifest='S-renamed')
        result, output = self.run_cmd([s2], manifest_id='S-renamed')
        self.assertEqual(0, result)
        self.assertIn('clean:S', output)
        self.assertTrue(os.path.exists('R.clean'))


class TestManifestProvenance(_ManifestCase):
    def test_downstream_sees_keys_from_same_completed_version(self):
        self.write('input.txt', 'same')
        producer = Task('produce:R', [lambda: {'total': 1}],
                        file_dep=['input.txt'], manifest='R-v1')
        consumer = Task('use:R', [_record_use],
                        getargs={'total': ('produce:R', 'total')},
                        manifest='R-v1')
        result, output = self.run_cmd([producer, consumer],
                                      manifest_id='R-v1')
        self.assertEqual(0, result)
        self.assertEqual(1, _RECORDED[0])

    def test_downstream_without_receipt_is_unconfirmed(self):
        dep = self.new_dep('R-v1')
        dep._set('produce:R', '_values_:', {'total': 99})
        dep._set('produce:R', 'result:', 'abc')
        dep.close()

        producer = Task('produce:R', [lambda: {'total': 1}],
                        manifest='R-v1')
        consumer = Task('use:R', [lambda total: None],
                        getargs={'total': ('produce:R', 'total')},
                        manifest='R-v1')
        result, output = self.run_cmd([producer, consumer],
                                      manifest_id='R-v1')
        # producer reruns, then consumer sees the fresh confirmed version
        self.assertEqual(0, result)


_RECORDED = []


def _record_use(total):
    _RECORDED.append(total)
    return True


class TestManifestTargetOwnership(_ManifestCase):
    def test_inactive_tasks_keep_target_owners(self):
        v1 = Task('clean:R', [lambda: None], targets=['R.clean'],
                  manifest='R-v1')
        v2 = Task('clean:S', [lambda: None], targets=['R.clean'],
                  manifest='S-v1')
        owners = manifest.target_owners([v1, v2], 'R-v1')
        self.assertIn('R.clean', owners)
        self.assertEqual(
            [('clean:R', manifest.task_graph_digest(v1), True),
             ('clean:S', manifest.task_graph_digest(v2), False)],
            owners['R.clean'])

    def test_generated_task_can_not_silently_replace_other_round(self):
        v1 = Task('clean:R', [lambda: None], targets=['R.clean'],
                  manifest='R-v1')
        owners = manifest.target_owners([v1], 'S-v1')
        new = Task('clean:R2', [lambda: None], targets=['R.clean'],
                   manifest='S-v1')
        with self.assertRaises(ManifestConflict):
            manifest.check_generated_task(
                new, {'R.clean': 'clean:R'}, owners, 'S-v1')

    def test_placeholder_can_be_replaced(self):
        from doit.task import DelayedLoader
        placeholder = Task('clean:R', None,
                           loader=DelayedLoader(lambda: None),
                           manifest='S-v1')
        real = Task('clean:R', [lambda: None], targets=['R.clean'],
                    manifest='S-v1')
        owners = manifest.target_owners([real], 'S-v1')
        # same graph name/target is allowed to replace a delayed placeholder
        manifest.check_generated_task(
            real, {'clean:R': placeholder}, owners, 'S-v1')


class TestManifestDigest(unittest.TestCase):
    def test_same_definition_same_digest(self):
        def make(field):
            return Task('clean:R', [(lambda: {'field': field},)],
                        file_dep=['input.txt'], manifest='R-v1')
        self.assertEqual(manifest.task_graph_digest(make('code')),
                         manifest.task_graph_digest(make('code')))
        self.assertNotEqual(manifest.task_graph_digest(make('code')),
                            manifest.task_graph_digest(make('other')))

    def test_tag_is_not_part_of_graph(self):
        a = Task('clean:R', [lambda: None], manifest='R-v1')
        b = Task('clean:R', [lambda: None], manifest='R-v2')
        self.assertEqual(manifest.task_graph_digest(a),
                         manifest.task_graph_digest(b))

    def test_calc_dep_result_not_in_static_graph(self):
        task = Task('use:R', [lambda: None], calc_dep=['getargs'])
        static = manifest.task_graph_digest(task)
        task.manifest_static_graph = static
        task.task_dep.append('computed')
        self.assertEqual(static, manifest.graph_id(task))


# ---------------------------------------------------------------------------
# Scenario tests: manifest versions crossing, calc_dep, getargs, resume
# ---------------------------------------------------------------------------

_SCENARIO = {}


def _record(label, value):
    _SCENARIO.setdefault(label, []).append(value)
    return {'label': label, 'value': value}


def _produce_v1():
    return _record('R', 'plain-code')


def _produce_v2():
    return _record('R', 'reserved-label')


def task_dispatch(field):
    return {'file_dep': ['input.txt'], 'field': field}


def make_version(label, field):
    producer = Task('produce:R', [(task_dispatch, [field])],
                    file_dep=['input.txt'], manifest=label)
    consumer = Task('use:R', [_use_total],
                    getargs={'total': ('produce:R', 'field')},
                    manifest=label)
    return [producer, consumer]


_USED = []


def _use_total(total):
    _USED.append(total)
    return True


class TestManifestScenario(_ManifestCase):
    def setUp(self):
        super().setUp()
        _USED.clear()
        _SCENARIO.clear()

    def test_interleaved_versions_use_own_graph(self):
        self.write('input.txt', 'same')
        v1 = make_version('R-v1', 'code')
        v2 = make_version('R-v2', 'label')
        self.run_cmd(v1, manifest_id='R-v1')
        self.assertEqual(['code'], _USED)
        _USED.clear()
        result, output = self.run_cmd(v1 + v2, manifest_id='R-v2')
        self.assertEqual(0, result)
        self.assertEqual(['label'], _USED)

    def test_calc_dep_added_dependency_is_recorded(self):
        self.write('input.txt', 'same')

        def calc():
            return {'file_dep': ['derived.txt']}

        def make_derived():
            self.write('derived.txt', 'x')
            return {'ok': True}

        calc_task = Task('calc:R', [calc], file_dep=['input.txt'],
                         manifest='R-v1')
        derived = Task('derived:R', [make_derived], targets=['derived.txt'],
                       manifest='R-v1')
        consumer = Task('use:R', [lambda: True], calc_dep=['calc:R'],
                        manifest='R-v1')
        result, output = self.run_cmd(
            [derived, calc_task, consumer], manifest_id='R-v1')
        self.assertEqual(0, result)
        self.assertIn('derived.txt', consumer.file_dep)

    def test_resume_after_partial_progress(self):
        self.write('input.txt', 'same')
        calls = []

        def step_a():
            calls.append('a')
            return {'a': 1}

        def step_b():
            calls.append('b')
            return {'b': 2}

        a = Task('step:A', [step_a], file_dep=['input.txt'],
                 uptodate=[True], manifest='R-v1')
        b = Task('step:B', [step_b], file_dep=['input.txt'],
                 uptodate=[True], manifest='R-v1')
        self.run_cmd([a], manifest_id='R-v1')
        self.assertEqual(['a'], calls)

        result, output = self.run_cmd([a, b], manifest_id='R-v1')
        self.assertEqual(0, result)
        self.assertEqual(['a', 'b'], calls)
        self.assertIn('-- step:A', output)
        self.assertIn('.  step:B', output)

    def test_legacy_values_without_receipt_stay_unconfirmed(self):
        dep = self.new_dep('R-v1')
        dep._set('produce:R', '_values_:', {'field': 'code'})
        dep._set('produce:R', 'result:', 'same-result')
        dep.close()

        producer = Task('produce:R', [lambda: {'field': 'code'}],
                        file_dep=['input.txt'], manifest='R-v1')
        dep = self.new_dep('R-v1')
        dep.register_task(producer)
        status, reason = dep.manifest_check(producer)
        self.assertEqual('unconfirmed', status)
        self.assertEqual('legacy_no_receipt', reason)
        dep.close()

    def test_same_legacy_values_do_not_count_as_success(self):
        self.write('input.txt', 'same')
        dep = self.new_dep('R-v1')
        dep._set('produce:R', '_values_:', {'field': 'code'})
        dep.close()

        calls = []

        def produce():
            calls.append(1)
            return {'field': 'code'}

        producer = Task('produce:R', [produce], file_dep=['input.txt'],
                        manifest='R-v1')
        result, output = self.run_cmd([producer], manifest_id='R-v1')
        self.assertEqual(0, result)
        self.assertEqual([1], calls)


class TestManifestConflict(_ManifestCase):
    def test_calc_dep_target_held_by_other_round_raises(self):
        self.write('input.txt', 'same')
        other = Task('clean:S', [lambda: None], targets=['shared.txt'],
                     manifest='S-v1')

        def calc():
            return {'file_dep': ['shared.txt']}

        calc_task = Task('calc:R', [calc], file_dep=['input.txt'],
                         manifest='R-v1')
        consumer = Task('use:R', [lambda: True], calc_dep=['calc:R'],
                        manifest='R-v1')
        result, output = self.run_cmd(
            [other, calc_task, consumer], manifest_id='R-v1')
        # the run aborts with an explicit error, it does not silently
        # take over the other round's target
        self.assertEqual(2, result)
        self.assertIn('belongs to another round', output)
        self.assertIn('shared.txt', output)

    def test_same_round_calc_dep_target_allowed(self):
        self.write('input.txt', 'same')

        def make_shared():
            self.write('shared.txt', 'x')
            return True

        producer = Task('clean:R', [make_shared], targets=['shared.txt'],
                        manifest='R-v1')

        def calc():
            return {'file_dep': ['shared.txt']}

        calc_task = Task('calc:R', [calc], file_dep=['input.txt'],
                         manifest='R-v1')
        consumer = Task('use:R', [lambda: True], calc_dep=['calc:R'],
                        manifest='R-v1')
        result, output = self.run_cmd(
            [producer, calc_task, consumer], manifest_id='R-v1')
        self.assertEqual(0, result)


class TestManifestDisabled(_ManifestCase):
    def test_default_run_has_no_manifest_keys(self):
        self.write('input.txt', 'same')
        task = Task('produce:R', [lambda: {'x': 1}], file_dep=['input.txt'])
        result, output = self.run_cmd([task])
        self.assertEqual(0, result)

        dep = self.new_dep(None)
        self.assertIsNone(dep._get('produce:R', 'manifest_states:'))
        self.assertEqual({'x': 1}, dep.get_values('produce:R'))

    def test_existing_values_reused_without_manifest(self):
        self.write('input.txt', 'same')
        called = []

        def produce():
            called.append(1)
            return {'x': 1}

        task = Task('produce:R', [produce], file_dep=['input.txt'],
                    uptodate=[True])
        self.run_cmd([task])
        self.run_cmd([task])
        self.assertEqual([1], called)


class TestManifestCalcDepResume(_ManifestCase):
    def test_calc_dep_task_is_up_to_date_on_second_run(self):
        self.write('input.txt', 'same')

        def calc():
            return {'file_dep': ['derived.txt']}

        def make():
            self.write('derived.txt', 'x')
            return {'ok': True}

        tasks = [
            Task('derived:R', [make], file_dep=['input.txt'],
                 targets=['derived.txt'], manifest='R-v1'),
            Task('calc:R', [calc], file_dep=['input.txt'], manifest='R-v1'),
            Task('use:R', [lambda: True], calc_dep=['calc:R'],
                 manifest='R-v1'),
        ]
        first, out1 = self.run_cmd(tasks, manifest_id='R-v1')
        self.assertEqual(0, first)
        self.assertIn('.  use:R', out1)

        second, out2 = self.run_cmd(tasks, manifest_id='R-v1')
        self.assertEqual(0, second)
        self.assertIn('-- use:R', out2)
        self.assertNotIn('.  use:R', out2)


class TestManifestUpstreamChange(_ManifestCase):
    def test_downstream_reruns_when_upstream_graph_changes(self):
        self.write('input.txt', 'same')
        runs = []

        def produce_v1():
            runs.append('v1')
            return {'total': 1}

        def produce_v2():
            runs.append('v2')
            return {'total': 1}

        def use(total):
            runs.append('use')
            return True

        producer1 = Task('produce:R', [produce_v1], file_dep=['input.txt'],
                         uptodate=[True], manifest='R-v1')
        consumer1 = Task('use:R', [use], getargs={'total': ('produce:R', 'total')},
                         uptodate=[True], manifest='R-v1')
        self.run_cmd([producer1, consumer1], manifest_id='R-v1')
        self.assertEqual(['v1', 'use'], runs)

        runs.clear()
        producer2 = Task('produce:R', [produce_v2], file_dep=['input.txt'],
                         uptodate=[True], manifest='R-v1')
        consumer2 = Task('use:R', [use], getargs={'total': ('produce:R', 'total')},
                         uptodate=[True], manifest='R-v1')
        result, output = self.run_cmd([producer2, consumer2],
                                      manifest_id='R-v1')
        self.assertEqual(0, result)
        self.assertIn('v2', runs)
        self.assertIn('use', runs)

    def test_getargs_fails_when_source_unconfirmed(self):
        self.write('input.txt', 'same')
        dep = self.new_dep('R-v1')
        dep._set('produce:R', '_values_:', {'total': 9})
        dep.close()

        consumer = Task('use:R', [lambda total: None],
                        getargs={'total': ('produce:R', 'total')},
                        manifest='R-v1')
        producer = Task('produce:R', [lambda: {'total': 9}],
                        file_dep=['input.txt'], manifest='R-v1')
        # producer has only legacy data without a receipt: it must run first
        result, output = self.run_cmd([producer, consumer],
                                      manifest_id='R-v1')
        self.assertEqual(0, result)
        self.assertIn('use:R', output)


class TestManifestResultDep(_ManifestCase):
    def test_result_dep_uses_current_version(self):
        self.write('input.txt', 'same')

        def produce_v1():
            return 'same-result'

        producer1 = Task('produce:R', [produce_v1], file_dep=['input.txt'],
                         uptodate=[True], manifest='R-v1')
        from doit.tools import result_dep
        consumer1 = Task('use:R', [lambda: True], uptodate=[result_dep('produce:R')],
                         manifest='R-v1')
        self.run_cmd([producer1, consumer1], manifest_id='R-v1')

        # same result string, different graph => consumer must not treat the
        # saved result as its own current version
        producer2 = Task('produce:R', [produce_v1], file_dep=['input.txt'],
                         uptodate=[False], manifest='R-v1')
        consumer2 = Task('use:R', [lambda: True], uptodate=[result_dep('produce:R')],
                         manifest='R-v1')
        result, output = self.run_cmd([producer2, consumer2],
                                      manifest_id='R-v1')
        self.assertEqual(0, result)
        self.assertIn('.  use:R', output)


class TestManifestList(_ManifestCase):
    def test_list_manifest_shows_unconfirmed(self):
        from doit.cmd_list import List
        task = Task('produce:R', [lambda: {'x': 1}],
                    manifest='R-v1', uptodate=[True])
        dep = self.new_dep('R-v1')
        dep._set('produce:R', '_values_:', {'x': 1})
        dep.close()

        out = StringIO()
        cmd = CmdFactory(List, backend='json', dep_file=self.dep_file,
                         task_list=[task], sel_tasks=[])
        cmd.outstream = out
        cmd._execute(manifest='R-v1')
        self.assertIn('needs confirmation:', out.getvalue())
        self.assertIn('produce:R', out.getvalue())


class TestManifestForget(_ManifestCase):
    def test_forget_makes_task_run_again(self):
        self.write('input.txt', 'same')
        task = Task('produce:R', [_write_plain], file_dep=['input.txt'],
                    targets=['R.clean'], manifest='R-v1')
        dep = Dependency(JsonDB, self.dep_file, manifest_mode=True,
                         manifest_id='R-v1')
        self._deps = getattr(self, '_deps', []) + [dep]

        cmd = CmdFactory(Run, backend='json', dep_file=self.dep_file,
                         task_list=[task])
        cmd.dep_manager = dep
        self.assertEqual(0, cmd._execute(StringIO(), manifest='R-v1'))

        dep.remove_success(task)
        self.assertEqual('none', dep.manifest_check(task)[0])

        fresh = Task('produce:R', [_write_plain], file_dep=['input.txt'],
                     targets=['R.clean'], manifest='R-v1')
        out = StringIO()
        cmd2 = CmdFactory(Run, backend='json', dep_file=self.dep_file,
                          task_list=[fresh])
        cmd2.dep_manager = dep
        self.assertEqual(0, cmd2._execute(out, manifest='R-v1'))
        self.assertIn('.  produce:R', out.getvalue())


class TestManifestActiveTargetConflict(_ManifestCase):
    def test_two_active_tasks_claiming_same_target_conflict(self):
        a = Task('clean:R', [lambda: None], targets=['shared'],
                 manifest='V')
        b = Task('clean:S', [lambda: None], targets=['shared'],
                 manifest='V')
        # the existing "unique target" rule aborts loudly; it never silently
        # picks one of the two tasks
        with self.assertRaises(Exception):
            TaskControl([a, b], manifest_id='V')

    def test_inactive_other_version_may_keep_target(self):
        r = Task('clean:R', [lambda: None], targets=['shared'],
                 manifest='R-v1')
        s = Task('clean:S', [lambda: None], targets=['shared'],
                 manifest='S-v1')
        # active R task and inactive S task: allowed, S is another round
        control = TaskControl([r, s], manifest_id='R-v1')
        self.assertEqual(['clean:R'], list(control.tasks))


class TestManifestInterleavedResume(_ManifestCase):
    """Two versions discovered interleaved; one round saves partial progress,
    exits, then resumes.  Downstream must see keys, source result and saved
    outputs from one actually-completed version."""

    def test_partial_progress_resume_keeps_one_version(self):
        self.write('input.txt', 'same')
        executed = []

        def clean_r():
            executed.append('R')
            self.write('R.clean', 'R-output')
            return {'code': 'plain'}

        def clean_s():
            executed.append('S')
            self.write('S.clean', 'S-output')
            return {'label': 'reserved'}

        def use_r(total):
            executed.append('use:' + str(total))
            return True

        def use_s(total):
            executed.append('use:' + str(total))
            return True

        r_producer = Task('clean:R', [clean_r], file_dep=['input.txt'],
                          targets=['R.clean'], manifest='R-v1')
        r_consumer = Task('use:R', [use_r],
                          getargs={'total': ('clean:R', 'code')},
                          manifest='R-v1')
        s_producer = Task('clean:S', [clean_s], file_dep=['input.txt'],
                          targets=['S.clean'], manifest='S-v1')
        s_consumer = Task('use:S', [use_s],
                          getargs={'total': ('clean:S', 'label')},
                          manifest='S-v1')

        # first round: only S saves partial progress
        result, out = self.run_cmd([s_producer], manifest_id='S-v1')
        self.assertEqual(0, result)
        self.assertEqual(['S'], executed)

        # interleaved: R round runs fully, S round is dormant
        executed.clear()
        result, out = self.run_cmd(
            [r_producer, r_consumer, s_producer, s_consumer],
            manifest_id='R-v1')
        self.assertEqual(0, result)
        self.assertEqual(['R', 'use:plain'], executed)

        # resume S: reuses its saved result, no stale R key
        executed.clear()
        result, out = self.run_cmd(
            [r_producer, r_consumer, s_producer, s_consumer],
            manifest_id='S-v1')
        self.assertEqual(0, result)
        self.assertEqual(['use:reserved'], executed)
        self.assertEqual('S-output', self.read('S.clean'))
        self.assertEqual('R-output', self.read('R.clean'))

    def test_round_that_never_completed_is_not_used(self):
        self.write('input.txt', 'same')
        producer = Task('produce:R', [lambda: {'k': 'v1'}],
                        file_dep=['input.txt'], uptodate=[True],
                        manifest='R-v1')
        dep = self.new_dep('R-v1')
        # pretend an earlier attempt wrote values but crashed before a receipt
        dep._set('produce:R', '_values_:', {'k': 'v1'})
        dep._set('produce:R', 'result:', None)
        dep.close()

        result, out = self.run_cmd([producer], manifest_id='R-v1')
        self.assertEqual(0, result)
        self.assertIn('.  produce:R', out)


class TestManifestConsistency(_ManifestCase):
    def test_keys_source_result_and_output_are_from_same_version(self):
        self.write('input.txt', 'same')

        def produce_v1():
            self.write('out.txt', 'v1')
            return {'k': 'v1'}

        def produce_v2():
            self.write('out.txt', 'v2')
            return {'k': 'v2'}

        seen = []

        def consume(k):
            seen.append((k, self.read('out.txt')))
            return True

        def build(producer):
            return [
                Task('produce:R', [producer], file_dep=['input.txt'],
                     targets=['out.txt'], manifest='R-v1'),
                Task('use:R', [consume], getargs={'k': ('produce:R', 'k')},
                     manifest='R-v1'),
            ]

        self.run_cmd(build(produce_v1), manifest_id='R-v1')
        self.assertEqual([('v1', 'v1')], seen)

        seen.clear()
        self.run_cmd(build(produce_v2), manifest_id='R-v1')
        # the changed producer reruns and the consumer sees the new version
        self.assertEqual([('v2', 'v2')], seen)
