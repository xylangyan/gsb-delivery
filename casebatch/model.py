from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .wire import PreparedMessage, normalize_append_bytes


def parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif value is None:
        raise ValueError("internal date is required")
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.utcoffset() is None:
        raise ValueError(f"internal date must include an explicit timezone: {value!r}")
    return dt


def _as_bytes(value: Any) -> Optional[bytes]:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError(f"unsupported body value: {type(value)!r}")


@dataclass
class Message:
    source: str = ""
    case_id: Any = None
    flags: Tuple[str, ...] = ()
    date: Optional[datetime] = None
    body: Optional[bytes] = None
    raw_length: Optional[int] = None
    normalized_length: Optional[int] = None
    lone_lf: int = 0
    lone_cr: int = 0
    digest: Optional[str] = None
    attribution: Optional[str] = None
    receipt: Dict[str, Any] = field(default_factory=dict)
    sequence: int = 0

    @classmethod
    def from_dict(cls, data: Any, index: int = 0) -> "Message":
        if not isinstance(data, Mapping):
            data = {"body": data}
        body = None
        for key in ("body_b64", "body_base64", "b64"):
            if key in data and data[key] is not None:
                body = base64.b64decode(data[key])
                break
        if body is None and "body_hex" in data and data["body_hex"] is not None:
            body = bytes.fromhex(str(data["body_hex"]))
        if body is None:
            for key in ("body", "msg", "raw", "content", "text", "正文", "原件", "原字节"):
                if key in data and data[key] is not None:
                    body = _as_bytes(data[key])
                    break

        flags_value = data.get(
            "flags", data.get("flag", data.get("旗标", data.get("flags_list")))
        )
        if flags_value is None:
            flags: Tuple[str, ...] = ()
        elif isinstance(flags_value, (str, bytes)):
            flags = (flags_value.decode() if isinstance(flags_value, bytes) else flags_value,)
        else:
            flags = tuple(flags_value)

        date_value = data.get(
            "date",
            data.get(
                "internaldate",
                data.get("internal_date", data.get("msg_time", data.get("内部日期"))),
            ),
        )
        date = parse_datetime(date_value) if date_value is not None else None

        source = str(
            data.get(
                "source",
                data.get(
                    "id",
                    data.get(
                        "message_id",
                        data.get(
                            "name",
                            data.get("来源", data.get("source_ref", f"msg-{index + 1}")),
                        ),
                    ),
                ),
            )
        )
        return cls(
            source=source,
            case_id=data.get("case_id", data.get("case", data.get("dossier", data.get("案号")))),
            flags=flags,
            date=date,
            body=body,
            raw_length=_optional_int(data.get("raw_length", data.get("original_length"))),
            normalized_length=_optional_int(
                data.get(
                    "normalized_length",
                    data.get("length", data.get("size", data.get("长度"))),
                )
            ),
            lone_lf=int(data.get("lone_lf", data.get("lf_only", 0)) or 0),
            lone_cr=int(data.get("lone_cr", data.get("cr_only", 0)) or 0),
            digest=data.get(
                "digest",
                data.get(
                    "sha256",
                    data.get(
                        "summary_digest",
                        data.get("摘要", data.get("正文摘要", data.get("digest_summary"))),
                    ),
                ),
            ),
            attribution=data.get(
                "attribution",
                data.get(
                    "owner",
                    data.get("origin", data.get("归属", data.get("原信归属"))),
                ),
            ),
            receipt=dict(data.get("receipt", {})),
            sequence=int(data.get("sequence", data.get("seq", index))),
        )

    def normalized_body(self) -> Optional[bytes]:
        if self.body is None:
            return None
        return normalize_append_bytes(self.body)

    def effective_normalized_length(self) -> int:
        normalized = self.normalized_body()
        if normalized is not None:
            return len(normalized)
        if self.normalized_length is not None:
            return self.normalized_length
        if self.raw_length is not None:
            return self.raw_length + self.lone_lf + self.lone_cr
        raise ValueError(f"message {self.source!r} has no body or length")

    def effective_raw_length(self) -> Optional[int]:
        if self.raw_length is not None:
            return self.raw_length
        if self.body is not None:
            return len(self.body)
        return None

    def sha256(self) -> Optional[str]:
        normalized = self.normalized_body()
        if normalized is None:
            return None
        return hashlib.sha256(normalized).hexdigest()

    def date_token(self) -> Optional[bytes]:
        if self.date is None:
            return None
        if self.date.utcoffset() is None:
            raise ValueError(f"message {self.source!r} date lacks an explicit timezone")
        from imapclient.datetime_util import datetime_to_INTERNALDATE

        return ('"' + datetime_to_INTERNALDATE(self.date) + '"').encode("ascii")

    def prepared(self, case_id: Any = None) -> PreparedMessage:
        normalized = self.normalized_body()
        digest = self.digest
        if digest is None and normalized is not None:
            digest = "sha256:" + hashlib.sha256(normalized).hexdigest()
        return PreparedMessage(
            length=self.effective_normalized_length(),
            flags=flags_token_bytes(self.flags),
            date=self.date_token(),
            source=self.source,
            case_id=self.case_id if self.case_id is not None else case_id,
            raw_length=self.effective_raw_length(),
            normalized_sha256=digest,
            data=normalized if normalized is not None else b"",
        )


