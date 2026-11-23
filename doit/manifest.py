"""Task-graph identity used by versioned manifests.

This module is intentionally independent from the rest of ``doit``.  It
turns a :class:`doit.task.Task` into a deterministic description and a
digest of that description.  The digest is used as the identity of the
*effective task graph*: two runs that define a task with the same actions
and the same dependencies get the same digest, even if they were selected
from different manifest versions.

The canonical description deliberately excludes attributes that do not
change what a task computes (``doc``, ``verbosity``, ``title`` ...), and it
excludes the manifest label itself.  That is what allows an unchanged
shard from an older manifest version to re-use a saved result while a
shard whose semantics changed gets a new identity.

Only the public helpers are meant to be used by ``doit`` itself:

``task_graph_digest(task)``
    return a stable digest for the task's effective definition.

``path_digest(path)``
    return a digest of a produced file/directory (``None`` if missing).

``manifest_tags(task)``
    normalise the ``manifest`` task attribute to a tuple of strings.
"""

import functools
import hashlib
import inspect
import json
import os
import re
import types


# attributes of action / uptodate helper objects that only exist while a run
# is in progress and must not influence a task's identity.
_VOLATILE_STATE = {
    'task',
    'out',
    'err',
    'result',
    'values',
    'executed',
    'dep_changed',
    'get_val',
    'tasks_dict',
    'config_digest',
    '_action_instances',
    '_actions',
}

_ADDRESS_RE = re.compile(r'0x[0-9a-fA-F]+')


def _scrub(text):
    """remove memory addresses from a fallback representation"""
    return _ADDRESS_RE.sub('0xADDR', text)


