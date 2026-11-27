"""
    ulid.ledger.reader
    ~~~~~~~~~~~~~~~~~~

    读者、翻页、签收与交付。

    每个读者只在自己的范围与排序条件内前进, 进度彼此独立, 不共用通用新界线。
    交付以物理原件编号去重: 已到手实物不因换号再交一次, 未送达每件只出现一次。
"""
import dataclasses
import typing

from .. import ulid
from .model import Page, ReaderSpec, ResumeRequest
from .ordering import Ordering
from .registry import Ledger


@dataclasses.dataclass
class Reader:
    """
    一个翻页客户端的服务端进度。

    :param reader_id: 读者编号
    :type reader_id: :class:`~str`
    :param spec: 读者的范围与排序条件
    :type spec: :class:`~ulid.ledger.model.ReaderSpec`
    """

    reader_id: str
    spec: ReaderSpec
    bound: typing.Optional[ulid.ULID] = None
    bound_item: typing.Optional[str] = None
    signed_pages: typing.List[typing.Tuple[int, typing.Tuple[str, ...]]] = dataclasses.field(default_factory=list)
    delivered: typing.Set[str] = dataclasses.field(default_factory=set)
    previewed: typing.Set[str] = dataclasses.field(default_factory=set)
    seen: typing.Set[str] = dataclasses.field(default_factory=set)

    def resume_request(self) -> ResumeRequest:
        """
        按当前进度构造续读请求(旧 ULID 界线加签过的页)。

        :rtype: :class:`~ulid.ledger.model.ResumeRequest`
        :raises ValueError: 尚无任何界线时无法续读
        """
        if self.bound is None:
            raise ValueError('Reader {} has no bound yet'.format(self.reader_id))
        return ResumeRequest(
            reader_id=self.reader_id,
            old_bound=self.bound,
            signed_pages=tuple(self.signed_pages))


