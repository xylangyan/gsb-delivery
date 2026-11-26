from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .model import Case, Fixture, TaskSpec
from .planner import GroupEvaluation, plan_linear, plan_task, updated_origins


class CaseSequence:
    """Virtual case stream backed by repeated compact booklet segments."""

    def __init__(self, segments: Sequence[MappingLike]):
        self.segment_dicts: List[Dict[str, Any]] = [dict(segment) for segment in segments]
        self.case_templates: List[List[Dict[str, Any]]] = []
        self.repeats: List[int] = []
        self.offsets: List[int] = [0]
        self.id_starts: List[int] = []
        self.id_steps: List[int] = []
        self.id_block_steps: List[int] = []

        for segment in self.segment_dicts:
            templates = [dict(case) for case in segment.get("cases", segment.get("dossiers", ()))]
            repeat = int(segment.get("repeat", segment.get("repetitions", 1)))
            id_step = int(segment.get("id_step", segment.get("case_id_step", 1)))
            id_block_step = int(
                segment.get("id_block_step", segment.get("repeat_id_step", len(templates) * id_step))
            )
            self.case_templates.append(templates)
            self.repeats.append(repeat)
            self.id_starts.append(int(segment.get("id_start", segment.get("start_id", 0))))
            self.id_steps.append(id_step)
            self.id_block_steps.append(id_block_step)
            self.offsets.append(self.offsets[-1] + repeat * len(templates))

        self.total = self.offsets[-1]

    @classmethod
    def from_task_data(cls, data: Dict[str, Any]) -> Optional["CaseSequence"]:
        segments = data.get("segments", data.get("booklets", data.get("小段案卷簿")))
        if not segments:
            return None
        return cls(segments)

    def _locate_segment(self, index: int) -> Tuple[int, int, int]:
        if index < 0 or index >= self.total:
            raise IndexError(f"case index {index} outside 0..{self.total - 1}")
        for segment_index in range(len(self.segment_dicts)):
            start = self.offsets[segment_index]
            end = self.offsets[segment_index + 1]
            if index < end:
                local = index - start
                block, case_index = divmod(local, len(self.case_templates[segment_index]))
                return segment_index, block, case_index
        raise IndexError(index)

    def case_id_at(self, index: int) -> Any:
        segment_index, block, case_index = self._locate_segment(index)
        return (
            self.id_starts[segment_index]
            + block * self.id_block_steps[segment_index]
            + case_index * self.id_steps[segment_index]
        )

    def case_at(self, index: int) -> Case:
        segment_index, block, case_index = self._locate_segment(index)
        raw = copy.deepcopy(self.case_templates[segment_index][case_index])
        case_id = self.case_id_at(index)
        raw["id"] = case_id

        message_dicts = list(raw.get("messages", raw.get("mails", ())))
        resolved_messages = []
        message_ordinal = index * max(1, len(message_dicts))
        for message_index, message in enumerate(message_dicts):
            item = dict(message)
            ordinal = message_ordinal + message_index
            item["case_id"] = case_id

            length_cycle = item.pop("length_cycle", None)
            if length_cycle is not None:
                item["length"] = length_cycle[ordinal % len(length_cycle)]

            flags_cycle = item.pop("flags_cycle", None)
            if flags_cycle is not None:
                item["flags"] = flags_cycle[ordinal % len(flags_cycle)]

            date_cycle = item.pop("date_cycle", None)
            if date_cycle is not None:
                item["date"] = date_cycle[ordinal % len(date_cycle)]
            elif "date_base" in item:
                base = datetime.fromisoformat(str(item.pop("date_base")).replace("Z", "+00:00"))
                step = float(item.pop("date_step_seconds", 0))
                item["date"] = (base + timedelta(seconds=step * ordinal)).isoformat()

            resolved_messages.append(item)

        raw["messages"] = resolved_messages
        return Case.from_dict(raw, index)

    def index_of_case_id(self, case_id: Any) -> int:
        if isinstance(case_id, str) and case_id.isdigit():
            case_id = int(case_id)
        for segment_index, templates in enumerate(self.case_templates):
            for case_index in range(len(templates)):
                delta = int(case_id) - self.id_starts[segment_index] - case_index * self.id_steps[segment_index]
                block_step = self.id_block_steps[segment_index]
                if block_step == 0:
                    if delta == 0:
                        block = 0
                    else:
                        continue
                elif delta % block_step != 0:
                    continue
                else:
                    block = delta // block_step
                if 0 <= block < self.repeats[segment_index]:
                    return self.offsets[segment_index] + block * len(templates) + case_index
        raise KeyError(f"case id {case_id!r} not found in compressed segments")


