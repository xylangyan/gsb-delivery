"""
    ulid.ledger.model
    ~~~~~~~~~~~~~~~~~

    发货清单子系统的数据模型。

    所有模型都以物理原件编号(:attr:`PhysicalItem.item_id`)为身份; 同一个物理原件转抄到
    别处仍只算一件, 因此补号、交付、改签全部围绕原件编号而不是 ULID 字符串。
"""
import dataclasses
import enum
import typing

from .. import ulid


class ItemState(enum.Enum):
    """
    原件在目录中的状态。
    """

    #: 仅在半成品目录或翻页客户端上见过, 尚未对外确认, 允许补号。
    DRAFT = 'draft'

    #: 已对外确认, ULID 冻结, 永远不能改号。
    CONFIRMED = 'confirmed'

    #: 实物已经交到读者手里, 原号始终有效且不得重复交付。
    DELIVERED = 'delivered'


class ReservationState(enum.Enum):
    """
    号池保留位的状态。
    """

    #: 临时保留, 尚未绑定到成功签发的一代。
    HELD = 'held'

    #: 已经随某一代成功签发, 成为该代的占用位。
    ISSUED = 'issued'

    #: 同一位置被更新一轮的保留接管, 旧保留不再占号。
    SUPERSEDED = 'superseded'

    #: 保留已释放; 晚到的释号只能关闭它自己那一轮的保留, 不能动当前占用位。
    RELEASED = 'released'


@dataclasses.dataclass(frozen=True)
class PhysicalItem:
    """
    一件物理原件及其当前登记。

    :param item_id: 物理原件编号, 全库唯一且永不复用语义
    :type item_id: :class:`~str`
    :param state: 原件状态
    :type state: :class:`~ulid.ledger.model.ItemState`
    :param current_ulid: 当前一代使用的 ULID
    :type current_ulid: :class:`~ulid.ulid.ULID`
    :param original_ulid: 原件最初领到的 ULID, 即便改签也保留可追溯
    :type original_ulid: :class:`~ulid.ulid.ULID`
    :param library_code: 原库编码, 新读者按它与已定序查看
    :type library_code: :class:`~str`
    :param generation: 当前 ULID 所属的代
    :type generation: :class:`~int`
    """

    item_id: str
    state: ItemState
    current_ulid: ulid.ULID
    original_ulid: ulid.ULID
    library_code: str
    generation: int

    @property
    def is_frozen(self) -> bool:
        """
        只有已对外确认的原件冻结, 不允许改号。

        已交付但未确认的原件允许换号: 换号通过等值别名保留原号, 且读者的交付记录
        保证实物不会被再交一回。

        :return: 冻结返回 ``True``
        :rtype: :class:`~bool`
        """
        return self.state is ItemState.CONFIRMED

    def with_ulid(self, value: ulid.ULID, generation: int) -> 'PhysicalItem':
        """
        返回换号后的新登记, 原件编号与最初 ULID 保持不变。

        :param value: 新一代 ULID
        :type value: :class:`~ulid.ulid.ULID`
        :param generation: 新代编号
        :type generation: :class:`~int`
        :return: 新的原件登记
        :rtype: :class:`~ulid.ledger.model.PhysicalItem`
        """
        return dataclasses.replace(
            self, current_ulid=value, generation=generation)


@dataclasses.dataclass(frozen=True)
class ReaderSpec:
    """
    一个读者原来的范围与原件排序条件。

    :param reader_id: 读者编号
    :type reader_id: :class:`~str`
    :param scope: 读者有权查看的原件编号集合
    :type scope: :class:`~frozenset`
    :param order_key: 原件编号的排序键函数(在范围内部使用)
    :type order_key: :class:`~typing.Callable`
    :param reticket_allowed: 是否获准改签为新格式号码
    :type reticket_allowed: :class:`~bool`
    """

    reader_id: str
    scope: typing.FrozenSet[str]
    order_key: typing.Callable[[str], typing.Any] = dataclasses.field(default=lambda value: value)
    reticket_allowed: bool = False