class ReaderService:
    """
    在给定目录上为读者翻页、签收和交付。

    :param ledger: 对照目录
    :type ledger: :class:`~ulid.ledger.registry.Ledger`
    :param ordering: 偏序视图, 缺省使用目录自带视图
    :type ordering: :class:`~ulid.ledger.ordering.Ordering` or :class:`~None`
    """

    def __init__(self, ledger: Ledger, ordering: typing.Optional[Ordering] = None) -> None:
        self.ledger = ledger
        self.ordering = ordering if ordering is not None else ledger.ordering
        self._readers = {}  # type: typing.Dict[str, Reader]

    def enroll(self, spec: ReaderSpec) -> Reader:
        """
        登记一个读者。

        :param spec: 读者范围与排序条件
        :type spec: :class:`~ulid.ledger.model.ReaderSpec`
        :return: 读者进度对象
        :rtype: :class:`~ulid.ledger.reader.Reader`
        """
        if spec.reader_id in self._readers:
            raise ValueError('Reader {} already enrolled'.format(spec.reader_id))
        reader = Reader(spec.reader_id, spec)
        self._readers[reader.reader_id] = reader
        return reader

    def get(self, reader_id: str) -> Reader:
        """
        取读者进度。

        :raises KeyError: 读者未登记
        """
        return self._readers[reader_id]

    def reader_ids(self) -> typing.Tuple[str, ...]:
        """
        全部已登记读者编号, 按登记顺序返回。

        :rtype: :class:`~typing.Tuple`
        """
        return tuple(self._readers)

    def adopt(self, reader: Reader) -> Reader:
        """
        把已有读者进度(例如重开后)挂回本服务。

        :param reader: 读者进度
        :type reader: :class:`~ulid.ledger.reader.Reader`
        :return: 挂回的读者进度
        :rtype: :class:`~ulid.ledger.reader.Reader`
        """
        self._readers[reader.reader_id] = reader
        return reader

    def ordered_scope(self, reader: Reader) -> typing.List[str]:
        """
        按读者排序条件(并满足全局偏序)排好的完整范围。

        :return: 原件编号列表
        :rtype: :class:`~typing.List`
        """
        return self.ordering.sort(reader.spec.scope, key=reader.spec.order_key)

    def next_page(self, reader: Reader, page_size: int) -> typing.Optional[Page]:
        """
        翻下一页: 只包含读者还没见过的原件, 每件在该读者处最多出现一次。

        :param reader: 读者进度
        :type reader: :class:`~ulid.ledger.reader.Reader`
        :param page_size: 每页条目数
        :type page_size: :class:`~int`
        :return: 新页面; 范围已读完时返回 ``None``
        :rtype: :class:`~ulid.ledger.model.Page` or :class:`~None`
        """
        if page_size <= 0:
            raise ValueError('Page size must be positive')
        pending = [
            item_id for item_id in self.ordered_scope(reader)
            if item_id not in reader.seen and self.ledger.contains(item_id)
        ]
        if not pending:
            return None
        page_items = pending[:page_size]
        entries = tuple((item_id, self.ledger.current_ulid(item_id)) for item_id in page_items)
        generation = max(self.ledger.get(item_id).generation for item_id in page_items)
        bound = entries[-1][1]
        page = Page(generation=generation, lower_bound=bound, entries=entries)
        for item_id in page_items:
            reader.seen.add(item_id)
            reader.previewed.add(item_id)
        reader.bound = bound
        reader.bound_item = page_items[-1]
        return page

    def sign(self, reader: Reader, page: Page) -> None:
        """
        读者签下一页, 页内原件由预读转为已签。

        :param reader: 读者进度
        :type reader: :class:`~ulid.ledger.reader.Reader`
        :param page: 翻过的页面
        :type page: :class:`~ulid.ledger.model.Page`
        """
        reader.signed_pages.append((page.generation, page.item_ids))
        for item_id in page.item_ids:
            reader.previewed.discard(item_id)

    def deliver(self, reader: Reader, item_id: str) -> bool:
        """
        把实物交付给读者; 同一物理原件不会重复交付。

        :param reader: 读者进度
        :type reader: :class:`~ulid.ledger.reader.Reader`
        :param item_id: 原件编号
        :type item_id: :class:`~str`
        :return: 本次真正发生交付返回 ``True``, 已交付过返回 ``False``
        :rtype: :class:`~bool`
        """
        if item_id not in reader.spec.scope:
            raise ValueError('Item {} outside scope of reader {}'.format(item_id, reader.reader_id))
        if item_id in reader.delivered:
            return False
        reader.delivered.add(item_id)
        reader.previewed.discard(item_id)
        self.ledger.deliver(item_id)
        return True

    def remaining(self, reader: Reader) -> typing.List[str]:
        """
        读者范围里还没有真正交付的原件, 按其排序条件排列, 每件只出现一次。

        :rtype: :class:`~typing.List`
        """
        return [
            item_id for item_id in self.ordered_scope(reader)
            if item_id not in reader.delivered and self.ledger.contains(item_id)
        ]

    def reopen_pages(self, reader: Reader, page_size: int) -> typing.List[Page]:
        """
        重开验收: 照实际已送原件接续, 把未送达原件逐页列出, 页页可追到原实物号。

        :param reader: 读者进度
        :type reader: :class:`~ulid.ledger.reader.Reader`
        :param page_size: 每页条目数
        :type page_size: :class:`~int`
        :return: 覆盖全部未送达原件的页面列表
        :rtype: :class:`~typing.List`
        """
        pages = []  # type: typing.List[Page]
        remaining = self.remaining(reader)
        for start in range(0, len(remaining), page_size):
            page_items = remaining[start:start + page_size]
            entries = tuple((item_id, self.ledger.current_ulid(item_id)) for item_id in page_items)
            generation = max(self.ledger.get(item_id).generation for item_id in page_items)
            pages.append(Page(generation=generation, lower_bound=entries[-1][1], entries=entries))
        return pages

    def library_view(self, scope: typing.Iterable[str]) -> typing.List[str]:
        """
        新读者视图: 按原库编码和已定序查看原件。

        :param scope: 允许查看的原件编号
        :type scope: :class:`~typing.Iterable`
        :return: 按原库编码、再按全局偏序排好的原件编号
        :rtype: :class:`~typing.List`
        """
        def library_key(item_id: str) -> typing.Tuple[str, str]:
            return self.ledger.get(item_id).library_code, item_id

        return self.ordering.sort(scope, key=library_key)
