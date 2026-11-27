"""
    ulid.ledger.ordering
    ~~~~~~~~~~~~~~~~~~~~

    部分先后关系与毫秒/后缀窗口。

    某些单子的顺序只确定到部分先后: 有边相连的原件必须按边排序, 没有关系的原件之间
    允许任意稳定次序。补号还受毫秒窗口(时间戳 48 位)与后缀窗口(随机量 80 位)限制;
    后缀窗口可能极长, 任何算法都不得逐号扫描填空白。
"""
import enum
import typing

from .. import consts, ulid


class WindowKind(enum.Enum):
    """
    窗口约束的种类。
    """

    #: 无窗口限制, 仅受偏序约束。
    OPEN = 'open'

    #: 限制 ULID 时间戳(毫秒)所在区间。
    MILLISECOND = 'millisecond'

    #: 在毫秒区间之上, 再限制随机后缀所在区间。
    SUFFIX = 'suffix'


class Window:
    """
    一个原件补号时允许落入的 ULID 窗口。

    毫秒边界与后缀边界均为闭区间, 任一边界为 ``None`` 表示该侧不限。窗口只做成员判定
    与随机落点, 绝不枚举区间内的全部号码。

    :param kind: 窗口种类
    :type kind: :class:`~ulid.ledger.ordering.WindowKind`
    :param lower_millis: 时间戳下界(含), 毫秒
    :type lower_millis: :class:`~int` or :class:`~None`
    :param upper_millis: 时间戳上界(含), 毫秒
    :type upper_millis: :class:`~int` or :class:`~None`
    :param lower_suffix: 随机后缀下界(含), 80 位整数
    :type lower_suffix: :class:`~int` or :class:`~None`
    :param upper_suffix: 随机后缀上界(含), 80 位整数
    :type upper_suffix: :class:`~int` or :class:`~None`
    """

    __slots__ = ['kind', 'lower_millis', 'upper_millis', 'lower_suffix', 'upper_suffix']

    def __init__(self,
                 kind: WindowKind = WindowKind.OPEN,
                 lower_millis: typing.Optional[int] = None,
                 upper_millis: typing.Optional[int] = None,
                 lower_suffix: typing.Optional[int] = None,
                 upper_suffix: typing.Optional[int] = None) -> None:
        if lower_millis is not None and upper_millis is not None and lower_millis > upper_millis:
            raise ValueError('Window millis bounds are inverted')
        if lower_suffix is not None and upper_suffix is not None and lower_suffix > upper_suffix:
            raise ValueError('Window suffix bounds are inverted')
        if kind is WindowKind.MILLISECOND and (lower_millis is None or upper_millis is None):
            raise ValueError('Millisecond window needs both millis bounds')
        if kind is WindowKind.SUFFIX and (
                lower_millis is None or upper_millis is None
                or lower_suffix is None or upper_suffix is None):
            raise ValueError('Suffix window needs millis and suffix bounds')
        for value in (lower_suffix, upper_suffix):
            if value is not None and not 0 <= value <= consts.MAX_RANDOMNESS.int:
                raise ValueError('Suffix bound outside 80-bit range')
        for value in (lower_millis, upper_millis):
            if value is not None and not 0 <= value <= consts.MAX_TIMESTAMP.int:
                raise ValueError('Millis bound outside 48-bit range')
        self.kind = kind
        self.lower_millis = lower_millis
        self.upper_millis = upper_millis
        self.lower_suffix = lower_suffix
        self.upper_suffix = upper_suffix

    @classmethod
    def open(cls) -> 'Window':
        """
        创建一个无限制窗口。

        :return: 开放窗口
        :rtype: :class:`~ulid.ledger.ordering.Window`
        """
        return cls(WindowKind.OPEN)

    @classmethod
    def millisecond(cls, lower_millis: int, upper_millis: int) -> 'Window':
        """
        创建一个毫秒窗口。

        :param lower_millis: 时间戳下界(含)
        :type lower_millis: :class:`~int`
        :param upper_millis: 时间戳上界(含)
        :type upper_millis: :class:`~int`
        :return: 毫秒窗口
        :rtype: :class:`~ulid.ledger.ordering.Window`
        """
        return cls(WindowKind.MILLISECOND, lower_millis, upper_millis)

    @classmethod
    def suffix(cls,
               lower_millis: int,
               upper_millis: int,
               lower_suffix: int,
               upper_suffix: int) -> 'Window':
        """
        创建一个带后缀边界的窗口(同时含毫秒边界)。

        :return: 后缀窗口
        :rtype: :class:`~ulid.ledger.ordering.Window`
        """
        return cls(WindowKind.SUFFIX, lower_millis, upper_millis, lower_suffix, upper_suffix)

    def contains(self, value: ulid.ULID) -> bool:
        """
        判定给定 ULID 是否落在窗口内。

        :param value: 待判定的 ULID
        :type value: :class:`~ulid.ulid.ULID`
        :return: 落在窗口内返回 ``True``
        :rtype: :class:`~bool`
        """
        if self.kind is WindowKind.OPEN:
            return True
        millis = value.timestamp().int
        if self.lower_millis is not None and millis < self.lower_millis:
            return False
        if self.upper_millis is not None and millis > self.upper_millis:
            return False
        if self.kind is WindowKind.SUFFIX:
            suffix = value.randomness().int
            if self.lower_suffix is not None and suffix < self.lower_suffix:
                return False
            if self.upper_suffix is not None and suffix > self.upper_suffix:
                return False
        return True

    def __repr__(self) -> str:
        return ('Window(kind={!r}, millis=[{!r},{!r}], suffix=[{!r},{!r}])').format(
            self.kind, self.lower_millis, self.upper_millis, self.lower_suffix, self.upper_suffix)