@dataclasses.dataclass(frozen=True)
class Page:
    """
    一次翻页交给读者的内容。

    :param generation: 页面使用的目录代
    :type generation: :class:`~int`
    :param lower_bound: 本页之后续读使用的旧 ULID 界线(排他上界)
    :type lower_bound: :class:`~ulid.ulid.ULID`
    :param entries: 页内条目, 每项为 ``(原件编号, ULID)``
    :type entries: :class:`~typing.Tuple`
    """

    generation: int
    lower_bound: ulid.ULID
    entries: typing.Tuple[typing.Tuple[str, ulid.ULID], ...]

    @property
    def item_ids(self) -> typing.Tuple[str, ...]:
        """
        本页涉及的全部原件编号。

        :return: 原件编号元组
        :rtype: :class:`~typing.Tuple`
        """
        return tuple(entry[0] for entry in self.entries)


@dataclasses.dataclass(frozen=True)
class ResumeRequest:
    """
    客户端续读请求: 旧 ULID 界线加签过的页。

    :param reader_id: 发起续读的读者
    :type reader_id: :class:`~str`
    :param old_bound: 客户端手上的旧 ULID 界线
    :type old_bound: :class:`~ulid.ulid.ULID`
    :param signed_pages: 已经签过的页代与页内原件编号
    :type signed_pages: :class:`~typing.Tuple`
    """

    reader_id: str
    old_bound: ulid.ULID
    signed_pages: typing.Tuple[typing.Tuple[int, typing.Tuple[str, ...]], ...]


@dataclasses.dataclass(frozen=True)
class Reticket:
    """
    新格式改签票。

    :param reader_id: 获准改签的读者
    :type reader_id: :class:`~str`
    :param generation: 改签票所属的新代
    :type generation: :class:`~int`
    :param owed_segments: 尚欠的连续原件区段(按读者排序), 每项为原件编号区间
    :type owed_segments: :class:`~typing.Tuple`
    :param aliases: 已收到原件的等值别名 ``(原件编号, 旧 ULID, 新 ULID)``
    :type aliases: :class:`~typing.Tuple`
    :param new_bound: 改签后使用的新界线
    :type new_bound: :class:`~ulid.ulid.ULID`
    """

    reader_id: str
    generation: int
    owed_segments: typing.Tuple[typing.Tuple[str, str], ...]
    aliases: typing.Tuple[typing.Tuple[str, ulid.ULID, ulid.ULID], ...]
    new_bound: ulid.ULID


@dataclasses.dataclass(frozen=True)
class AuditBlock:
    """
    不能改签的核对记录: 尚未送达的单必定掉到旧界线之后时留下。

    :param reader_id: 受影响读者
    :type reader_id: :class:`~str`
    :param bound: 无法越过的旧界线
    :type bound: :class:`~ulid.ulid.ULID`
    :param pending_items: 待补原件编号(按读者排序)
    :type pending_items: :class:`~typing.Tuple`
    """

    reader_id: str
    bound: ulid.ULID
    pending_items: typing.Tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Publication:
    """
    一次发布: 新的对照目录与原进度映射必须一起发布。

    :param generation: 本次发布的代
    :type generation: :class:`~int`
    :param catalog: 原件编号到新 ULID 的对照目录
    :type catalog: :class:`~typing.Dict`
    :param progress_map: 原进度映射, 旧代界线/旧 ULID 到新代的对应关系
    :type progress_map: :class:`~typing.Dict`
    :param alias_map: 旧 ULID 到新 ULID 的等值别名
    :type alias_map: :class:`~typing.Dict`
    """

    generation: int
    catalog: typing.Dict[str, ulid.ULID]
    progress_map: typing.Dict[typing.Tuple[str, typing.Any], typing.Any]
    alias_map: typing.Dict[ulid.ULID, ulid.ULID]


@dataclasses.dataclass(frozen=True)
class ExportBook:
    """
    导出簿: 原件号、真正交付与只有预读的区段、每个读者原来的范围和排序条件。

    :param items: 原件编号到原件登记的映射
    :type items: :class:`~typing.Dict`
    :param delivered: 每个读者已真正交付的原件编号
    :type delivered: :class:`~typing.Dict`
    :param previewed: 每个读者只预读(翻过页但未交付)的原件编号
    :type previewed: :class:`~typing.Dict`
    :param reader_specs: 每个读者原来的范围与排序条件
    :type reader_specs: :class:`~typing.Dict`
    """

    items: typing.Dict[str, PhysicalItem]
    delivered: typing.Dict[str, typing.FrozenSet[str]]
    previewed: typing.Dict[str, typing.FrozenSet[str]]
    reader_specs: typing.Dict[str, ReaderSpec]
