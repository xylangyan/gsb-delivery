"""
    ulid.ledger.planner
    ~~~~~~~~~~~~~~~~~~~

    改签规划: 给出能实际交付的 ULID 与改号最少的方案。

    优化目标(按字典序):
        1. 改号的原件件数最少(能保留原号就保留, 后补新件不计入改号);
        2. 改号件数相同的方案里, 需要改签的读者数最少。

    可行性全部用 128 位闭区间算术判定, 从不枚举 ULID 窗口; 真正发号交给
    :class:`~ulid.ledger.numbering.NumberingService` 在窗口内哈希落点。

    硬锚点(永不改号):
        * 已对外确认或已交付的原件;
        * 不能改签读者尚未越过(未交付)的原件;
        * 不能改签读者已经签下的旧界线。
"""
import dataclasses
import itertools
import typing

from .. import consts, ulid
from .model import AuditBlock, Reticket
from .ordering import PartialOrder, Window
from .reader import Reader
from .registry import Ledger

_FULL_LOW = 0
_FULL_HIGH = consts.MAX_ULID.int
_MAX_SUFFIX = consts.MAX_RANDOMNESS.int


@dataclasses.dataclass(frozen=True)
class BackfillRequest:
    """
    一个后补件的补号请求。

    :param item_id: 物理原件编号
    :type item_id: :class:`~str`
    :param window: 补号允许落入的窗口
    :type window: :class:`~ulid.ledger.ordering.Window`
    :param library_code: 原库编码
    :type library_code: :class:`~str` or :class:`~None`
    """

    item_id: str
    window: Window
    library_code: typing.Optional[str] = None


@dataclasses.dataclass
class RenumberPlan:
    """
    一次改签规划的结果。

    :param generation: 规划面向的新代
    :type generation: :class:`~int`
    :param new_items: 后补件的新发 ULID
    :type new_items: :class:`~typing.Dict`
    :param renames: 改号原件的 ``原件编号 -> 新 ULID``
    :type renames: :class:`~typing.Dict`
    :param retickets: 获准改签读者的改签票
    :type retickets: :class:`~typing.Dict`
    :param audits: 不能改签读者的核对记录
    :type audits: :class:`~typing.Tuple`
    :param score: 选中方案的 ``(改号件数, 改签读者数)``
    :type score: :class:`~typing.Tuple`
    :param library_codes: 后补件的原库编码
    :type library_codes: :class:`~typing.Dict`
    """

    generation: int
    new_items: typing.Dict[str, ulid.ULID]
    renames: typing.Dict[str, ulid.ULID]
    retickets: typing.Dict[str, Reticket]
    audits: typing.Tuple[AuditBlock, ...]
    score: typing.Tuple[int, int]
    library_codes: typing.Dict[str, typing.Optional[str]]

    @property
    def reticketed_readers(self) -> typing.FrozenSet[str]:
        """
        本方案实际需要改签的读者编号集合。

        :rtype: :class:`~frozenset`
        """
        return frozenset(self.retickets)

    @property
    def changed_items(self) -> typing.Dict[str, ulid.ULID]:
        """
        后补与改号合并后的 ``原件编号 -> 新 ULID``。

        :rtype: :class:`~typing.Dict`
        """
        merged = dict(self.new_items)
        merged.update(self.renames)
        return merged


def window_interval(window: Window) -> typing.Tuple[int, int]:
    """
    把窗口换算成 128 位 ULID 整数闭区间, 不枚举区间内号码。

    :param window: 输入窗口
    :type window: :class:`~ulid.ledger.ordering.Window`
    :return: ``(下界, 上界)``
    :rtype: :class:`~typing.Tuple`
    """
    if window.kind.name == 'OPEN':
        return _FULL_LOW, _FULL_HIGH
    lower_millis = window.lower_millis if window.lower_millis is not None else 0
    upper_millis = window.upper_millis if window.upper_millis is not None else consts.MAX_TIMESTAMP.int
    lower_suffix = window.lower_suffix if window.lower_suffix is not None else 0
    upper_suffix = window.upper_suffix if window.upper_suffix is not None else _MAX_SUFFIX
    return (lower_millis << 80) | lower_suffix, (upper_millis << 80) | upper_suffix