class PartialOrder:
    """
    原件之间的部分先后关系。

    边 ``a -> b`` 表示原件 ``a`` 必须排在原件 ``b`` 之前。关系按传递闭包比较,
    无边相连的两个原件互不可比, 排序时回退到调用方给定的稳定键。
    """

    __slots__ = ['_successors']

    def __init__(self) -> None:
        self._successors = {}  # type: typing.Dict[str, typing.Set[str]]

    def add_before(self, before_id: str, after_id: str) -> None:
        """
        声明 ``before_id`` 必须排在 ``after_id`` 之前。

        :param before_id: 在前的原件编号
        :type before_id: :class:`~str`
        :param after_id: 在后的原件编号
        :type after_id: :class:`~str`
        """
        if before_id == after_id:
            raise ValueError('An item cannot be ordered before itself')
        self._successors.setdefault(before_id, set()).add(after_id)
        self._successors.setdefault(after_id, set())

    def is_before(self, before_id: str, after_id: str) -> bool:
        """
        按传递闭包判定 ``before_id`` 是否必须先于 ``after_id``。

        :return: 存在先后约束返回 ``True``
        :rtype: :class:`~bool`
        """
        if before_id == after_id:
            return False
        seen = set()  # type: typing.Set[str]
        stack = list(self._successors.get(before_id, ()))
        while stack:
            current = stack.pop()
            if current == after_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self._successors.get(current, ()))
        return False

    def predecessors(self, item_id: str) -> typing.FrozenSet[str]:
        """
        返回必须排在给定原件之前的全部原件(传递闭包)。

        :param item_id: 原件编号
        :type item_id: :class:`~str`
        :return: 前驱原件编号集合
        :rtype: :class:`~frozenset`
        """
        result = set()  # type: typing.Set[str]
        for node in list(self._successors):
            if node != item_id and self.is_before(node, item_id):
                result.add(node)
        return frozenset(result)

    def successors(self, item_id: str) -> typing.FrozenSet[str]:
        """
        返回必须排在给定原件之后的全部原件(传递闭包)。

        :param item_id: 原件编号
        :type item_id: :class:`~str`
        :return: 后继原件编号集合
        :rtype: :class:`~frozenset`
        """
        result = set()  # type: typing.Set[str]
        stack = list(self._successors.get(item_id, ()))
        seen = set()  # type: typing.Set[str]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            result.add(current)
            stack.extend(self._successors.get(current, ()))
        return frozenset(result)

    def topo_sort(self,
                  item_ids: typing.Iterable[str],
                  key: typing.Optional[typing.Callable[[str], typing.Any]] = None) -> typing.List[str]:
        """
        在尊重全部先后边的前提下做稳定拓扑排序。

        互不可比的原件按 ``key`` (缺省按原件编号本身) 决定先后, 保证结果确定。

        :param item_ids: 待排序原件编号
        :type item_ids: :class:`~typing.Iterable`
        :param key: 稳定次序键函数
        :type key: :class:`~typing.Callable` or :class:`~None`
        :return: 排好序的原件编号列表
        :rtype: :class:`~typing.List`
        :raises ValueError: 当先后关系存在环时
        """
        nodes = list(item_ids)
        node_set = set(nodes)
        indegree = {node: 0 for node in nodes}
        adjacency = {node: [] for node in nodes}  # type: typing.Dict[str, typing.List[str]]
        for node in nodes:
            for successor in self._successors.get(node, ()):
                if successor in node_set:
                    adjacency[node].append(successor)
                    indegree[successor] += 1
        ordered = sorted(
            (node for node in nodes if indegree[node] == 0),
            key=key if key is not None else lambda value: value)
        result = []  # type: typing.List[str]
        while ordered:
            current = ordered.pop(0)
            result.append(current)
            for successor in adjacency[current]:
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    ordered.append(successor)
            ordered.sort(key=key if key is not None else lambda value: value)
        if len(result) != len(nodes):
            raise ValueError('Partial order contains a cycle')
        return result


class Ordering:
    """
    组合偏序与逐原件窗口的排序/校验视图。

    :param partial: 原件间的部分先后关系
    :type partial: :class:`~ulid.ledger.ordering.PartialOrder`
    :param windows: 原件编号到窗口的映射
    :type windows: :class:`~typing.Dict`
    """

    __slots__ = ['partial', 'windows']

    def __init__(self,
                 partial: typing.Optional[PartialOrder] = None,
                 windows: typing.Optional[typing.Dict[str, Window]] = None) -> None:
        self.partial = partial if partial is not None else PartialOrder()
        self.windows = windows if windows is not None else {}

    def window_for(self, item_id: str) -> Window:
        """
        返回原件的窗口; 未登记的原件得到开放窗口。

        :param item_id: 原件编号
        :type item_id: :class:`~str`
        :return: 对应窗口
        :rtype: :class:`~ulid.ledger.ordering.Window`
        """
        return self.windows.get(item_id, Window.open())

    def sort(self,
             item_ids: typing.Iterable[str],
             key: typing.Optional[typing.Callable[[str], typing.Any]] = None) -> typing.List[str]:
        """
        按偏序与稳定键排序原件编号。

        :return: 排序后的原件编号列表
        :rtype: :class:`~typing.List`
        """
        return self.partial.topo_sort(item_ids, key)

    def validate(self, item_id: str, value: ulid.ULID) -> None:
        """
        校验给原件分配的 ULID 满足其窗口约束。

        :param item_id: 原件编号
        :type item_id: :class:`~str`
        :param value: 拟分配的 ULID
        :type value: :class:`~ulid.ulid.ULID`
        :raises ValueError: 当 ULID 落在窗口之外
        """
        if not self.window_for(item_id).contains(value):
            raise ValueError('ULID {} outside window of item {}'.format(value.str, item_id))
