"""
    ulid.ledger.numbering
    ~~~~~~~~~~~~~~~~~~~~~

    号池: 轮次、临时保留、租约令牌与代签发。

    规则:
        * 临时保留和下一个相同保留可能来自不同轮; 晚到的释号只能关闭它自己那一轮的
          保留, 当前还占着位置的新保留不受影响。
        * 只有成功签发进某一代的位置才会在重开后继续被承认; 重开只走已签发的代。
        * 后缀窗口可能极长, 落点由哈希在窗口跨度内取模得到, 绝不逐号扫描填空白。
"""
import dataclasses
import hashlib
import itertools
import typing

from .. import consts, ulid
from .clock import ControlledClock
from .model import ReservationState
from .ordering import Window, WindowKind

#: 碰撞后重新哈希落点的最大尝试次数, 每次尝试都是独立哈希而不是逐号枚举。
MAX_PROBES = 128

_RANDOMNESS_SPAN = consts.MAX_RANDOMNESS.int + 1
_TIMESTAMP_SPAN = consts.MAX_TIMESTAMP.int + 1


@dataclasses.dataclass
class Reservation:
    """
    一次临时保留。

    :param token: 租约令牌, 释号时必须原样交回
    :type token: :class:`~int`
    :param round_id: 保留所属轮次
    :type round_id: :class:`~str`
    :param position: 保留的 128 位 ULID 整数位置
    :type position: :class:`~int`
    :param tag: 调用方给出的稳定标签(通常是原件编号), 让落点可重现
    :type tag: :class:`~str`
    :param state: 保留状态
    :type state: :class:`~ulid.ledger.model.ReservationState`
    """

    token: int
    round_id: str
    position: int
    tag: str
    state: ReservationState