def flags_token_bytes(flags: Sequence[Any]) -> bytes:
    from .wire import flags_token

    return flags_token(flags)


@dataclass
class Case:
    case_id: Any
    folder: str = "INBOX"
    category: str = "default"
    messages: List[Message] = field(default_factory=list)
    predecessors: Set[Any] = field(default_factory=set)
    order_index: int = 0
    attributes: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], index: int = 0) -> "Case":
        case_id = data.get("id", data.get("case_id", data.get("case", data.get("案号", index + 1))))
        raw_messages = data.get(
            "messages",
            data.get("mails", data.get("emails", data.get("邮件", data.get("信件", ())))),
        )
        messages = [
            Message.from_dict({**m, "case_id": case_id} if isinstance(m, Mapping) else m, i)
            for i, m in enumerate(raw_messages)
        ]
        predecessors = set(
            data.get(
                "predecessors",
                data.get(
                    "after",
                    data.get(
                        "requires",
                        data.get("前置案号", data.get("registered_order", ())),
                    ),
                ),
            )
        )
        if "predecessor" in data:
            predecessors.add(data["predecessor"])
        return cls(
            case_id=case_id,
            folder=str(
                data.get(
                    "folder",
                    data.get("target_folder", data.get("目标文件夹", "INBOX")),
                )
            ),
            category=str(
                data.get(
                    "category",
                    data.get("kind", data.get("案号类别", data.get("类别", "default"))),
                )
            ),
            messages=messages,
            predecessors=predecessors,
            order_index=int(data.get("order_index", data.get("index", index))),
            attributes=dict(data.get("attributes", {})),
        )

    def prepared_messages(self) -> List[PreparedMessage]:
        return [message.prepared(self.case_id) for message in self.messages]


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(value)


@dataclass(frozen=True)
class BudgetTier:
    index: int
    burst_limit: Optional[int]
    command_limit: Optional[int]
    name: str = ""

    @classmethod
    def from_value(cls, value: Any, index: int) -> "BudgetTier":
        name = f"T{index}"
        if isinstance(value, int):
            return cls(index, value, value, name)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, Mapping)):
            burst = int(value[0]) if len(value) > 0 and value[0] is not None else None
            command = int(value[1]) if len(value) > 1 and value[1] is not None else burst
            return cls(index, burst, command, name)
        if isinstance(value, Mapping):
            burst = value.get(
                "burst_limit",
                value.get(
                    "max_between_responses",
                    value.get(
                        "between_responses",
                        value.get(
                            "burst",
                            value.get("两次回包上限", value.get("续报预算")),
                        ),
                    ),
                ),
            )
            command = value.get(
                "command_limit",
                value.get(
                    "max_command_bytes",
                    value.get(
                        "command_bytes",
                        value.get(
                            "total_limit",
                            value.get(
                                "total",
                                value.get("整令字节上限", value.get("整个命令预算")),
                            ),
                        ),
                    ),
                ),
            )
            return cls(
                index=index,
                burst_limit=_optional_int(burst),
                command_limit=_optional_int(command),
                name=str(value.get("name", value.get("label", name))),
            )
        raise TypeError(f"unsupported budget tier: {value!r}")