def _digest_json(data):
    payload = json.dumps(data, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=True, default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _code_key(code):
    """canonical description of a code object (stable across processes)"""
    return {
        '__code__': code.co_name,
        'bytecode': hashlib.sha1(code.co_code).hexdigest(),
        'consts': canonical(code.co_consts),
        'names': list(code.co_names),
        'varnames': list(code.co_varnames),
    }


def _immutable_scalar(value):
    return isinstance(value, (str, int, float, bool, type(None)))


def _closure_value(value):
    """identity contribution of a closure cell

    Only immutable scalars and flat tuples of them are included.  Closures
    commonly capture mutable runtime state (a log list, a counter, a parsed
    config dict) whose *contents* change while the task definition does not;
    feeding that into the digest would make an unchanged task look new on
    every run.
    """
    if _immutable_scalar(value):
        return value
    if isinstance(value, tuple) and all(_immutable_scalar(v) for v in value):
        return list(value)
    return '<opaque>'


def _function_key(func):
    """canonical description of a callable"""
    if isinstance(func, functools.partial):
        return {
            '__partial__': canonical(func.func),
            'args': canonical(func.args),
            'keywords': canonical(func.keywords),
        }

    if inspect.ismethod(func):
        return {
            '__method__': _function_key(func.__func__),
            'self': canonical(_instance_state(func.__self__)),
        }

    if inspect.isfunction(func):
        closure = []
        for cell in (func.__closure__ or ()):
            try:
                closure.append(_closure_value(cell.cell_contents))
            except ValueError:  # empty cell
                closure.append(None)
        return {
            '__function__': '{}.{}'.format(
                getattr(func, '__module__', ''),
                getattr(func, '__qualname__', getattr(func, '__name__', ''))),
            'code': _code_key(func.__code__),
            'defaults': canonical(func.__defaults__),
            'kwdefaults': canonical(func.__kwdefaults__),
            'closure': closure,
        }

    if inspect.isbuiltin(func):
        return {
            '__builtin__': '{}.{}'.format(
                getattr(func, '__module__', ''),
                getattr(func, '__qualname__', getattr(func, '__name__', ''))),
        }

    if inspect.isclass(func):
        return {'__class__': '{}.{}'.format(
            getattr(func, '__module__', ''), func.__qualname__)}

    call = getattr(type(func), '__call__', None)
    state = _instance_state(func)
    return {
        '__callable__': '{}.{}'.format(
            type(func).__module__, type(func).__qualname__),
        'state': canonical(state),
        'call': _code_key(call.__code__) if inspect.isfunction(call) else None,
    }


def _instance_state(obj):
    """stable part of a callable object's state"""
    state = getattr(obj, '__dict__', None)
    if state is None:
        return {}
    return {k: _state_value(v) for k, v in state.items()
            if k not in _VOLATILE_STATE and not k.startswith('__')}


def _state_value(value):
    if _immutable_scalar(value):
        return value
    return '<opaque>'


def canonical(obj, _seen=None):
    """return a JSON friendly, deterministic representation of ``obj``"""
    if _seen is None:
        _seen = set()

    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj

    if isinstance(obj, bytes):
        return {'__bytes__': obj.hex()}

    if isinstance(obj, (list, tuple)):
        return [canonical(item, _seen) for item in obj]

    if isinstance(obj, (set, frozenset)):
        items = [canonical(item, _seen) for item in obj]
        return {'__set__': sorted(items, key=lambda item: json.dumps(
            item, sort_keys=True, default=str))}

    if isinstance(obj, dict):
        return {str(key): canonical(value, _seen)
                for key, value in obj.items()}

    # pathlib.PurePath and os.PathLike
    if hasattr(obj, '__fspath__'):
        try:
            return {'__path__': os.fspath(obj)}
        except TypeError:
            pass

    if isinstance(obj, types.CodeType):
        return _code_key(obj)

    if isinstance(obj, type):
        return {'__class__': '{}.{}'.format(
            getattr(obj, '__module__', ''), obj.__qualname__)}

    if inspect.isfunction(obj) or inspect.ismethod(obj) or \
            inspect.isbuiltin(obj) or isinstance(obj, functools.partial):
        return _function_key(obj)

    # guard against recursive object graphs
    obj_id = id(obj)
    if obj_id in _seen:
        return '<recursive>'
    _seen.add(obj_id)
    try:
        state = getattr(obj, '__dict__', None)
        if state is not None:
            filtered = {
                key: value for key, value in state.items()
                if key not in _VOLATILE_STATE and not key.startswith('__')}
            return {
                '__object__': '{}.{}'.format(
                    type(obj).__module__, type(obj).__qualname__),
                'state': canonical(filtered, _seen),
            }
        return {'__repr__': _scrub(repr(obj))}
    finally:
        _seen.discard(obj_id)


def _action_key(action):
    """canonical description of a task action instance"""
    # import lazily to avoid an import cycle (task -> manifest -> action)
    from .action import CmdAction, PythonAction

    if isinstance(action, PythonAction):
        return {
            'type': 'python',
            'callable': canonical(action.py_callable),
            'args': canonical(action.args),
            'kwargs': canonical(action.kwargs),
        }

    if isinstance(action, CmdAction):
        pkwargs = {key: value for key, value in action.pkwargs.items()}
        return {
            'type': 'cmd',
            'action': canonical(getattr(action, '_action', None)),
            'shell': action.shell,
            'save_out': action.save_out,
            'encoding': action.encoding,
            'decode_error': action.decode_error,
            'pkwargs': canonical(pkwargs),
        }

    state = {key: value for key, value in getattr(action, '__dict__', {}).items()
             if key not in _VOLATILE_STATE and not key.startswith('__')}
    return {
        'type': '{}.{}'.format(type(action).__module__,
                               type(action).__qualname__),
        'state': canonical(state),
    }


def _actions_key(actions):
    return [_action_key(action) for action in actions]


def task_canonical(task):
    """return the canonical description of what a task declares

    Dependencies added at execution time by ``calc_dep`` are not part of a
    task's identity: they are produced by another task and validated through
    the provenance of that task.  When a task object is re-used (tests,
    embedded API) the captured declaration is used so earlier dynamic
    additions do not change its identity.
    """
    captured = getattr(task, 'manifest_declared', None)
    if captured is not None:
        file_dep = captured['file_dep']
        task_dep = captured['task_dep']
        calc_dep = captured['calc_dep']
        setup = captured['setup']
    else:
        file_dep = task.file_dep
        task_dep = task.task_dep
        calc_dep = task.calc_dep
        setup = task.setup_tasks

    getargs = []
    for arg, spec in sorted(task.getargs.items()):
        if isinstance(spec, str):  # legacy / invalid format, keep as is
            getargs.append([arg, spec, None])
        else:
            try:
                source, key = spec
            except (TypeError, ValueError):
                source, key = spec, None
            getargs.append([arg, source, key])

    uptodate = []
    for item in task.uptodate:
        call, args, kwargs = item[0], item[1], item[2]
        uptodate.append([canonical(call), canonical(args), canonical(kwargs)])

    if task._remove_targets:
        clean = {'remove_targets': True}
    else:
        clean = {'actions': _actions_key(task.clean_actions)}

    return {
        'name': task.name,
        'actions': _actions_key(task.actions),
        'file_dep': sorted(file_dep),
        'task_dep': sorted(task_dep),
        'calc_dep': sorted(calc_dep),
        'targets': sorted(task.targets),
        'setup': sorted(setup),
        'getargs': getargs,
        'params': canonical(task.params),
        'creator_params': canonical(task.creator_params),
        'uptodate': uptodate,
        'clean': clean,
        'teardown': _actions_key(task.teardown),
        'io': {'capture': task.io.capture},
        'pos_arg': task.pos_arg,
        'subtask_of': task.subtask_of,
        'has_subtask': bool(task.has_subtask),
        'meta': canonical(task.meta),
    }


def task_graph_digest(task):
    """return a stable digest of the task's effective definition"""
    return _digest_json(task_canonical(task))


def graph_id(task):
    """identity used by receipts

    Dependencies computed by ``calc_dep`` only exist while a run is in
    progress.  The identity must be the *static* graph captured when the
    task was first reached, otherwise an unchanged task would never match
    the receipt saved after its dynamic dependencies were added.
    """
    return getattr(task, 'manifest_static_graph', None) or \
        task_graph_digest(task)


def task_source_names(task):
    """return the names of tasks whose results this task can consume"""
    names = set()
    names.update(task.task_dep)
    names.update(task.setup_tasks)
    names.update(task.calc_dep)
    for arg, spec in task.getargs.items():
        if isinstance(spec, str):
            names.add(spec)
        else:
            try:
                names.add(spec[0])
            except (TypeError, ValueError, IndexError):
                pass
    return sorted(names)


def provenance_digest(task, tasks_dict, active=None):
    """digest of a task graph plus the graphs of its upstream tasks

    The task's own graph only lists dependency *names*.  It can not tell
    whether the task producing a value changed its meaning.  This digest
    makes upstream definitions part of the identity recorded in a receipt,
    so a result produced against another version of an upstream task is not
    re-used.
    """
    upstream = []
    for name in task_source_names(task):
        dep = tasks_dict.get(name)
        if dep is None:
            upstream.append([name, None])
        else:
            upstream.append([name, effective_manifest(dep, active),
                             graph_id(dep)])
    return _digest_json({
        'graph': graph_id(task),
        'upstream': upstream,
    })


def receipt_ref(receipt):
    """return a short, stable reference to a receipt payload"""
    return _digest_json({
        'graph': receipt.get('graph'),
        'provenance': receipt.get('provenance'),
        'values': receipt.get('values'),
        'result': receipt.get('result'),
        'outputs': receipt.get('outputs'),
    })


def values_digest(values):
    """stable digest of a task's computed values"""
    return _digest_json(values)


def manifest_tags(task):
    """normalise the ``manifest`` attribute to a tuple of strings"""
    value = getattr(task, 'manifest', None)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    tags = []
    for item in value:
        tag = str(item)
        if tag not in tags:
            tags.append(tag)
    return tuple(tags)


def effective_manifest(task, active=None):
    """return the manifest tag used to scope a task's saved result

    An explicit task manifest wins over the manifest selected for the run.
    When a task belongs to several manifests its tags are joined so the
    saved result is not silently shared with a different version.
    """
    tags = manifest_tags(task)
    if tags:
        return '+'.join(tags)
    return active


def task_is_active(task, active):
    """whether a task belongs to the selected manifest version"""
    tags = manifest_tags(task)
    if not tags:
        return True
    return active is not None and active in tags


def version_label(task, active=None):
    """label recorded in a receipt for a task's manifest version"""
    tag = effective_manifest(task, active)
    return '' if tag is None else tag


def select_tasks(task_list, active):
    """split tasks into the ones effective for ``active`` and the rest

    :return: (selected, reserved)
        ``selected`` keeps definition order and holds exactly one task per
        name.  ``reserved`` holds inactive tasks, used to detect a delayed
        generation that would silently overwrite another round's target.
    :raises ManifestConflict: two effective tasks share a name.
    """
    from .exceptions import ManifestConflict

    selected = []
    grouped = {}
    reserved = []
    for task in task_list:
        if task_is_active(task, active):
            if task.name in grouped:
                other = grouped[task.name]
                raise ManifestConflict(
                    "Task '{}' is defined more than once for manifest '{}' "
                    "(graphs {} and {}). Select a single version with "
                    "`manifest`.".format(
                        task.name, active,
                        task_graph_digest(other)[:12],
                        task_graph_digest(task)[:12]))
            grouped[task.name] = task
            selected.append(task)
        else:
            reserved.append(task)
    return selected, reserved


def target_owners(task_list, active):
    """map target path -> list of (task name, graph digest, is_active)"""
    owners = {}
    for task in task_list:
        is_active = task_is_active(task, active)
        for target in task.targets:
            owners.setdefault(str(target), []).append(
                (task.name, task_graph_digest(task), is_active))
    return owners


def check_generated_task(task, tasks, owners, active):
    """validate a task generated at execution time

    :param task: newly generated Task
    :param tasks: dict of tasks already known by the dispatcher (name -> Task)
    :param owners: target ownership map that also includes inactive tasks
    :param active: selected manifest version

    Replacing a delayed placeholder, or re-generating the exact same graph,
    is allowed.  Anything else would silently discard another round's task.
    """
    from .exceptions import ManifestConflict

    existing = tasks.get(task.name)
    if existing is not None and existing is not task:
        placeholder = bool(getattr(existing, 'loader', None))
        same_graph = (task_graph_digest(existing) == task_graph_digest(task))
        if not placeholder and not same_graph:
            raise ManifestConflict(
                "Task '{}' generated for manifest '{}' would replace an "
                "existing task with a different graph ({} -> {}).".format(
                    task.name, active,
                    task_graph_digest(existing)[:12],
                    task_graph_digest(task)[:12]))

    if not task_is_active(task, active):
        return
    for target in task.targets:
        for name, graph, is_active in owners.get(str(target), ()):
            if name == task.name and graph == task_graph_digest(task):
                continue
            raise ManifestConflict(
                "Task '{}' for manifest '{}' defines target '{}' already held "
                "by task '{}' (manifest {}){}.".format(
                    task.name, active, target, name,
                    'active' if is_active else 'inactive',
                    '' if is_active else ' from another round'))



def path_digest(path):
    """return a digest for a produced file or directory

    ``None`` means the path does not exist (or can not be read).
    """
    if not os.path.exists(path):
        return None
    if os.path.isfile(path):
        md5 = hashlib.md5()
        try:
            with open(path, 'rb') as file_data:
                while True:
                    block = file_data.read(128 * 1024)
                    if not block:
                        break
                    md5.update(block)
        except OSError:
            return None
        return md5.hexdigest()

    if os.path.isdir(path):
        entries = []
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, path)
                entries.append([rel.replace(os.sep, '/'), path_digest(full)])
        return _digest_json(entries)

    return None
