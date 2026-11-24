"""Bundle loading and CoNLL-U deliverable writing.

Bundle JSON schema (one file, one list under ``sentences``)::

    {
      "sentences": [
        {
          "sent_id": "s1",
          "id_map": {"1": "1", "3": "2"},
          "hidden_roles": ["2", "4.1"],
          "license": {"2.1": ["2", "4.1"], "3.1": ["2"]},
          "placeholder_rows": {"2.1": {"form": "_", "misc": {}}},
          "basic_override": {"3": {"head": "1", "deprel": "obj"}},
          "queries": [
            {"name": "q1", "source": "1", "target": "3", "labels": ["nsubj"]}
          ],
          "conllu": "# sent_id = s1\n1\t...\n\n"
        }
      ]
    }

``conllu`` is optional.  Without it the run still produces the edge manifest;
with it the writer emits a pyconll-readable CoNLL-U deliverable.

Original relation numbering: edges are numbered from 1 in sentence token
order, taking each token's DEPS entries in their serialized order.  The
manifest reports those 1-based numbers for every adopted relation.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path

from pyconll.conllu import Sentence, Token, conllu

from .graph import Edge
from .model import License, Query, SentenceInput
from .solver import SentenceResult, solve_sentence


@dataclass
class LoadedSentence:
    data: SentenceInput
    original_text: str | None
    placeholder_templates: dict[str, dict]
    basic_override: dict[str, dict]
    edge_numbers: dict[int, int]


def load_bundle(path: str | Path) -> list[LoadedSentence]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    loaded: list[LoadedSentence] = []
    for item in raw["sentences"]:
        edges: list[Edge] = []
        edge_numbers: dict[int, int] = {}
        original_text = item.get("conllu")
        if original_text:
            sentence = conllu.load_from_string(original_text)[0]
            counter = 0
            for token in sentence.tokens:
                if token.is_multiword():
                    continue
                for head, relation in token.deps.items():
                    counter += 1
                    edge_numbers[len(edges)] = counter
                    edges.append(Edge(head, token.id, relation[0]))

        data = SentenceInput(
            sent_id=item["sent_id"],
            id_map=dict(item["id_map"]),
            edges=edges,
            hidden_roles=set(item.get("hidden_roles", ())),
            license=License({key: tuple(value) for key, value in item["license"].items()}),
            queries=[
                Query(
                    name=query["name"],
                    source=query["source"],
                    target=query["target"],
                    labels=tuple(query["labels"]),
                )
                for query in item.get("queries", ())
            ],
        )
        loaded.append(
            LoadedSentence(
                data=data,
                original_text=original_text,
                placeholder_templates=item.get("placeholder_rows", {}),
                basic_override=item.get("basic_override", {}),
                edge_numbers=edge_numbers,
            )
        )
    return loaded


def _deps_by_dst(edges) -> dict[str, dict[str, tuple[str, ...]]]:
    grouped: dict[str, dict[str, tuple[str, ...]]] = {}
    for edge in edges:
        grouped.setdefault(edge.dst, {})[edge.src] = (edge.label,)
    return grouped


def _empty_row(placeholder_id: str, template: dict | None, deps: dict) -> Token:
    template = template or {}
    return Token(
        id=placeholder_id,
        form=template.get("form", "_"),
        lemma=template.get("lemma", "_"),
        upos=template.get("upos", "_"),
        xpos=template.get("xpos", "_"),
        feats=dict(template.get("feats", {})),
        head=template.get("head"),
        deprel=template.get("deprel"),
        deps=deps,
        misc=dict(template.get("misc", {})),
    )


def build_deliverable(loaded: LoadedSentence, result: SentenceResult):
    """Return a serialized pyconll Sentence plus its manifest dict."""

    if not loaded.original_text:
        raise ValueError(f"{loaded.data.sent_id}: bundle has no original conllu text")

    original = conllu.load_from_string(loaded.original_text)[0]
    deps_by_dst = _deps_by_dst(result.output_edges)
    id_map = loaded.data.id_map

    output_tokens: list[Token] = []
    placed_placeholders: set[str] = set()
    for token in original.tokens:
        if token.is_multiword():
            if all(part in id_map for part in _mwt_span(token.id)):
                start, end = _mwt_span(token.id)
                new_span = f"{id_map[start]}-{id_map[end]}"
                output_tokens.append(_clone_mwt(token, new_span))
            continue
        if token.id not in id_map:
            continue
        new_id = id_map[token.id]
        override = loaded.basic_override.get(token.id, {})
        new_head = override.get("head")
        if new_head is None:
            if token.head is not None and token.head in id_map:
                new_head = id_map[token.head]
            else:
                new_head = token.head
        output_tokens.append(
            Token(
                id=new_id,
                form=token.form,
                lemma=token.lemma,
                upos=token.upos,
                xpos=token.xpos,
                feats=dict(token.feats),
                head=new_head,
                deprel=override.get("deprel", token.deprel),
                deps=dict(deps_by_dst.get(new_id, {})),
                misc=dict(token.misc),
            )
        )
        for placeholder_id, roles in result.placeholder_rows.items():
            base = placeholder_id.split(".")[0]
            if base == new_id.split(".")[0]:
                output_tokens.append(
                    _empty_row(
                        placeholder_id,
                        loaded.placeholder_templates.get(placeholder_id),
                        dict(deps_by_dst.get(placeholder_id, {})),
                    )
                )
                placed_placeholders.add(placeholder_id)

    missing = sorted(set(result.placeholder_rows) - placed_placeholders)
    if missing:
        raise ValueError(
            f"{result.sent_id}: placeholder rows {missing} have no delivered token "
            "with the matching base id"
        )

    valid_ids = {token.id for token in output_tokens}
    dangling = [
        token.id
        for token in output_tokens
        if not token.is_empty_node()
        and token.head is not None
        and token.head != "0"
        and token.head not in valid_ids
    ]
    if dangling:
        raise ValueError(
            f"{result.sent_id}: basic HEAD of tokens {dangling} points at an id that is "
            "not delivered; supply a basic_override for those tokens in the bundle"
        )

    delivered = Sentence()
    delivered.meta = dict(original.meta)
    delivered.meta["sent_id"] = result.sent_id
    delivered.meta["adopted_original_relation_numbers"] = ",".join(
        str(loaded.edge_numbers[index]) for index in result.kept_edge_ids
    )
    provenance = "; ".join(
        f"{placeholder}<=['" + "','".join(roles) + "']"
        for placeholder, roles in result.placeholder_rows.items()
    )
    if provenance:
        delivered.meta["placeholder_provenance"] = provenance
    delivered.tokens = output_tokens

    buffer = io.StringIO()
    conllu.write_sentence(delivered, buffer)
    manifest = _manifest(loaded, result)
    return buffer.getvalue(), manifest


def _mwt_span(mwt_id: str) -> tuple[str, str]:
    start, end = mwt_id.split("-")
    return start, end


def _clone_mwt(token: Token, new_id: str) -> Token:
    return Token(
        id=new_id,
        form=token.form,
        lemma=token.lemma,
        upos=token.upos,
        xpos=token.xpos,
        feats=dict(token.feats),
        head=None,
        deprel=None,
        deps={},
        misc=dict(token.misc),
    )


def _manifest(loaded: LoadedSentence, result: SentenceResult) -> dict:
    adopted = [loaded.edge_numbers[index] for index in result.kept_edge_ids]
    dropped = [loaded.edge_numbers[index] for index in result.dropped_edge_ids]
    return {
        "sent_id": result.sent_id,
        "feasible": result.feasible,
        "placeholder_rows": {key: list(value) for key, value in result.placeholder_rows.items()},
        "adopted_original_relation_numbers": adopted,
        "dropped_original_relation_numbers": dropped,
        "output_edges": [
            {"src": edge.src, "dst": edge.dst, "label": edge.label}
            for edge in result.output_edges
        ],
        "query_answers": result.query_answers,
        "original_answers": result.original_answers,
        "ties": [
            {
                "role_assignment": dict(tie.assignment),
                "original_relation_numbers": [
                    loaded.edge_numbers[index] for index in tie.edge_indices
                ],
            }
            for tie in result.ties
        ],
        "diagnostics": result.diagnostics,
    }


def run_bundle(
    input_path: str | Path,
    output_conllu: str | Path,
    manifest_path: str | Path,
) -> list[SentenceResult]:
    loaded_items = load_bundle(input_path)
    conllu_parts: list[str] = []
    manifests: list[dict] = []
    results: list[SentenceResult] = []
    for loaded in loaded_items:
        result = solve_sentence(loaded.data)
        results.append(result)
        manifests.append(_manifest(loaded, result))
        if result.feasible and loaded.original_text:
            try:
                text, _ = build_deliverable(loaded, result)
            except ValueError as exc:
                result.feasible = False
                result.diagnostics.append(str(exc))
                manifests[-1] = _manifest(loaded, result)
                conllu_parts.append(
                    f"# sent_id = {result.sent_id}\n"
                    "# status = infeasible (see manifest diagnostics)\n\n"
                )
                continue
            conllu_parts.append(text if text.endswith("\n\n") else text + "\n")
        elif not result.feasible:
            conllu_parts.append(
                f"# sent_id = {result.sent_id}\n"
                "# status = infeasible (see manifest diagnostics)\n\n"
            )

    Path(output_conllu).write_text("".join(conllu_parts), encoding="utf-8")
    Path(manifest_path).write_text(
        json.dumps({"sentences": manifests}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return results
