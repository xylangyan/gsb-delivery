"""
    ulid.ledger.service
    ~~~~~~~~~~~~~~~~~~~

    发布、导出与重开门面。

    新的对照目录与原进度映射必须一起发布; 重开后只走已成功签发的一代,
    两位客户端也可先后完成各自的旧续读。
"""
import copy
import dataclasses
import typing

from .. import ulid
from .model import ExportBook, ItemState, Publication
from .numbering import NumberingService
from .planner import RenumberPlan
from .reader import Reader, ReaderService
from .registry import Ledger


class LedgerService:
    """
    把目录、号池、读者与发布流程串起来的服务。

    :param ledger: 对照目录
    :type ledger: :class:`~ulid.ledger.registry.Ledger`
    :param numbering: 号池
    :type numbering: :class:`~ulid.ledger.numbering.NumberingService`
    :param reader_service: 读者服务, 缺省自动创建
    :type reader_service: :class:`~ulid.ledger.reader.ReaderService` or :class:`~None`
    """

    def __init__(self,
                 ledger: typing.Optional[Ledger] = None,
                 numbering: typing.Optional[NumberingService] = None,
                 reader_service: typing.Optional[ReaderService] = None) -> None:
        self.ledger = ledger if ledger is not None else Ledger()
        self.numbering = numbering if numbering is not None else NumberingService()
        self.reader_service = reader_service if reader_service is not None else ReaderService(self.ledger)
        self.publications = {}  # type: typing.Dict[int, Publication]
        self.plans = {}  # type: typing.Dict[int, RenumberPlan]

    def publish(self, plan: RenumberPlan, round_id: str) -> Publication:
        """
        把规划签发给新代, 目录与原进度映射一起发布。

        冻结原件(已确认/已交付)保持原号; 未冻结原件按规划换号; 后补件登记进目录。

        :param plan: 改签规划
        :type plan: :class:`~ulid.ledger.planner.RenumberPlan`
        :param round_id: 号池轮次
        :type round_id: :class:`~str`
        :return: 本次发布
        :rtype: :class:`~ulid.ledger.model.Publication`
        """
        generation = plan.generation
        issued = self.numbering.issue(generation, round_id) if self.numbering.has_round(round_id) else {}
        for item_id, value in plan.new_items.items():
            if item_id in self.ledger.ids():
                continue
            self.ledger.register(
                item_id,
                issued.get(item_id, value),
                state=ItemState.DRAFT,
                library_code=plan.library_codes.get(item_id) or item_id,
                generation=generation)
        for item_id, value in plan.renames.items():
            new_value = issued.get(item_id, value)
            self.ledger.reassign(item_id, new_value, generation)

        catalog = self.ledger.catalog()
        progress_map = self._progress_map(plan)
        alias_map = self._alias_map(plan)
        publication = Publication(
            generation=generation,
            catalog=catalog,
            progress_map=progress_map,
            alias_map=alias_map)
        self.publications[generation] = publication
        self.plans[generation] = plan
        return publication

    def _progress_map(self, plan: RenumberPlan):
        progress = {}
        for reader_id, reticket in plan.retickets.items():
            progress[(reader_id, 'bound')] = reticket.new_bound
            progress[(reader_id, 'owed_segments')] = reticket.owed_segments
        for audit in plan.audits:
            progress[(audit.reader_id, 'audit')] = audit
        return progress

    def _alias_map(self, plan: RenumberPlan):
        alias_map = {}
        for item_id, new_value in plan.changed_items.items():
            if self.ledger.contains(item_id):
                item = self.ledger.get(item_id)
                if item.original_ulid != new_value:
                    alias_map[item.original_ulid] = new_value
                for _generation, old_value, _new_value in self.ledger.alias_history(item_id):
                    if old_value != new_value:
                        alias_map[old_value] = new_value
        for reticket in plan.retickets.values():
            for _item_id, old_value, new_value in reticket.aliases:
                if old_value != new_value:
                    alias_map[old_value] = new_value
        return alias_map

    def export_book(self) -> ExportBook:
        """
        导出簿: 原件号、真正交付与只有预读的区段、每个读者原来的范围和排序条件。

        :rtype: :class:`~ulid.ledger.model.ExportBook`
        """
        delivered = {}
        previewed = {}
        specs = {}
        for reader_id in self.reader_service.reader_ids():
            reader = self.reader_service.get(reader_id)
            delivered[reader_id] = frozenset(reader.delivered)
            previewed[reader_id] = frozenset(reader.previewed)
            specs[reader_id] = reader.spec
        return ExportBook(
            items=self.ledger.snapshot(),
            delivered=delivered,
            previewed=previewed,
            reader_specs=specs)

    def reopen(self) -> 'LedgerService':
        """
        重开: 只承认已成功签发的代, 丢弃未签发保留, 读者进度原样保留供先后续读。

        :return: 新的服务实例
        :rtype: :class:`~ulid.ledger.service.LedgerService`
        """
        new_numbering = self.numbering.reopen()
        new_ledger = Ledger(self.ledger.ordering)
        for item in self.ledger.items():
            new_ledger.register(
                item.item_id,
                item.current_ulid,
                state=item.state,
                library_code=item.library_code,
                generation=item.generation)
        new_reader_service = ReaderService(new_ledger, self.reader_service.ordering)
        for reader_id in self.reader_service.reader_ids():
            old_reader = self.reader_service.get(reader_id)
            new_reader = Reader(
                reader_id=old_reader.reader_id,
                spec=old_reader.spec,
                bound=old_reader.bound,
                bound_item=old_reader.bound_item,
                signed_pages=list(old_reader.signed_pages),
                delivered=set(old_reader.delivered),
                previewed=set(old_reader.previewed),
                seen=set(old_reader.seen))
            new_reader_service.adopt(new_reader)
        service = LedgerService(new_ledger, new_numbering, new_reader_service)
        service.publications = copy.deepcopy(self.publications)
        service.plans = dict(self.plans)
        return service

    def continue_resume(self, reader: Reader, page_size: int):
        """
        重开之后让一个客户端完成自己的旧续读, 逐页返回它实际还欠的原件。

        :param reader: 重开保留下来的读者进度
        :type reader: :class:`~ulid.ledger.reader.Reader`
        :param page_size: 每页条目数
        :type page_size: :class:`~int`
        :return: 页面列表, 每件未送达原件只出现一次并带原实物号
        :rtype: :class:`~typing.List`
        """
        return self.reader_service.reopen_pages(reader, page_size)