MappingLike = Dict[str, Any]


@dataclass
class CompiledManifest:
    sequence: CaseSequence
    fixtures: Sequence[Fixture]
    tag: bytes
    prefix: List[GroupEvaluation]
    cycle: List[GroupEvaluation]
    cycle_repetitions: int
    tail: List[GroupEvaluation]
    prefix_case_count: int
    cycle_case_count: int
    tail_case_count: int
    prefix_command_count: int
    cycle_command_count: int
    total_commands: int
    cycle_start_boundary: int = 0
    cycle_state: Tuple[int, ...] = ()

    def locate_case(self, case_id: Any) -> Dict[str, Any]:
        global_index = self.sequence.index_of_case_id(case_id)

        if global_index < self.prefix_case_count:
            return self._locate_flat(
                case_id,
                global_index,
                self.prefix,
                global_start=0,
                command_index_base=0,
                repetition=0,
                layer="prefix",
            )

        repeated_cases = self.cycle_repetitions * self.cycle_case_count
        cycle_end = self.prefix_case_count + repeated_cases
        if global_index < cycle_end:
            relative = global_index - self.prefix_case_count
            repetition, phase = divmod(relative, self.cycle_case_count)
            for command_offset, evaluation in enumerate(self.cycle):
                length = len(evaluation.case_ids)
                start = sum(len(item.case_ids) for item in self.cycle[:command_offset])
                if start <= phase < start + length:
                    command_index = (
                        self.prefix_command_count
                        + repetition * self.cycle_command_count
                        + command_offset
                        + 1
                    )
                    global_start = (
                        self.prefix_case_count
                        + repetition * self.cycle_case_count
                        + start
                    )
                    info = self._command_info(
                        evaluation,
                        global_start,
                        command_index,
                        layer="cycle",
                        repetition=repetition,
                        queried_case_id=case_id,
                        queried_global_index=global_index,
                    )
                    info["phase_in_cycle"] = phase
                    return info
            raise RuntimeError("cycle locator failed")

        tail_start = cycle_end
        return self._locate_flat(
            case_id,
            global_index,
            self.tail,
            global_start=tail_start,
            command_index_base=self.prefix_command_count
            + self.cycle_repetitions * self.cycle_command_count,
            repetition=None,
            layer="tail",
        )

    def _locate_flat(
        self,
        case_id: Any,
        global_index: int,
        commands: Sequence[GroupEvaluation],
        *,
        global_start: int,
        command_index_base: int,
        repetition: Optional[int],
        layer: str,
    ) -> Dict[str, Any]:
        phase = global_index - global_start
        cursor = 0
        for offset, evaluation in enumerate(commands):
            length = len(evaluation.case_ids)
            if cursor <= phase < cursor + length:
                return self._command_info(
                    evaluation,
                    global_start + cursor,
                    command_index_base + offset + 1,
                    layer=layer,
                    repetition=repetition,
                    queried_case_id=case_id,
                    queried_global_index=global_index,
                )
            cursor += length
        raise RuntimeError("flat locator failed")

    def _command_info(
        self,
        evaluation: GroupEvaluation,
        global_start: int,
        command_index: int,
        *,
        layer: str,
        repetition: Optional[int],
        queried_case_id: Any = None,
        queried_global_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        case_ids = [
            self.sequence.case_id_at(global_start + offset)
            for offset in range(len(evaluation.case_ids))
        ]
        budgets = [budget.to_dict() for budget in evaluation.budgets]
        if layer == "cycle" and repetition is not None:
            for budget in budgets:
                budget["cycle_repetition"] = repetition
                provenance = budget.setdefault("provenance", {})
                provenance["recurring_cycle"] = True
                if repetition > 0:
                    provenance["recurring_tier_state"] = list(evaluation.next_tiers)
        return {
            "case_id": queried_case_id,
            "global_case_index": queried_global_index,
            "command_index": command_index,
            "layer": layer,
            "cycle_repetition": repetition,
            "command_case_ids": case_ids,
            "folder": evaluation.folder,
            "cut_id": evaluation.cut_id,
            "bytes_by_fixture": {
                name: report.total_bytes for name, report in evaluation.reports.items()
            },
            "starting_budget": budgets,
            "message_sources": evaluation.message_sources,
        }

    def to_segment_sheet(self) -> Dict[str, Any]:
        return {
            "status": "feasible",
            "compressed": True,
            "total_cases": self.sequence.total,
            "command_count": self.total_commands,
            "segments": [
                {
                    "layer": "prefix",
                    "repeat": 1,
                    "case_count": self.prefix_case_count,
                    "command_count": self.prefix_command_count,
                    "commands": [
                        command.to_dict(include_length_fields=False) for command in self.prefix
                    ],
                },
                {
                    "layer": "cycle",
                    "repeat": self.cycle_repetitions,
                    "case_count_per_repeat": self.cycle_case_count,
                    "command_count_per_repeat": self.cycle_command_count,
                    "state_tiers": list(self.cycle_state),
                    "commands": [
                        command.to_dict(include_length_fields=False) for command in self.cycle
                    ],
                },
                {
                    "layer": "tail",
                    "repeat": 1,
                    "case_count": self.tail_case_count,
                    "command_count": len(self.tail),
                    "commands": [
                        command.to_dict(include_length_fields=False) for command in self.tail
                    ],
                },
            ],
        }

    def to_summary_dict(self) -> Dict[str, Any]:
        return {
            "status": "feasible",
            "compressed": True,
            "total_cases": self.sequence.total,
            "command_count": self.total_commands,
            "cycle": {
                "case_count": self.cycle_case_count,
                "command_count": self.cycle_command_count,
                "repetitions": self.cycle_repetitions,
                "state_tiers": list(self.cycle_state),
            },
            "prefix_commands": len(self.prefix),
            "tail_commands": len(self.tail),
            "segment_sheet": self.to_segment_sheet(),
            "locator": "call CompiledManifest.locate_case(case_id)",
        }


def _boundary_tiers(result_commands: Sequence[GroupEvaluation], fixture_count: int) -> List[Tuple[int, ...]]:
    boundaries: List[Tuple[int, ...]] = []
    for evaluation in result_commands:
        boundaries.append(tuple(int(budget.before_tier) for budget in evaluation.budgets[:fixture_count]))
    final = tuple(int(budget.after_tier) for budget in result_commands[-1].budgets[:fixture_count])
    boundaries.append(final)
    return boundaries


def plan_compressed(data: Dict[str, Any]) -> Dict[str, Any]:
    sequence = CaseSequence.from_task_data(data)
    if sequence is None:
        return plan_task(TaskSpec.from_dict(data))

    spec = TaskSpec.from_dict({**data, "cases": []})
    if not spec.fixtures:
        raise ValueError("at least one fixture is required")

    period = int(data.get("period_cases", sequence.case_templates[0].__len__()))
    state_count = 1
    for fixture in spec.fixtures:
        state_count *= len(fixture.tiers)
    lookahead = spec.max_group_cases or 0
    probe_cases = min(sequence.total, max(period, period * (state_count + 2) + lookahead))

    probe_cases_list = [sequence.case_at(i) for i in range(probe_cases)]
    result = plan_linear(
        probe_cases_list,
        spec.fixtures,
        tag=spec.tag,
        max_group_cases=spec.max_group_cases,
        sequence_ids=[sequence.case_id_at(i) for i in range(probe_cases)],
    )
    if result.status != "feasible":
        return result.to_dict()

    if sequence.total == probe_cases or sequence.total <= period * 2:
        full_cases = [sequence.case_at(i) for i in range(sequence.total)]
        full_result = plan_linear(
            full_cases,
            spec.fixtures,
            tag=spec.tag,
            max_group_cases=spec.max_group_cases,
            sequence_ids=[sequence.case_id_at(i) for i in range(sequence.total)],
        )
        payload = full_result.to_dict()
        payload["compressed"] = False
        return payload

    positions = [0]
    for evaluation in result.commands:
        positions.append(positions[-1] + len(evaluation.case_ids))
    boundaries = _boundary_tiers(result.commands, len(spec.fixtures))

    first_seen: Dict[Tuple[int, Tuple[int, ...]], int] = {}
    candidate_pairs: List[Tuple[int, int]] = []
    for boundary_index, tiers in enumerate(boundaries):
        key = (positions[boundary_index] % period, tiers)
        if key in first_seen:
            candidate_pairs.append((first_seen[key], boundary_index))
        else:
            first_seen[key] = boundary_index

    for cycle_start, cycle_end in candidate_pairs:
        prefix = result.commands[:cycle_start]
        cycle = result.commands[cycle_start:cycle_end]
        if not cycle:
            continue
        prefix_case_count = positions[cycle_start]
        cycle_case_count = positions[cycle_end] - positions[cycle_start]
        cycle_state = boundaries[cycle_start]
        cycle_origins = updated_origins(spec.fixtures, cycle[-1])
        remaining = sequence.total - prefix_case_count
        base_repetitions, remainder = divmod(remaining, cycle_case_count)

        for extra_cycles_removed in range(4):
            repetitions = base_repetitions - extra_cycles_removed
            tail_case_count = remainder + extra_cycles_removed * cycle_case_count
            if repetitions < 0 or tail_case_count < 0:
                continue
            tail_start = prefix_case_count + repetitions * cycle_case_count
            tail_cases = [sequence.case_at(tail_start + i) for i in range(tail_case_count)]
            tail_result = plan_linear(
                tail_cases,
                spec.fixtures,
                tag=spec.tag,
                max_group_cases=spec.max_group_cases,
                initial_tiers=cycle_state,
                initial_origins=cycle_origins,
                sequence_ids=[sequence.case_id_at(tail_start + i) for i in range(tail_case_count)],
            )
            if tail_result.status != "feasible":
                continue

            manifest = CompiledManifest(
                sequence=sequence,
                fixtures=spec.fixtures,
                tag=spec.tag,
                prefix=prefix,
                cycle=cycle,
                cycle_repetitions=repetitions,
                tail=tail_result.commands,
                prefix_case_count=prefix_case_count,
                cycle_case_count=cycle_case_count,
                tail_case_count=tail_case_count,
                prefix_command_count=len(prefix),
                cycle_command_count=len(cycle),
                total_commands=len(prefix)
                + repetitions * len(cycle)
                + len(tail_result.commands),
                cycle_start_boundary=cycle_start,
                cycle_state=cycle_state,
            )
            summary = manifest.to_summary_dict()
            summary["commands_preview"] = {
                "prefix": [e.to_dict(include_length_fields=False) for e in prefix],
                "cycle": [e.to_dict(include_length_fields=False) for e in cycle],
                "tail": [e.to_dict(include_length_fields=False) for e in tail_result.commands],
            }
            return {"manifest": manifest, "summary": summary}

    payload = result.to_dict()
    payload["warning"] = "no repeatable budget-state cycle found; returning probe plan"
    return payload
