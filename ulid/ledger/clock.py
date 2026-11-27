"""
    ulid.ledger.clock
    ~~~~~~~~~~~~~~~~~

    可控毫秒时钟, 用于重现旧钟回拨。

    时钟只负责给出当前毫秒时间; 回拨后新领的号其时间戳可能早于此前已签发的号,
    因此排号不能只依赖时钟单调, 全部先后关系交给 :mod:`~ulid.ledger.ordering`。
"""
import typing


class ControlledClock:
    """
    可手动推进、回拨的毫秒时钟。

    :param initial_millis: 初始 Unix 毫秒时间, 默认 0
    :type initial_millis: :class:`~int`
    """

    __slots__ = ['_millis']

    def __init__(self, initial_millis: typing.Optional[int] = None) -> None:
        self._millis = 0 if initial_millis is None else int(initial_millis)

    @property
    def millis(self) -> int:
        """
        当前时钟的 Unix 毫秒值。

        :return: 当前毫秒时间
        :rtype: :class:`~int`
        """
        return self._millis

    def milliseconds(self) -> int:
        """
        与 ulid 时间提供者一致的毫秒接口。

        :return: 当前毫秒时间
        :rtype: :class:`~int`
        """
        return self._millis

    def set(self, millis: int) -> int:
        """
        直接把时钟拨到给定毫秒, 允许早于当前值(旧钟回拨)。

        :param millis: 目标 Unix 毫秒时间
        :type millis: :class:`~int`
        :return: 设置后的毫秒时间
        :rtype: :class:`~int`
        """
        if millis < 0:
            raise ValueError('Clock millis must be non-negative')
        self._millis = int(millis)
        return self._millis

    def advance(self, millis: int = 1) -> int:
        """
        向前推进给定毫秒数; 传入负数即显式回拨。

        :param millis: 推进量, 可为负
        :type millis: :class:`~int`
        :return: 推进后的毫秒时间
        :rtype: :class:`~int`
        """
        return self.set(self._millis + int(millis))

    def rollback(self, millis: int) -> int:
        """
        旧钟回拨: 把时钟向回拨给定毫秒数。

        :param millis: 回拨的毫秒数, 必须为正
        :type millis: :class:`~int`
        :return: 回拨后的毫秒时间
        :rtype: :class:`~int`
        """
        if millis < 0:
            raise ValueError('Rollback distance must be non-negative')
        return self.set(self._millis - int(millis))