class ReticketPlanner:
    """
    在现有目录、读者进度与后补请求上做改签规划。

    :param ledger: 对照目录
    :type ledger: :class:`~ulid.ledger.registry.Ledger`
    :param reader_service: 读者翻页服务
    :type reader_service: :class:`~ulid.ledger.reader.ReaderService`
    :param numbering: 号池, 负责在窗口内发新号
    :type numbering: :class:`~ulid.ledger.numbering.NumberingService`
    """

    def __init__(self, ledger: Ledger, reader_service, numbering) -> None:
        self.ledger = ledger
        self.reader_service = reader_service
        self.numbering = numbering

    def plan(self,
             generation: int,
             round_id: str,
             backfills: typing.Iterable[BackfillRequest]) -> RenumberPlan:
        """
        为新代生成改号最少的规划并向号池保留新号。

        :param generation: 新代编号
        :type generation: :class:`~int`
        :param round_id: 号池轮次, 新号在该轮保留
        :type round_id: :class:`~str`
        :param backfills: 后补件请求
        :type backfills: :class:`~typing.Iterable`
        :return: 改签规划
        :rtype: :class:`~ulid.ledger.planner.RenumberPlan`
        """
        backfills = tuple(backfills)
        windows = {request.item_id: request.window for request in backfills}
        backfill_codes = {request.item_id: request.library_code for request in backfills}
        readers = tuple(self.reader_service.get(rid) for rid in self.reader_service.reader_ids())
        sequences = {
            reader.reader_id: self.reader_service.ordered_scope(reader)
            for reader in readers
        }

        anchors = self._anchors(readers)
        movable = self._movable(anchors, windows)
        intervals = self._intervals(movable, windows, readers, sequences, anchors)
        blocked = frozenset(
            item_id for item_id in movable
            if intervals[item_id][0] > intervals[item_id][1])
        audits = self._audit_blocks(readers, sequences, intervals, anchors, blocked)

        feasible_movable = movable - blocked
        rename_set, score = self._minimum_renames(
            feasible_movable, intervals, windows, readers, sequences, anchors)

        new_items, renames = self._issue_numbers(
            generation, round_id, feasible_movable, rename_set, intervals, windows)
        retickets = self._retickets(readers, sequences, {**new_items, **renames}, generation)
        return RenumberPlan(
            generation=generation,
            new_items=new_items,
            renames=renames,
            retickets=retickets,
            audits=audits,
            score=score,
            library_codes=backfill_codes)

    def _anchors(self, readers) -> typing.Dict[str, int]:
        anchors = {}
        for item_id in self.ledger.ids():
            item = self.ledger.get(item_id)
            if item.is_frozen:
                anchors[item_id] = item.current_ulid.int
        for reader in readers:
            if reader.spec.reticket_allowed:
                continue
            # 不能改签的读者整段视图都继续用原格式号码, 其范围内在册件一律为锚点。
            for item_id in reader.spec.scope:
                if item_id not in anchors and self.ledger.contains(item_id):
                    anchors[item_id] = self.ledger.current_ulid(item_id).int
        return anchors

    def _movable(self, anchors, windows) -> typing.FrozenSet[str]:
        known = set(self.ledger.ids())
        return frozenset((known | set(windows)) - set(anchors))

    def _intervals(self, movable, windows, readers, sequences, anchors):
        intervals = {item_id: [_FULL_LOW, _FULL_HIGH] for item_id in movable}
        for item_id in movable:
            if item_id in windows:
                low, high = window_interval(windows[item_id])
                intervals[item_id][0] = max(intervals[item_id][0], low)
                intervals[item_id][1] = min(intervals[item_id][1], high)
        for reader in readers:
            sequence = sequences[reader.reader_id]
            running_low = reader.bound.int + 1 if (
                not reader.spec.reticket_allowed and reader.bound is not None) else _FULL_LOW
            for item_id in sequence:
                if item_id in anchors:
                    running_low = max(running_low, anchors[item_id] + 1)
                elif item_id in intervals:
                    intervals[item_id][0] = max(intervals[item_id][0], running_low)
            running_high = _FULL_HIGH
            for item_id in reversed(sequence):
                if item_id in anchors:
                    running_high = min(running_high, anchors[item_id] - 1)
                elif item_id in intervals:
                    intervals[item_id][1] = min(intervals[item_id][1], running_high)
        return intervals

    def _audit_blocks(self, readers, sequences, intervals, anchors, blocked):
        audits = []
        for reader in readers:
            if reader.spec.reticket_allowed or reader.bound is None:
                continue
            stuck = []
            for item_id in sequences[reader.reader_id]:
                if item_id in reader.delivered or item_id in anchors:
                    continue
                if item_id in blocked:
                    stuck.append(item_id)
                    continue
                low, high = intervals.get(item_id, (_FULL_LOW, _FULL_HIGH))
                if low > high:
                    stuck.append(item_id)
            if stuck:
                audits.append(AuditBlock(
                    reader_id=reader.reader_id,
                    bound=reader.bound,
                    pending_items=tuple(dict.fromkeys(stuck))))
        return tuple(audits)

    def _current_values(self, movable):
        values = {}
        for item_id in movable:
            if self.ledger.contains(item_id):
                values[item_id] = self.ledger.current_ulid(item_id).int
        return values

    def _minimum_renames(self, movable, intervals, windows, readers, sequences, anchors):
        current = self._current_values(movable)
        forced = {
            item_id for item_id in movable
            if item_id not in current
            or not intervals[item_id][0] <= current[item_id] <= intervals[item_id][1]
        }
        components = self._components(forced, current, intervals, readers, sequences, anchors)
        options_per_component = [
            self._component_options(component, forced, current, intervals, readers, sequences, anchors)
            for component in components
        ]
        chosen, renames, affected_readers = self._combine_options(
            options_per_component, forced, current, readers, sequences)
        rename_set = frozenset(forced) | chosen
        return rename_set, (renames, len(affected_readers))

    def _components(self, forced, current, intervals, readers, sequences, anchors):
        adjacency = {}  # type: typing.Dict[str, typing.Set[str]]

        def link(left: str, right: str) -> None:
            adjacency.setdefault(left, set()).add(right)
            adjacency.setdefault(right, set()).add(left)

        for reader in readers:
            sequence = sequences[reader.reader_id]
            previous_id = None
            previous_value = None
            for item_id in sequence:
                value = self._representative(item_id, forced, current, intervals, anchors)
                if previous_id is not None and value is not None and previous_value is not None:
                    if value <= previous_value:
                        self._link_movable(link, item_id, previous_id, anchors)
                previous_id = item_id
                previous_value = value
            for index, item_id in enumerate(sequence):
                if item_id not in forced:
                    continue
                for neighbor in sequence[max(0, index - 1):index + 2]:
                    if neighbor != item_id and neighbor not in anchors:
                        link(item_id, neighbor)
        seen = set()
        components = []
        for node in adjacency:
            if node in seen:
                continue
            stack = [node]
            component = set()
            while stack:
                current_node = stack.pop()
                if current_node in seen:
                    continue
                seen.add(current_node)
                component.add(current_node)
                stack.extend(adjacency[current_node])
            components.append(frozenset(component))
        return components

    @staticmethod
    def _link_movable(link, left, right, anchors):
        if left not in anchors and right not in anchors:
            link(left, right)

    def _representative(self, item_id, forced, current, intervals, anchors):
        if item_id in anchors:
            return anchors[item_id]
        if item_id in forced:
            return intervals[item_id][0]
        return current.get(item_id)

    def _component_options(self, component, forced, current, intervals,
                           readers, sequences, anchors):
        candidates = sorted(item_id for item_id in component if item_id not in forced)
        ranked = []
        for size in range(0, len(candidates) + 1):
            for subset in itertools.combinations(candidates, size):
                rename_set = frozenset(subset) | forced
                if not self._feasible(rename_set, current, intervals, readers, sequences, anchors):
                    continue
                registered_renames = sum(
                    1 for item_id in component if item_id in rename_set and item_id in current)
                ranked.append((frozenset(subset), registered_renames))
        if not ranked:
            raise RuntimeError('No feasible renumber plan for conflict component {}'.format(component))
        minimum_renames = min(score for _subset, score in ranked)
        return [(subset, score) for subset, score in ranked if score == minimum_renames]

    def _combine_options(self, options_per_component, forced, current, readers, sequences):
        best = None  # type: typing.Optional[typing.Tuple[frozenset, int, frozenset]]
        for combination in itertools.product(*options_per_component):
            chosen = set()
            for subset, _component_renames in combination:
                chosen.update(subset)
            rename_set = frozenset(chosen) | forced
            registered_renames = frozenset(
                item_id for item_id in rename_set if item_id in current)
            affected = frozenset(self._reticket_readers(registered_renames, readers, sequences))
            renames = len(registered_renames)
            candidate = (frozenset(chosen), renames, affected)
            if best is None or (renames, len(affected)) < (best[1], len(best[2])):
                best = candidate
        return best

    def _feasible(self, rename_set, current, intervals, readers, sequences, anchors):
        for reader in readers:
            previous_value = None
            for item_id in sequences[reader.reader_id]:
                low, high = intervals.get(item_id, (_FULL_LOW, _FULL_HIGH))
                if item_id in anchors:
                    value = anchors[item_id]
                elif item_id in rename_set or item_id not in current:
                    floor = low if previous_value is None else max(low, previous_value + 1)
                    if floor > high:
                        return False
                    value = floor
                else:
                    value = current[item_id]
                    if not low <= value <= high:
                        return False
                if previous_value is not None and value <= previous_value:
                    return False
                previous_value = value
        return True

    @staticmethod
    def _reticket_readers(rename_set, readers, sequences):
        affected = set()
        for reader in readers:
            if not reader.spec.reticket_allowed:
                continue
            if rename_set.intersection(sequences[reader.reader_id]):
                affected.add(reader.reader_id)
        return affected

    def _issue_numbers(self, generation, round_id, movable,
                       rename_set, intervals, windows):
        new_items = {}
        renames = {}
        changed = sorted(
            item_id for item_id in movable
            if item_id in rename_set or not self.ledger.contains(item_id))
        order = self._issue_order(changed)
        assigned = {
            item_id: self.ledger.current_ulid(item_id).int
            for item_id in self.ledger.ids()
            if item_id not in rename_set
        }
        for item_id in order:
            low, high = intervals[item_id]
            floor = max(low, self._predecessor_floor(item_id, assigned))
            ceiling = min(high, self._successor_ceiling(item_id, assigned, set(changed)))
            if floor > ceiling:
                raise RuntimeError('Empty feasible interval for item {}'.format(item_id))
            window = self._issue_window(item_id, floor, ceiling, windows)
            tag = '{}:{}'.format(generation, item_id)
            _window_low, window_high = window_interval(window)
            _token, value = self.numbering.hold_position(
                round_id, floor, tag, min(ceiling, window_high))
            assigned[item_id] = value.int
            if self.ledger.contains(item_id):
                renames[item_id] = value
            else:
                new_items[item_id] = value
        return new_items, renames

    def _issue_order(self, changed):
        order_model = PartialOrder()
        changed_set = set(changed)
        for reader_id in self.reader_service.reader_ids():
            reader = self.reader_service.get(reader_id)
            sequence = self.reader_service.ordered_scope(reader)
            previous = None
            for item_id in sequence:
                if item_id not in changed_set:
                    continue
                if previous is not None:
                    order_model.add_before(previous, item_id)
                previous = item_id
        return order_model.topo_sort(changed)

    def _predecessor_floor(self, item_id, assigned):
        floor = _FULL_LOW
        for reader_id in self.reader_service.reader_ids():
            reader = self.reader_service.get(reader_id)
            sequence = self.reader_service.ordered_scope(reader)
            if item_id not in sequence:
                continue
            index = sequence.index(item_id)
            for predecessor in reversed(sequence[:index]):
                if predecessor in assigned:
                    floor = max(floor, assigned[predecessor] + 1)
                    break
        return floor

    def _successor_ceiling(self, item_id, assigned, changed):
        ceiling = _FULL_HIGH
        for reader_id in self.reader_service.reader_ids():
            reader = self.reader_service.get(reader_id)
            sequence = self.reader_service.ordered_scope(reader)
            if item_id not in sequence:
                continue
            index = sequence.index(item_id)
            for successor in sequence[index + 1:]:
                if successor in assigned:
                    ceiling = min(ceiling, assigned[successor] - 1)
                    break
                if successor in changed:
                    continue
                break
        return ceiling

    @staticmethod
    def _issue_window(item_id, floor, ceiling, windows):
        lower_millis = floor >> 80
        upper_millis = ceiling >> 80
        if lower_millis == upper_millis:
            source = windows.get(item_id)
            if source is not None and source.kind.name == 'SUFFIX':
                return Window.suffix(
                    lower_millis, upper_millis,
                    max(source.lower_suffix, floor & _MAX_SUFFIX),
                    min(source.upper_suffix, ceiling & _MAX_SUFFIX))
            return Window.suffix(
                lower_millis, upper_millis, floor & _MAX_SUFFIX, ceiling & _MAX_SUFFIX)
        return Window.millisecond(lower_millis, upper_millis)

    def _retickets(self, readers, sequences, changed, generation):
        retickets = {}
        for reader in readers:
            if not reader.spec.reticket_allowed:
                continue
            sequence = sequences[reader.reader_id]
            owed = [item_id for item_id in sequence if item_id not in reader.delivered]
            segments = tuple(_contiguous_segments(owed))
            aliases = tuple(
                (item_id, self.ledger.current_ulid(item_id), changed[item_id])
                for item_id in sequence
                if item_id in changed and item_id in reader.delivered)
            retickets[reader.reader_id] = Reticket(
                reader_id=reader.reader_id,
                generation=generation,
                owed_segments=segments,
                aliases=aliases,
                new_bound=self._new_bound(reader, changed))
        return retickets

    def _new_bound(self, reader, changed):
        if reader.bound is None or reader.bound_item is None:
            return consts.MIN_ULID
        bound_item = reader.bound_item
        if bound_item in changed:
            return changed[bound_item]
        if self.ledger.contains(bound_item):
            return self.ledger.current_ulid(bound_item)
        return reader.bound


def _contiguous_segments(ordered_ids):
    if not ordered_ids:
        return
    start = previous = ordered_ids[0]
    for item_id in ordered_ids[1:]:
        yield start, previous
        start = item_id
        previous = item_id
    yield start, previous
