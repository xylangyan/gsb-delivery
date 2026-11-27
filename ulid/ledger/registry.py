"""
    ulid.ledger.registry
    ~~~~~~~~~~~~~~~~~~~~

    对照目录登记: 原件编号、状态、各代 ULID 与等值别名历史。

    身份始终是物理原件编号; ULID 只是某一代的标签。已确认/已交付的原件冻结,
    任何改签都不得改动它们的号。
"""
import dataclasses
import typing

from .. import ulid
from .model import ItemState, PhysicalItem
from .ordering import Ordering


class Ledger:
    """
    内存中的对照目录, 支持多代 ULID 与原件状态流转。

    :param ordering: 偏序与窗口视图, 换号时做窗口校验
    :type ordering: :class:`~ulid.ledger.ordering.Ordering`
    """

    def __init__(self, ordering: typing.Optional[Ordering] = None) -> None:
        self.ordering = ordering if ordering is not None else Ordering()
        self._items = {}  # type: typing.Dict[str, PhysicalItem]
        self._occupancy = {}  # type: typing.Dict[typing.Tuple[int, int], str]
        self._aliases = {}  # type: typing.Dict[str, typing.List[typing.Tuple[int, ulid.ULID, ulid.ULID]]]

    def register(self,
                 item_id: str,
                 value: ulid.ULID,
                 state: ItemState = ItemState.DRAFT,
                 library_code: typing.Optional[str] = None,
                 generation: int = 0) -> PhysicalItem:
        """
        把一件物理原件登记进目录。

        :param item_id: 物理原件编号
        :type item_id: :class:`~str`
        :param value: 初始 ULID
        :type value: :class:`~ulid.ulid.ULID`
        :param state: 初始状态
        :type state: :class:`~ulid.ledger.model.ItemState`
        :param library_code: 原库编码, 缺省与原件编号相同
        :type library_code: :class:`~str` or :class:`~None`
        :param generation: 初始代
        :type generation: :class:`~int`
        :return: 登记后的原件
        :rtype: :class:`~ulid.ledger.model.PhysicalItem`
        :raises ValueError: 原件编号或代内 ULID 冲突
        """
        if item_id in self._items:
            raise ValueError('Item {} already registered'.format(item_id))
        self._require_free(generation, value)
        self.ordering.validate(item_id, value)
        item = PhysicalItem(
            item_id=item_id,
            state=state,
            current_ulid=value,
            original_ulid=value,
            library_code=library_code if library_code is not None else item_id,
            generation=generation)
        self._items[item_id] = item
        self._occupancy[(generation, value.int)] = item_id
        self._aliases[item_id] = []
        return item

    def _require_free(self, generation: int, value: ulid.ULID) -> None:
        owner = self._occupancy.get((generation, value.int))
        if owner is not None:
            raise ValueError('ULID {} already used by item {} in generation {}'.format(
                value.str, owner, generation))

    def contains(self, item_id: str) -> bool:
        """
        判定原件是否已登记。

        :return: 已登记返回 ``True``
        :rtype: :class:`~bool`
        """
        return item_id in self._items

    def get(self, item_id: str) -> PhysicalItem:
        """
        取原件登记。

        :raises KeyError: 原件未登记
        """
        return self._items[item_id]

    def current_ulid(self, item_id: str) -> ulid.ULID:
        """
        取原件当前一代的 ULID。

        :rtype: :class:`~ulid.ulid.ULID`
        """
        return self._items[item_id].current_ulid

    def ids(self) -> typing.Tuple[str, ...]:
        """
        全部已登记原件编号。

        :rtype: :class:`~typing.Tuple`
        """
        return tuple(self._items)

    def items(self) -> typing.Tuple[PhysicalItem, ...]:
        """
        全部原件登记。

        :rtype: :class:`~typing.Tuple`
        """
        return tuple(self._items.values())

    def snapshot(self) -> typing.Dict[str, PhysicalItem]:
        """
        原件编号到登记的快照副本。

        :rtype: :class:`~typing.Dict`
        """
        return dict(self._items)

    def confirm(self, item_id: str) -> PhysicalItem:
        """
        把原件标记为已对外确认, 此后冻结。

        :return: 更新后的原件登记
        :rtype: :class:`~ulid.ledger.model.PhysicalItem`
        """
        item = self._items[item_id]
        if item.state is ItemState.DRAFT:
            item = dataclasses.replace(item, state=ItemState.CONFIRMED)
            self._items[item_id] = item
        return item

    def deliver(self, item_id: str) -> PhysicalItem:
        """
        记录原件已实物交付。

        草稿件转为已交付(仍可换号, 但会留等值别名且不重复交付); 已确认件保持确认状态,
        号继续冻结。

        :return: 更新后的原件登记
        :rtype: :class:`~ulid.ledger.model.PhysicalItem`
        """
        item = self._items[item_id]
        if item.state is ItemState.DRAFT:
            item = dataclasses.replace(item, state=ItemState.DELIVERED)
            self._items[item_id] = item
        return item

    def reassign(self, item_id: str, value: ulid.ULID, generation: int) -> PhysicalItem:
        """
        给未冻结原件换发新一代 ULID, 并记录等值别名。

        :param item_id: 原件编号
        :type item_id: :class:`~str`
        :param value: 新 ULID
        :type value: :class:`~ulid.ulid.ULID`
        :param generation: 新代编号
        :type generation: :class:`~int`
        :return: 换号后的原件登记
        :rtype: :class:`~ulid.ledger.model.PhysicalItem`
        :raises ValueError: 原件冻结、ULID 冲突或落入窗口之外
        """
        item = self._items[item_id]
        if item.is_frozen:
            raise ValueError('Frozen item {} cannot be renumbered'.format(item_id))
        if value == item.current_ulid:
            return item
        self._require_free(generation, value)
        self.ordering.validate(item_id, value)
        old_value = item.current_ulid
        self._occupancy.pop((item.generation, old_value.int), None)
        updated = item.with_ulid(value, generation)
        self._items[item_id] = updated
        self._occupancy[(generation, value.int)] = item_id
        self._aliases[item_id].append((generation, old_value, value))
        return updated

    def alias_history(self, item_id: str) -> typing.Tuple[typing.Tuple[int, ulid.ULID, ulid.ULID], ...]:
        """
        原件历次换号的 ``(代, 旧 ULID, 新 ULID)`` 记录。

        :rtype: :class:`~typing.Tuple`
        """
        return tuple(self._aliases[item_id])

    def resolve(self, value: typing.Union[ulid.ULID, str, int]) -> typing.Optional[str]:
        """
        把任意一代的 ULID 解析回原件编号; 无法识别返回 ``None``。

        :param value: ULID 实例、Base32 字符串或整数
        :type value: :class:`~ulid.ulid.ULID` or :class:`~str` or :class:`~int`
        :return: 原件编号
        :rtype: :class:`~str` or :class:`~None`
        """
        candidate = _coerce_ulid(value)
        for item in self._items.values():
            if item.current_ulid == candidate or item.original_ulid == candidate:
                return item.item_id
        for item_id, history in self._aliases.items():
            for _generation, old_value, new_value in history:
                if old_value == candidate or new_value == candidate:
                    return item_id
        return None

    def catalog(self, generation: typing.Optional[int] = None) -> typing.Dict[str, ulid.ULID]:
        """
        导出 ``原件编号 -> ULID`` 对照目录; 可限定代。

        :param generation: 只包含该代的原件, ``None`` 表示全部当前登记
        :type generation: :class:`~int` or :class:`~None`
        :rtype: :class:`~typing.Dict`
        """
        result = {}
        for item_id, item in self._items.items():
            if generation is None or item.generation == generation:
                result[item_id] = item.current_ulid
        return result

    def frozen_ids(self) -> typing.FrozenSet[str]:
        """
        全部冻结原件编号。

        :rtype: :class:`~frozenset`
        """
        return frozenset(item_id for item_id, item in self._items.items() if item.is_frozen)

    def draft_ids(self) -> typing.FrozenSet[str]:
        """
        全部仍可补号的草稿原件编号。

        :rtype: :class:`~frozenset`
        """
        return frozenset(
            item_id for item_id, item in self._items.items() if item.state is ItemState.DRAFT)


def _coerce_ulid(value: typing.Union[ulid.ULID, str, int]) -> ulid.ULID:
    if isinstance(value, ulid.ULID):
        return value
    if isinstance(value, str):
        from .. import base32

        return ulid.ULID(base32.decode_ulid(value))
    if isinstance(value, int):
        return ulid.ULID(value.to_bytes(16, byteorder='big'))
    raise TypeError('Cannot coerce {} to ULID'.format(type(value).__name__))