@dataclass
class Fixture:
    name: str
    literal_plus: bool = False
    tiers: List[BudgetTier] = field(default_factory=list)
    initial_tier: int = 0
    transitions: Dict[str, Any] = field(default_factory=dict)
    byte_edges: Optional[List[int]] = None
    label_width: int = 8

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], index: int = 0) -> "Fixture":
        name = str(
            data.get(
                "name",
                data.get("label", data.get("id", data.get("名称", f"F{index + 1}"))),
            )
        )
        raw_tiers = data.get(
            "tiers",
            data.get("budgets", data.get("budget_tiers", data.get("预算档", data.get("档位")))),
        )
        if raw_tiers is None:
            burst = data.get("burst_limit", data.get("max_between_responses"))
            command = data.get("command_limit", data.get("max_command_bytes"))
            if burst is None and command is None:
                raise ValueError(f"fixture {name!r} has no budget tiers")
            raw_tiers = [{"burst_limit": burst, "command_limit": command}]
        tiers = [BudgetTier.from_value(v, i) for i, v in enumerate(raw_tiers)]
        initial = int(data.get("initial_tier", data.get("start_tier", data.get("tier", 0))))
        edges = data.get("byte_edges", data.get("size_edges", data.get("byte_tier_edges")))
        return cls(
            name=name,
            literal_plus=bool(
                data.get(
                    "literal_plus",
                    data.get("literal+", data.get("plus", data.get("use_literal_plus", False))),
                )
            ),
            tiers=tiers,
            initial_tier=initial,
            transitions=dict(data.get("transitions", data.get("next_tiers", data.get("transition", {})))),
            byte_edges=[int(x) for x in edges] if edges is not None else None,
            label_width=int(data.get("label_width", 8)),
        )

    def classify_byte_tier(self, total_bytes: int) -> int:
        if self.byte_edges is not None:
            for i, edge in enumerate(self.byte_edges):
                if total_bytes <= int(edge):
                    return i
            return len(self.byte_edges)
        command_limits = [tier.command_limit for tier in self.tiers if tier.command_limit is not None]
        for i, limit in enumerate(command_limits):
            if total_bytes <= limit:
                return i
        return len(command_limits)

    def next_tier_index(self, current_tier: int, category: str, total_bytes: int) -> Tuple[Optional[int], str]:
        byte_tier = self.classify_byte_tier(total_bytes)
        category_rules = self.transitions.get(category)
        rule_name = f"transitions[{category!r}][{byte_tier}]"
        if category_rules is None:
            category_rules = self.transitions.get("*")
            rule_name = f"transitions['*'][{byte_tier}]"
        if category_rules is None:
            return current_tier, "default:stay"

        if isinstance(category_rules, Mapping):
            str_key = str(byte_tier)
            if str_key in category_rules:
                next_value = category_rules[str_key]
            elif byte_tier in category_rules:
                next_value = category_rules[byte_tier]
            else:
                return current_tier, rule_name + ":missing-rule-stay"
        elif isinstance(category_rules, Sequence) and not isinstance(category_rules, (str, bytes)):
            if byte_tier >= len(category_rules):
                return None, rule_name + "=byte-tier-out-of-range"
            next_value = category_rules[byte_tier]
        else:
            next_value = category_rules

        if next_value is None:
            return None, rule_name + "=infeasible"
        if isinstance(next_value, str) and next_value in {"stop", "infeasible", "none"}:
            return None, rule_name + "=infeasible"
        next_index = int(next_value)
        if next_index < 0 or next_index >= len(self.tiers):
            return None, rule_name + f"=invalid-tier-{next_index}"
        return next_index, rule_name


@dataclass
class TaskSpec:
    cases: List[Case]
    fixtures: List[Fixture]
    tag: bytes = b"tag"
    order: Optional[List[Any]] = None
    allow_reorder: bool = True
    max_group_cases: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskSpec":
        raw_cases = data.get(
            "cases",
            data.get(
                "dossiers",
                data.get(
                    "案卷",
                    data.get(
                        "case_files",
                        data.get("案卷列表", data.get("case_numbers", ())),
                    ),
                ),
            ),
        )
        cases = [Case.from_dict(c, i) for i, c in enumerate(raw_cases)]
        by_id = {case.case_id: case for case in cases}

        raw_precedence = data.get("precedence", data.get("order_constraints", data.get("before", ())))
        for item in raw_precedence:
            if isinstance(item, Mapping):
                before = item.get("before", item.get("earlier", item.get("predecessor")))
                after = item.get("after", item.get("successor"))
                if after in by_id and before is not None:
                    by_id[after].predecessors.add(before)
            elif isinstance(item, Sequence) and len(item) >= 2:
                before, after = item[0], item[1]
                if after in by_id:
                    by_id[after].predecessors.add(before)

        raw_fixtures = data.get(
            "fixtures",
            data.get(
                "relays",
                data.get(
                    "clamps",
                    data.get("夹具", data.get("中转机", data.get("relay_fixtures"))),
                ),
            ),
        )
        if raw_fixtures is None:
            fixture_a = data.get("fixture_a", data.get("fixture1"))
            fixture_b = data.get("fixture_b", data.get("fixture2"))
            raw_fixtures = [x for x in (fixture_a, fixture_b) if x is not None]
        fixtures = [Fixture.from_dict(f, i) for i, f in enumerate(raw_fixtures)]
        if len(fixtures) != 2:
            # The problem always has two relays, but generic library use may not.
            pass

        tag = data.get("tag", data.get("command_tag", data.get("tag_name", "tag")))
        if isinstance(tag, str):
            tag_bytes = tag.encode("ascii")
        else:
            tag_bytes = bytes(tag)

        order = data.get("order", data.get("loading_order", data.get("装批顺序")))
        return cls(
            cases=cases,
            fixtures=fixtures,
            tag=tag_bytes,
            order=list(order) if order is not None else None,
            allow_reorder=bool(data.get("allow_reorder", data.get("reorder", True))),
            max_group_cases=_optional_int(data.get("max_group_cases")),
            raw=dict(data),
        )

    def case_map(self) -> Dict[Any, Case]:
        return {case.case_id: case for case in self.cases}