class NumberingService:
    """
    带轮次与代概念的 ULID 号池。

    :param clock: 可控毫秒时钟
    :type clock: :class:`~ulid.ledger.clock.ControlledClock`
    :param issued_generations: 重开时带入的、已成功签发的代及其占用位置
    :type issued_generations: :class:`~typing.Dict`
    """

    def __init__(self,
                 clock: typing.Optional[ControlledClock] = None,
                 issued_generations: typing.Optional[typing.Dict[int, typing.FrozenSet[int]]] = None) -> None:
        self.clock = clock if clock is not None else ControlledClock()
        self._tokens = itertools.count(1)
        self._reservations = {}  # type: typing.Dict[int, Reservation]
        self._round_positions = {}  # type: typing.Dict[str, typing.Dict[int, int]]
        self._position_owner = {}  # type: typing.Dict[int, int]
        self._generations = {
            generation: set(positions)
            for generation, positions in (issued_generations or {}).items()
        }  # type: typing.Dict[int, typing.Set[int]]

    def _issued_positions(self) -> typing.FrozenSet[int]:
        issued = set()  # type: typing.Set[int]
        for positions in self._generations.values():
            issued.update(positions)
        return frozenset(issued)

    def _prepare_position(self, position: int, round_id: str) -> bool:
        """
        让 ``round_id`` 可以占用 ``position``。

        已签发位置永久占用; 同一轮仍有效的保留也算占用; 其它轮仍有效的保留会被接管,
        其旧保留置为 SUPERSEDED。返回 ``False`` 表示该位置当前轮不可用。

        :rtype: :class:`~bool`
        """
        if position in self._issued_positions():
            return False
        owner = self._position_owner.get(position)
        if owner is None:
            return True
        previous = self._reservations[owner]
        if previous.state is ReservationState.HELD and previous.round_id == round_id:
            return False
        if previous.state is ReservationState.HELD:
            previous.state = ReservationState.SUPERSEDED
            self._round_positions[previous.round_id].pop(position, None)
        self._position_owner.pop(position, None)
        return True

    @staticmethod
    def _digest(tag: str, round_id: str, attempt: int) -> int:
        payload = '{}|{}|{}'.format(round_id, tag, attempt).encode('utf-8')
        digest = hashlib.blake2b(payload, digest_size=16).digest()
        return int.from_bytes(digest, byteorder='big')

    def _candidate(self, window: Window, tag: str, round_id: str, attempt: int) -> int:
        entropy = self._digest(tag, round_id, attempt)
        if window.kind is WindowKind.OPEN:
            millis = self.clock.millis
            suffix = entropy % _RANDOMNESS_SPAN
        elif window.kind is WindowKind.MILLISECOND:
            span = window.upper_millis - window.lower_millis + 1
            millis = window.lower_millis + (entropy % span)
            suffix = (entropy // span) % _RANDOMNESS_SPAN
        else:
            millis_span = window.upper_millis - window.lower_millis + 1
            suffix_span = window.upper_suffix - window.lower_suffix + 1
            millis = window.lower_millis + (entropy % millis_span)
            suffix = window.lower_suffix + ((entropy // millis_span) % suffix_span)
        return (millis << 80) | suffix

    def hold(self,
             round_id: str,
             window: typing.Optional[Window] = None,
             tag: typing.Optional[str] = None,
             guard: typing.Optional[typing.Callable[[int], bool]] = None) -> typing.Tuple[int, ulid.ULID]:
        """
        在给定轮次中临时保留一个窗口内的 ULID 位置。

        :param round_id: 轮次标识
        :type round_id: :class:`~str`
        :param window: 允许落点的窗口, 缺省为开放窗口
        :type window: :class:`~ulid.ledger.ordering.Window` or :class:`~None`
        :param tag: 稳定标签(通常为原件编号), 保证落点可重现
        :type tag: :class:`~str` or :class:`~None`
        :param guard: 可选的 128 位位置守卫, 返回 ``False`` 的位置继续重新哈希
        :type guard: :class:`~typing.Callable` or :class:`~None`
        :return: ``(租约令牌, 保留的 ULID)``
        :rtype: :class:`~typing.Tuple`
        :raises RuntimeError: 多次哈希落点仍然冲突
        """
        window = window if window is not None else Window.open()
        tag = tag if tag is not None else 'tag-{}'.format(next(self._tokens))
        round_positions = self._round_positions.setdefault(round_id, {})
        for attempt in range(MAX_PROBES):
            position = self._candidate(window, tag, round_id, attempt)
            value = ulid.ULID(position.to_bytes(16, byteorder='big'))
            if not window.contains(value):
                continue
            if guard is not None and not guard(position):
                continue
            if not self._prepare_position(position, round_id):
                continue
            token = next(self._tokens)
            reservation = Reservation(token, round_id, position, tag, ReservationState.HELD)
            self._reservations[token] = reservation
            round_positions[position] = token
            self._position_owner[position] = token
            return token, value
        raise RuntimeError('Unable to obtain free ULID position within window after {} probes'.format(MAX_PROBES))

    def release(self, token: int) -> bool:
        """
        凭租约令牌释号。

        晚到的释号如果对应保留已被另一轮的新保留取代, 不会释放当前占用位。

        :param token: 保留时发放的租约令牌
        :type token: :class:`~int`
        :return: 成功释放返回 ``True``, 令牌无效或保留已结束返回 ``False``
        :rtype: :class:`~bool`
        """
        reservation = self._reservations.get(token)
        if reservation is None or reservation.state is not ReservationState.HELD:
            return False
        reservation.state = ReservationState.RELEASED
        self._round_positions[reservation.round_id].pop(reservation.position, None)
        if self._position_owner.get(reservation.position) == token:
            self._position_owner.pop(reservation.position, None)
        return True

    def reservation(self, token: int) -> Reservation:
        """
        按令牌查保留。

        :raises KeyError: 令牌不存在
        """
        return self._reservations[token]

    def has_round(self, round_id: str) -> bool:
        """
        判定轮次是否至少建立过保留记录。

        :rtype: :class:`~bool`
        """
        return round_id in self._round_positions

    def hold_position(self,
                      round_id: str,
                      position: int,
                      tag: str,
                      ceiling: typing.Optional[int] = None
                      ) -> typing.Tuple[int, ulid.ULID]:
        """
        在精确的 128 位位置(或其后第一个空闲位)保留, 哈希落点失败时的确定性兜底。

        兜底只在调用方给出的可行区间 ``[position, ceiling]`` 内向前找, 扫描次数受
        :data:`MAX_PROBES` 限制, 不会对超长窗口做全量填空。

        :param round_id: 轮次标识
        :type round_id: :class:`~str`
        :param position: 期望的起始位置
        :type position: :class:`~int`
        :param tag: 稳定标签
        :type tag: :class:`~str`
        :param ceiling: 允许占用的最高位置(含)
        :type ceiling: :class:`~int` or :class:`~None`
        :return: ``(租约令牌, 保留的 ULID)``
        :rtype: :class:`~typing.Tuple`
        :raises RuntimeError: 区间内没有空闲位置
        """
        ceiling = position if ceiling is None else ceiling
        round_positions = self._round_positions.setdefault(round_id, {})
        interval_length = ceiling - position + 1
        scan_limit = min(interval_length, MAX_PROBES) if interval_length > MAX_PROBES else interval_length
        for offset in range(scan_limit):
            candidate_position = position + offset
            if not self._prepare_position(candidate_position, round_id):
                continue
            token = next(self._tokens)
            reservation = Reservation(
                token, round_id, candidate_position, tag, ReservationState.HELD)
            self._reservations[token] = reservation
            round_positions[candidate_position] = token
            self._position_owner[candidate_position] = token
            return token, ulid.ULID(candidate_position.to_bytes(16, byteorder='big'))
        raise RuntimeError('No free position in [{}, {}] for {}'.format(position, ceiling, tag))

    def issue(self, generation: int, round_id: str) -> typing.Dict[str, ulid.ULID]:
        """
        把某一轮仍然有效的全部保留签发给指定代。

        签发成功后这些位置成为该代的永久占用, 重开时继续有效。

        :param generation: 目标代
        :type generation: :class:`~int`
        :param round_id: 要签发的轮次
        :type round_id: :class:`~str`
        :return: ``标签 -> ULID`` 的签发结果
        :rtype: :class:`~typing.Dict`
        :raises KeyError: 轮次不存在
        """
        if round_id not in self._round_positions:
            raise KeyError('Unknown round {}'.format(round_id))
        issued = self._generations.setdefault(generation, set())
        result = {}  # type: typing.Dict[str, ulid.ULID]
        for position, token in list(self._round_positions[round_id].items()):
            reservation = self._reservations[token]
            if reservation.state is not ReservationState.HELD:
                continue
            reservation.state = ReservationState.ISSUED
            issued.add(position)
            result[reservation.tag] = ulid.ULID(position.to_bytes(16, byteorder='big'))
        return result

    def issued_positions(self, generation: int) -> typing.FrozenSet[int]:
        """
        返回某代已成功签发的全部位置。

        :rtype: :class:`~frozenset`
        """
        return frozenset(self._generations.get(generation, set()))

    def issued_generations(self) -> typing.Tuple[int, ...]:
        """
        全部至少有一个签发位置的代, 按代号排序。

        :rtype: :class:`~typing.Tuple`
        """
        return tuple(sorted(generation for generation, positions in self._generations.items() if positions))

    def reopen(self) -> 'NumberingService':
        """
        重开一个号池: 只承认已经成功签发的代及其占用位置, 临时保留全部丢弃。

        :return: 新的号池实例, 共享同一时钟
        :rtype: :class:`~ulid.ledger.numbering.NumberingService`
        """
        surviving = {
            generation: frozenset(positions)
            for generation, positions in self._generations.items() if positions
        }
        return NumberingService(self.clock, surviving)
