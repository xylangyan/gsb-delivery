from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Mapping, Sequence, cast

from . import api as detector_api
from .api import from_bytes
from .constant import IANA_SUPPORTED
from .utils import any_specified_encoding, iana_name, identify_sig_or_bom

NO_CANDIDATE = "no_candidate"
ENCODING_NOT_FOUND = "encoding_not_found"
UNDECODABLE = "undecodable"
EXPECTED_MISMATCH = "expected_mismatch"
MATCHED = "matched"

_NO_CANDIDATE_ISOLATION = "__charset_normalizer_audit_no_candidates__"

_FROM_BYTES_KEYS = {
    "steps",
    "chunk_size",
    "threshold",
    "cp_isolation",
    "cp_exclusion",
    "preemptive_behaviour",
    "explain",
    "language_threshold",
    "enable_fallback",
}

_CONFIG_ENABLED_KEYS = (
    "cp_isolation",
    "isolation",
    "candidates",
    "candidate_encodings",
    "enabled_encodings",
    "enabled",
    "allow",
    "allowed",
    "fixed_enabled",
    "fixed_enable",
)
_CONFIG_DISABLED_KEYS = (
    "cp_exclusion",
    "exclusion",
    "exclusions",
    "disabled_encodings",
    "disabled",
    "exclude",
    "excluded",
    "blocked",
    "fixed_excluded",
    "fixed_disable",
)
_CONFIG_NAME_KEYS = ("name", "id", "site", "profile", "key", "label")
_CONFIG_SWITCH_KEYS = (
    "switches",
    "switch_values",
    "toggles",
    "options",
    "switch_states",
)


class AttrDict(Dict[str, Any]):
    """Dictionary record that also exposes keys as attributes."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


@dataclass(frozen=True)
class SwitchSpec:
    key: str
    encodings: frozenset[str]
    group: str | None = None
    default: bool = False
    together: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)


def _normalise_encoding(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"Encoding names must be strings, got {type(value).__name__}")
    return iana_name(value, False)


def _encoding_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        values: Iterable[Any] = [
            item for item in re.split(r"[,;|\s]+", value.strip()) if item
        ]
    elif isinstance(value, Mapping):
        values = value.keys()
    else:
        values = value
    return {_normalise_encoding(item) for item in values if item is not None}


def _first_mapping_value(data: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled", "enable", "y"}:
        return True
    if text in {"0", "false", "no", "off", "disabled", "disable", "n"}:
        return False
    return default


def parse_switch_specs(
    switches: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    encoding_groups: Mapping[str, Any] | None = None,
) -> dict[str, SwitchSpec]:
    """Parse single encodings and encodings that must toggle together."""
    if not switches:
        return {}

    groups: dict[str, frozenset[str]] = {}
    for group_name, encodings in (encoding_groups or {}).items():
        groups[str(group_name)] = frozenset(_encoding_set(encodings))

    parsed: dict[str, SwitchSpec] = {}

    def add_spec(key: str, raw: Any, fallback_group: str | None = None) -> None:
        if isinstance(raw, SwitchSpec):
            parsed[key] = raw
            return

        group_name = fallback_group
        default = False
        together = True
        metadata: dict[str, Any] = {}

        if isinstance(raw, Mapping):
            group_name = raw.get("group") or raw.get("encoding_group") or group_name
            default = _as_bool(raw.get("default") or raw.get("default_on"), False)
            together = _as_bool(
                raw.get("together", raw.get("grouped", raw.get("linked", True))),
                True,
            )
            raw_encodings = (
                raw.get("encodings")
                or raw.get("encoding")
                or raw.get("cp")
                or raw.get("code_pages")
                or raw.get("members")
            )
            if raw_encodings is None and group_name is None:
                raw_encodings = key
            metadata = {
                k: v
                for k, v in raw.items()
                if k
                not in {
                    "encodings",
                    "encoding",
                    "cp",
                    "code_pages",
                    "members",
                    "group",
                    "encoding_group",
                    "default",
                    "default_on",
                    "together",
                    "grouped",
                    "linked",
                }
            }
        else:
            raw_encodings = raw

        encodings: set[str] = set()
        if group_name is not None and group_name in groups:
            encodings.update(groups[group_name])
        if raw_encodings is not None:
            encodings.update(_encoding_set(raw_encodings))

        parsed[key] = SwitchSpec(
            key=str(key),
            encodings=frozenset(encodings),
            group=group_name,
            default=default,
            together=together or len(encodings) > 1,
            metadata=metadata,
        )

    if isinstance(switches, Mapping):
        for key, raw in switches.items():
            if isinstance(raw, bool):
                add_spec(str(key), {"encoding": str(key), "default": raw})
            else:
                add_spec(str(key), raw)
    else:
        for index, raw in enumerate(switches):
            if not isinstance(raw, Mapping):
                add_spec(str(index), raw)
                continue
            switch_key: Any = (
                raw.get("key")
                or raw.get("name")
                or raw.get("switch")
                or raw.get("id")
                or raw.get("label")
            )
            if switch_key is None:
                raise ValueError(f"Switch definition at index {index} has no name/key")
            add_spec(str(switch_key), raw)

    return parsed


def _extract_switch_values(
    config: Mapping[str, Any],
    override: Mapping[str, Any] | None = None,
) -> dict[str, bool]:
    values: dict[str, Any] = {}
    raw_values = _first_mapping_value(config, _CONFIG_SWITCH_KEYS)
    if isinstance(raw_values, Mapping):
        values.update(raw_values)
    if override:
        values.update(override)
    switch_values: dict[str, bool] = {}
    for key, value in values.items():
        if key in _FROM_BYTES_KEYS:
            continue
        if not isinstance(value, (bool, int, float, str)) and value is not None:
            continue
        switch_values[str(key)] = _as_bool(value)
    return switch_values


def _detector_params(config: Mapping[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    nested = (
        config.get("params")
        or config.get("detector")
        or config.get("kwargs")
        or config.get("options")
    )
    if isinstance(nested, Mapping):
        params.update(nested)
    for key in _FROM_BYTES_KEYS:
        if key in config and key not in {"cp_isolation", "cp_exclusion"}:
            params[key] = config[key]
    normalized: dict[str, Any] = {}
    for key, value in params.items():
        if key not in _FROM_BYTES_KEYS:
            continue
        if key in {"steps", "chunk_size"} and not isinstance(value, int):
            try:
                normalized[key] = int(value)
            except (TypeError, ValueError):
                normalized[key] = value
        elif key in {"threshold", "language_threshold"} and not isinstance(
            value, float
        ):
            try:
                normalized[key] = float(value)
            except (TypeError, ValueError):
                normalized[key] = value
        elif key in {"preemptive_behaviour", "explain", "enable_fallback"}:
            normalized[key] = _as_bool(value)
        else:
            normalized[key] = value
    return normalized


@dataclass(frozen=True)
class ResolvedConfig:
    name: str | None
    enabled: frozenset[str] | None
    disabled: frozenset[str]
    switch_values: Mapping[str, bool]
    params: Mapping[str, Any]
    groups: tuple[str, ...]
    expected_text: str | None
    expected_contains: str | None
    raw: Mapping[str, Any] = field(hash=False, compare=False, default_factory=dict)

    @property
    def call_kwargs(self) -> dict[str, Any]:
        kwargs = dict(self.params)
        if self.enabled is None:
            kwargs["cp_isolation"] = None
        elif len(self.enabled) == 0:
            kwargs["cp_isolation"] = [_NO_CANDIDATE_ISOLATION]
        else:
            kwargs["cp_isolation"] = sorted(self.enabled)
        kwargs["cp_exclusion"] = sorted(self.disabled) if self.disabled else None
        return kwargs

    @property
    def native_kwargs(self) -> dict[str, Any]:
        """Arguments exactly as a table's native cp_* fields requested."""
        kwargs = dict(self.params)
        if "cp_isolation" in self.raw:
            kwargs["cp_isolation"] = self.raw["cp_isolation"]
        if "cp_exclusion" in self.raw:
            kwargs["cp_exclusion"] = self.raw["cp_exclusion"]
        return kwargs


def resolve_config(
    config: Any = None,
    switch_specs: Mapping[str, SwitchSpec] | None = None,
    switch_values: Mapping[str, Any] | None = None,
    **overrides: Any,
) -> ResolvedConfig:
    """Resolve fixed sets and binary/group switches into detector arguments."""
    if isinstance(config, ResolvedConfig):
        if switch_values is None and not overrides:
            return config
        data = dict(config.raw)
        data["switches"] = {**dict(config.switch_values), **dict(switch_values or {})}
    elif config is None:
        data = {}
    elif isinstance(config, Mapping):
        data = dict(config)
    elif isinstance(config, str):
        data = {"cp_isolation": [config], "name": config}
    elif isinstance(config, (list, tuple, set, frozenset)):
        data = {"cp_isolation": list(config)}
    else:
        raise TypeError(f"Unsupported configuration type: {type(config).__name__}")

    if overrides:
        data.update(overrides)

    name = _first_mapping_value(data, _CONFIG_NAME_KEYS)
    groups_raw = data.get("config_groups") or data.get("groups") or data.get("group")
    if groups_raw is None:
        groups: tuple[str, ...] = ()
    elif isinstance(groups_raw, str):
        groups = (groups_raw,)
    else:
        groups = tuple(str(item) for item in groups_raw)

    explicit_enabled = _first_mapping_value(data, _CONFIG_ENABLED_KEYS)
    explicit_disabled = _first_mapping_value(data, _CONFIG_DISABLED_KEYS)

    enabled: set[str] | None
    if explicit_enabled is None:
        enabled = None
    else:
        enabled = _encoding_set(explicit_enabled)

    disabled = _encoding_set(explicit_disabled)

    values = _extract_switch_values(data, switch_values)
    specs = switch_specs or {}
    for switch_key, enabled_state in values.items():
        spec = specs.get(switch_key)
        encodings = set(spec.encodings) if spec else {_normalise_encoding(switch_key)}
        if enabled_state:
            if enabled is not None:
                enabled.update(encodings)
        else:
            disabled.update(encodings)

    fixed_enabled = data.get("fixed_enabled") or data.get("fixed_enable")
    if fixed_enabled is not None:
        if enabled is None:
            enabled = set()
        enabled.update(_encoding_set(fixed_enabled))

    fixed_disabled = data.get("fixed_disabled") or data.get("fixed_excluded")
    if fixed_disabled is not None:
        disabled.update(_encoding_set(fixed_disabled))

    # Fixed exclusion wins over an enabled switch/isolation entry.
    if enabled is not None:
        enabled.difference_update(disabled)

    expected_text = data.get("expected_text") or data.get("expected")
    expected_contains = data.get("expected_contains") or data.get("contains")

    return ResolvedConfig(
        name=str(name) if name is not None else None,
        enabled=None if enabled is None else frozenset(enabled),
        disabled=frozenset(disabled),
        switch_values=values,
        params=_detector_params(data),
        groups=groups,
        expected_text=expected_text,
        expected_contains=expected_contains,
        raw=data,
    )


def candidate_order(
    payload: bytes | bytearray,
    preemptive_behaviour: bool = True,
) -> list[str]:
    """Return the detector's encoding order for one concrete payload."""
    specified = any_specified_encoding(payload) if preemptive_behaviour else None
    sig_encoding, _ = identify_sig_or_bom(payload)

    prioritized: list[str] = []
    if specified is not None:
        prioritized.append(_normalise_encoding(specified))
    if sig_encoding is not None:
        prioritized.insert(0, _normalise_encoding(sig_encoding))
    prioritized.append("ascii")
    if "utf_8" not in prioritized:
        prioritized.append("utf_8")

    rest = list(detector_api.IANA_SUPPORTED_MB_FIRST)
    ordered: list[str] = []
    seen: set[str] = set()
    for encoding in prioritized + rest + list(IANA_SUPPORTED):
        encoding = _normalise_encoding(encoding)
        if encoding not in seen:
            seen.add(encoding)
            ordered.append(encoding)
    return ordered


def candidate_is_allowed(
    encoding: str,
    config: ResolvedConfig | Mapping[str, Any] | None = None,
) -> bool:
    resolved = config if isinstance(config, ResolvedConfig) else resolve_config(config)
    normalised = _normalise_encoding(encoding)
    if normalised in resolved.disabled:
        return False
    if resolved.enabled is None:
        return True
    return normalised in resolved.enabled


def first_candidate_difference(
    payload: bytes | bytearray,
    left: Any,
    right: Any,
    switch_specs: Mapping[str, SwitchSpec] | None = None,
) -> AttrDict | None:
    """Find the first canonical candidate whose site decision differs."""
    left_config = (
        left if isinstance(left, ResolvedConfig) else resolve_config(left, switch_specs)
    )
    right_config = (
        right
        if isinstance(right, ResolvedConfig)
        else resolve_config(right, switch_specs)
    )

    preemptive = bool(
        left_config.params.get(
            "preemptive_behaviour",
            right_config.params.get("preemptive_behaviour", True),
        )
    )
    order = candidate_order(payload, preemptive)

    for index, encoding in enumerate(order):
        left_allowed = candidate_is_allowed(encoding, left_config)
        right_allowed = candidate_is_allowed(encoding, right_config)
        if left_allowed != right_allowed:
            switches = [
                key
                for key, spec in (switch_specs or {}).items()
                if encoding in spec.encodings
            ]
            return AttrDict(
                index=index,
                position=index + 1,
                encoding=encoding,
                left_allowed=left_allowed,
                right_allowed=right_allowed,
                left_decision="enabled" if left_allowed else "disabled",
                right_decision="enabled" if right_allowed else "disabled",
                switches=switches,
            )

    extra_left = set(left_config.enabled or ()) - set(order)
    extra_right = set(right_config.enabled or ()) - set(order)
    if extra_left != extra_right or left_config.disabled != right_config.disabled:
        only_left = sorted(extra_left - extra_right)
        only_right = sorted(extra_right - extra_left)
        candidate_values: Sequence[str] = (
            only_left
            or only_right
            or sorted(left_config.disabled ^ right_config.disabled)
        )
        extra_encoding: str | None = candidate_values[0] if candidate_values else None
        return AttrDict(
            index=len(order),
            position=len(order) + 1,
            encoding=extra_encoding,
            left_allowed=extra_encoding not in left_config.disabled
            if extra_encoding
            else None,
            right_allowed=(
                extra_encoding not in right_config.disabled if extra_encoding else None
            ),
            left_decision="extra_configuration",
            right_decision="extra_configuration",
            switches=[],
        )
    return None


def representative_for(results: Any, encoding: str) -> tuple[Any, bool]:
    """Return the real top-level owner and whether requested name is a leaf."""
    requested = _normalise_encoding(encoding)
    try:
        match = results[encoding]
    except (KeyError, ValueError):
        return None, False
    is_submatch = match.encoding != requested
    if not is_submatch:
        # An alias can be listed by could_be but normalise to owner encoding.
        is_submatch = requested not in {
            _normalise_encoding(match.encoding)
        } and requested in {
            _normalise_encoding(item.encoding) for item in match.submatch
        }
    return match, is_submatch


def _digest_text(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _safe_str(match: Any) -> tuple[str | None, str | None]:
    if match is None:
        return None, None
    try:
        return str(match), None
    except (UnicodeError, LookupError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _check_expected(text: str | None, config: ResolvedConfig) -> str | None:
    if text is None:
        return UNDECODABLE
    if config.expected_text is not None and text != config.expected_text:
        return EXPECTED_MISMATCH
    if config.expected_contains is not None and config.expected_contains not in text:
        return EXPECTED_MISMATCH
    return MATCHED


def inspect_configuration(
    payload: bytes | bytearray,
    config: Any = None,
    encoding: str | None = None,
    switch_specs: Mapping[str, SwitchSpec] | None = None,
    shared_cache: dict[str, Any] | None = None,
    use_native_cp_fields: bool = False,
    **kwargs: Any,
) -> AttrDict:
    """Read one site configuration through the original detector entry point."""
    resolved = resolve_config(config, switch_specs, **kwargs)

    detector_kwargs = (
        resolved.native_kwargs if use_native_cp_fields else resolved.call_kwargs
    )
    if shared_cache is not None:
        detector_kwargs["_shared_cache"] = shared_cache

    results = from_bytes(payload, **detector_kwargs)
    best = results.best()

    primary_text, primary_decode_error = _safe_str(best)
    primary_status = (
        NO_CANDIDATE if best is None else _check_expected(primary_text, resolved)
    )
    if best is not None and primary_text is None:
        primary_status = UNDECODABLE

    representative = None
    is_submatch = False
    lookup_status = None
    lookup_error = None
    representative_text = None
    representative_decode_error = None
    if encoding is not None:
        representative, is_submatch = representative_for(results, encoding)
        if representative is None:
            lookup_status = ENCODING_NOT_FOUND
            lookup_error = f"KeyError: {encoding!r}"
        else:
            representative_text, representative_decode_error = _safe_str(representative)
            lookup_status = UNDECODABLE if representative_text is None else MATCHED

    target_text = representative_text if encoding is not None else primary_text
    expected_status = (
        _check_expected(target_text, resolved) if best is not None else NO_CANDIDATE
    )
    if encoding is not None and representative is None:
        expected_status = ENCODING_NOT_FOUND

    outcome_encoding = best.encoding if best is not None else None
    outcome_text = primary_text
    outcome_digest = _digest_text(outcome_text)
    if best is not None and outcome_text is None:
        outcome_identity = f"undecodable:{best.encoding}"
    elif outcome_digest is None:
        outcome_identity = NO_CANDIDATE
    else:
        outcome_identity = outcome_digest

    return AttrDict(
        name=resolved.name,
        config=resolved,
        results=results,
        best=best,
        primary=best,
        representative=representative,
        requested_encoding=_normalise_encoding(encoding) if encoding else None,
        is_submatch=is_submatch,
        text=primary_text,
        primary_text=primary_text,
        representative_text=representative_text,
        decode_error=primary_decode_error or representative_decode_error,
        primary_status=primary_status,
        lookup_status=lookup_status,
        lookup_error=lookup_error,
        expected_status=expected_status,
        status=expected_status,
        outcome_encoding=outcome_encoding,
        encoding=outcome_encoding,
        text_sha256=outcome_digest,
        fingerprint=best.fingerprint
        if best is not None and primary_text is not None
        else None,
        outcome_key=(outcome_encoding, outcome_identity),
        outcome_identity=outcome_identity,
    )


def primary_and_representative(
    payload: bytes | bytearray,
    config: Any = None,
    encoding: str | None = None,
    **kwargs: Any,
) -> tuple[Any, Any]:
    """Return the native primary match and the owner found by encoding name."""
    record = inspect_configuration(payload, config, encoding=encoding, **kwargs)
    return record.best, record.representative


# Descriptive aliases for callers/tests that phrase the operation differently.
read_configuration = inspect_configuration
inspect_site = inspect_configuration
read_site = inspect_configuration
audit_config = inspect_configuration
evaluate_configuration = inspect_configuration
get_primary_and_representative = primary_and_representative
read_primary_and_representative = primary_and_representative


def _looks_like_config(data: Mapping[str, Any]) -> bool:
    config_keys = set(
        _CONFIG_ENABLED_KEYS + _CONFIG_DISABLED_KEYS + _CONFIG_SWITCH_KEYS
    )
    config_keys.update(_CONFIG_NAME_KEYS)
    config_keys.update(_FROM_BYTES_KEYS)
    config_keys.update(
        {
            "params",
            "detector",
            "kwargs",
            "expected",
            "expected_text",
            "expected_contains",
            "fixed_enabled",
            "fixed_disabled",
            "fixed_excluded",
        }
    )
    return any(key in data for key in config_keys)


def _iter_config_entries(configs: Any) -> Iterator[tuple[str | None, Any]]:
    if configs is None:
        return
    if isinstance(configs, Mapping) and "configs" in configs:
        table_name = configs.get("name")
        for index, entry in enumerate(_iter_config_entries(configs["configs"])):
            entry_name, entry_config = entry
            if isinstance(entry_config, Mapping) and table_name:
                entry_config = dict(entry_config)
                groups = list(
                    entry_config.get("config_groups")
                    or entry_config.get("groups")
                    or ()
                )
                groups.append(str(table_name))
                entry_config["config_groups"] = groups
            yield entry_name or f"config-{index}", entry_config
        return
    if isinstance(configs, Mapping) and _looks_like_config(configs):
        name = _first_mapping_value(configs, _CONFIG_NAME_KEYS)
        yield str(name) if name is not None else None, configs
        return
    if isinstance(configs, Mapping):
        for group_or_name, value in configs.items():
            if isinstance(value, (list, tuple)) and not isinstance(value, (str, bytes)):
                for index, item in enumerate(value):
                    if isinstance(item, Mapping):
                        item = dict(item)
                        groups = list(
                            item.get("config_groups") or item.get("groups") or ()
                        )
                        groups.append(str(group_or_name))
                        item["config_groups"] = tuple(dict.fromkeys(groups))
                    name = item.get("name") if isinstance(item, Mapping) else None
                    yield str(name) if name else f"{group_or_name}-{index}", item
            else:
                yield str(group_or_name), value
        return
    if isinstance(configs, (str, bytes)):
        yield None, configs
        return
    for index, config in enumerate(configs):
        if isinstance(config, Mapping):
            name = _first_mapping_value(config, _CONFIG_NAME_KEYS)
            yield str(name) if name is not None else f"config-{index}", config
        else:
            yield f"config-{index}", config


def _config_cache_key(
    resolved: ResolvedConfig, payload: bytes | bytearray
) -> tuple[Any, ...]:
    order = candidate_order(
        payload,
        bool(resolved.params.get("preemptive_behaviour", True)),
    )
    allowed = tuple(
        encoding for encoding in order if candidate_is_allowed(encoding, resolved)
    )
    extra_enabled = tuple(sorted((resolved.enabled or frozenset()) - set(order)))
    params = tuple(sorted((key, repr(value)) for key, value in resolved.params.items()))
    return allowed, extra_enabled, params


def _switch_delta(
    left: ResolvedConfig,
    right: ResolvedConfig,
    switch_specs: Mapping[str, SwitchSpec] | None,
    divergence: AttrDict | None,
) -> list[AttrDict]:
    keys = set(left.switch_values) | set(right.switch_values)
    changed: list[AttrDict] = []
    for key in sorted(keys):
        left_value = left.switch_values.get(key)
        right_value = right.switch_values.get(key)
        if left_value != right_value:
            spec = (switch_specs or {}).get(key)
            changed.append(
                AttrDict(
                    switch=key,
                    left=left_value,
                    right=right_value,
                    encodings=sorted(spec.encodings) if spec else [],
                    together=spec.together if spec else False,
                )
            )
    if not changed and divergence is not None and divergence.switches:
        for key in divergence.switches:
            spec = (switch_specs or {}).get(key)
            changed.append(
                AttrDict(
                    switch=key,
                    left=divergence.left_allowed,
                    right=divergence.right_allowed,
                    encodings=sorted(spec.encodings) if spec else [divergence.encoding],
                    together=spec.together if spec else False,
                    inferred=True,
                )
            )
    return changed


def _comparison_from_records(
    payload: bytes | bytearray,
    left_record: AttrDict,
    right_record: AttrDict,
    switch_specs: Mapping[str, SwitchSpec] | None = None,
) -> AttrDict:
    left_resolved = left_record.config
    right_resolved = right_record.config
    divergence = first_candidate_difference(
        payload, left_resolved, right_resolved, switch_specs
    )
    changed_switches = _switch_delta(
        left_resolved, right_resolved, switch_specs, divergence
    )
    outcome_changed = left_record.outcome_key != right_record.outcome_key
    return AttrDict(
        left=left_record,
        right=right_record,
        first_divergence=divergence,
        first_candidate_difference=divergence,
        changed_switches=changed_switches,
        switch_sheet=changed_switches,
        outcome_changed=outcome_changed,
        text_changed=left_record.text != right_record.text,
        encoding_changed=left_record.outcome_encoding != right_record.outcome_encoding,
    )


def compare_configurations(
    payload: bytes | bytearray,
    left: Any,
    right: Any,
    switch_specs: Mapping[str, SwitchSpec] | None = None,
    shared_cache: dict[str, Any] | None = None,
    **kwargs: Any,
) -> AttrDict:
    left_resolved = resolve_config(left, switch_specs, **kwargs)
    right_resolved = resolve_config(right, switch_specs, **kwargs)
    left_record = inspect_configuration(
        payload, left_resolved, switch_specs=switch_specs, shared_cache=shared_cache
    )
    right_record = inspect_configuration(
        payload, right_resolved, switch_specs=switch_specs, shared_cache=shared_cache
    )
    return _comparison_from_records(payload, left_record, right_record, switch_specs)


compare_sites = compare_configurations
diff_configurations = compare_configurations


def _impact_sheet_entry(impact: AttrDict) -> AttrDict:
    return AttrDict(
        switch=impact.switch,
        encodings=impact.encodings,
        together=impact.together,
        base_value=impact.base_value,
        variant_value=impact.variant_value,
        first_divergence=impact.first_divergence,
        base_encoding=impact.base_encoding,
        variant_encoding=impact.variant_encoding,
        base_text_sha256=impact.base_text_sha256,
        variant_text_sha256=impact.variant_text_sha256,
    )


def _switch_sheet_for_outcome(
    impacts: Sequence[AttrDict] | None, outcome_key: tuple[Any, ...]
) -> list[AttrDict]:
    if not impacts:
        return []
    return [
        _impact_sheet_entry(impact)
        for impact in impacts
        if impact.changes_outcome
        and (
            impact.base.outcome_key == outcome_key
            or impact.variant.outcome_key == outcome_key
        )
    ]


def audit_configurations(
    payload: bytes | bytearray,
    configs: Any = None,
    switch_specs: Mapping[str, SwitchSpec] | Sequence[Mapping[str, Any]] | None = None,
    encoding_groups: Mapping[str, Any] | None = None,
    encoding: str | None = None,
    switches: Any = None,
    shared_cache: dict[str, Any] | None = None,
    base_config: Any = None,
    include_impacts: bool = False,
    **kwargs: Any,
) -> AttrDict:
    """Audit distinct effective configurations and count final outcomes."""
    if switch_specs is None and switches is not None:
        if isinstance(switches, Mapping) and all(
            isinstance(v, bool) for v in switches.values()
        ):
            switch_specs = None
            kwargs.setdefault("switch_values", switches)
        else:
            switch_specs = switches
    specs = (
        switch_specs
        if isinstance(switch_specs, dict)
        and all(isinstance(v, SwitchSpec) for v in switch_specs.values())
        else parse_switch_specs(switch_specs, encoding_groups)
    )

    entries = list(_iter_config_entries(configs))
    if not entries:
        entries = [(None, None)]

    metric_cache: dict[str, Any] = {} if shared_cache is None else shared_cache
    unique: dict[tuple[Any, ...], AttrDict] = {}
    records: list[AttrDict] = []

    for input_name, raw_config in entries:
        resolved = resolve_config(raw_config, specs, **kwargs)
        if input_name and not resolved.name:
            resolved = ResolvedConfig(
                name=input_name,
                enabled=resolved.enabled,
                disabled=resolved.disabled,
                switch_values=resolved.switch_values,
                params=resolved.params,
                groups=resolved.groups,
                expected_text=resolved.expected_text,
                expected_contains=resolved.expected_contains,
                raw=resolved.raw,
            )
        key = _config_cache_key(resolved, payload)
        if key in unique:
            existing = unique[key]
            existing.duplicate_names.append(resolved.name or input_name)
            continue

        record = inspect_configuration(
            payload,
            resolved,
            encoding=encoding,
            switch_specs=specs,
            shared_cache=metric_cache,
        )
        record["effective_key"] = key
        record["duplicate_names"] = []
        unique[key] = record
        records.append(record)

    groups_map: dict[tuple[Any, ...], list[AttrDict]] = {}
    for record in records:
        groups_map.setdefault(record.outcome_key, []).append(record)

    comparisons: list[AttrDict] = []
    for left_index, left_record in enumerate(records):
        for right_record in records[left_index + 1 :]:
            if left_record.outcome_key == right_record.outcome_key:
                continue
            comparison = _comparison_from_records(
                payload,
                left_record,
                right_record,
                specs,
            )
            comparison["left_name"] = left_record.name
            comparison["right_name"] = right_record.name
            comparisons.append(comparison)

    impacts: list[AttrDict] | None = None
    if include_impacts:
        impacts = switch_impact_report(
            payload,
            specs,
            base_config=base_config,
            shared_cache=metric_cache,
            **kwargs,
        )

    groups: list[AttrDict] = []
    for outcome_key, members in groups_map.items():
        witness = None
        for comparison in comparisons:
            if (
                comparison.left.outcome_key == outcome_key
                or comparison.right.outcome_key == outcome_key
            ):
                if witness is None or len(comparison.changed_switches) < len(
                    witness.changed_switches
                ):
                    witness = comparison
        changing_switches = sorted(
            {
                switch.switch
                for comparison in comparisons
                if comparison.left.outcome_key == outcome_key
                or comparison.right.outcome_key == outcome_key
                for switch in comparison.changed_switches
            }
        )
        first = members[0]
        groups.append(
            AttrDict(
                outcome_key=list(outcome_key),
                encoding=first.outcome_encoding,
                text=first.text,
                text_sha256=first.text_sha256,
                fingerprint=first.fingerprint,
                statuses=sorted({record.status for record in members}),
                count=len(members),
                record_count=len(members)
                + sum(len(record.duplicate_names) for record in members),
                configs=[record.name for record in members],
                names=[record.name for record in members],
                duplicate_names={
                    record.name: record.duplicate_names for record in members
                },
                changing_switches=changing_switches,
                switch_sheet=witness.changed_switches if witness is not None else [],
                effective_switch_sheet=_switch_sheet_for_outcome(impacts, outcome_key),
                witness=witness,
            )
        )

    groups.sort(
        key=lambda group: (-group.count, group.encoding or "", group.text_sha256 or "")
    )

    counts_by_outcome = {
        (group.encoding, group.text_sha256 or group.outcome_key[1]): group.count
        for group in groups
    }
    counts_by_text_identity = {
        (group.encoding, group.fingerprint): group.count
        for group in groups
        if group.fingerprint is not None
    }
    counts_by_encoding: dict[str | None, int] = {}
    for group in groups:
        counts_by_encoding[group.encoding] = (
            counts_by_encoding.get(group.encoding, 0) + group.count
        )

    pair_switch_sheet: list[AttrDict] = []
    seen_switch_deltas: set[tuple[str, str, str]] = set()
    for comparison in comparisons:
        for switch_change in comparison.changed_switches:
            delta_key = (
                switch_change.switch,
                repr(switch_change.left),
                repr(switch_change.right),
            )
            if delta_key in seen_switch_deltas:
                continue
            seen_switch_deltas.add(delta_key)
            pair_switch_sheet.append(switch_change)

    return AttrDict(
        records=records,
        groups=groups,
        outcomes=groups,
        comparisons=comparisons,
        switch_impacts=impacts,
        switch_sheet=pair_switch_sheet,
        changing_switches=sorted(
            {switch_change.switch for switch_change in pair_switch_sheet}
        ),
        effective_switch_sheet=[
            _impact_sheet_entry(impact)
            for impact in (impacts or [])
            if impact.changes_outcome
        ],
        counts_by_outcome=counts_by_outcome,
        outcome_counts=counts_by_outcome,
        counts_by_text_identity=counts_by_text_identity,
        fingerprint_counts=counts_by_text_identity,
        counts_by_encoding=counts_by_encoding,
        total_configurations=len(entries),
        distinct_configurations=len(records),
        duplicate_configurations=len(entries) - len(records),
        metric_cache=metric_cache,
    )


audit_payload = audit_configurations
analyze_configurations = audit_configurations
summarize_outcomes = audit_configurations
run_audit = audit_configurations
audit_profiles = audit_configurations
analyze_profiles = audit_configurations
build_report = audit_configurations
generate_report = audit_configurations
count_outcomes = audit_configurations


def outcome_count_map(
    *args: Any, **kwargs: Any
) -> dict[tuple[str | None, str | None], int]:
    report = audit_configurations(*args, **kwargs)
    return cast(
        "dict[tuple[str | None, str | None], int]",
        dict(report.counts_by_outcome),
    )


def text_identity_count_map(
    *args: Any, **kwargs: Any
) -> dict[tuple[str | None, int | None], int]:
    report = audit_configurations(*args, **kwargs)
    return cast(
        "dict[tuple[str | None, int | None], int]",
        dict(report.counts_by_text_identity),
    )


get_best_and_representative = primary_and_representative
get_primary_and_named_match = primary_and_representative
find_first_candidate_difference = first_candidate_difference
diff_sites = compare_configurations


def switch_impact_report(
    payload: bytes | bytearray,
    switch_specs: Mapping[str, SwitchSpec] | Sequence[Mapping[str, Any]] | None,
    base_config: Any = None,
    switch_values: Mapping[str, Any] | None = None,
    shared_cache: dict[str, Any] | None = None,
    **kwargs: Any,
) -> list[AttrDict]:
    """Toggle each switch once and report whether the final identity changes."""
    specs = (
        switch_specs
        if isinstance(switch_specs, dict)
        and all(isinstance(v, SwitchSpec) for v in switch_specs.values())
        else parse_switch_specs(switch_specs)
    )
    base_values = {key: _as_bool(value.default) for key, value in specs.items()}
    if isinstance(base_config, ResolvedConfig):
        base_values.update(base_config.switch_values)
    elif isinstance(base_config, Mapping):
        base_values.update(_extract_switch_values(base_config))
    if switch_values:
        base_values.update(switch_values)
    base_resolved = resolve_config(base_config, specs, base_values, **kwargs)
    base_record = inspect_configuration(
        payload, base_resolved, shared_cache=shared_cache, **kwargs
    )

    impacts: list[AttrDict] = []
    for key, spec in specs.items():
        toggled_values = dict(base_resolved.switch_values)
        toggled_values[key] = not _as_bool(toggled_values.get(key, spec.default))
        variant = resolve_config(base_config, specs, toggled_values, **kwargs)
        variant_record = inspect_configuration(
            payload, variant, shared_cache=shared_cache, **kwargs
        )
        divergence = first_candidate_difference(payload, base_resolved, variant, specs)
        changed = base_record.outcome_key != variant_record.outcome_key
        impacts.append(
            AttrDict(
                switch=key,
                encodings=sorted(spec.encodings),
                together=spec.together,
                base_value=base_resolved.switch_values.get(key, spec.default),
                variant_value=toggled_values[key],
                changes_outcome=changed,
                effective=changed,
                first_divergence=divergence,
                base_encoding=base_record.outcome_encoding,
                variant_encoding=variant_record.outcome_encoding,
                base_text_sha256=base_record.text_sha256,
                variant_text_sha256=variant_record.text_sha256,
                base=base_record,
                variant=variant_record,
            )
        )
    return impacts


switch_sheet = switch_impact_report
build_switch_sheet = switch_impact_report
effective_switch_report = switch_impact_report


class SiteAuditor:
    """Reusable auditor with one shared probe cache."""

    def __init__(
        self,
        switch_specs: Mapping[str, SwitchSpec]
        | Sequence[Mapping[str, Any]]
        | None = None,
        encoding_groups: Mapping[str, Any] | None = None,
        **defaults: Any,
    ) -> None:
        self.switch_specs = (
            switch_specs
            if isinstance(switch_specs, dict)
            and all(isinstance(value, SwitchSpec) for value in switch_specs.values())
            else parse_switch_specs(switch_specs, encoding_groups)
        )
        self.defaults = defaults
        self.cache: dict[str, Any] = {}

    def inspect(
        self, payload: bytes | bytearray, config: Any = None, **kwargs: Any
    ) -> AttrDict:
        options = {**self.defaults, **kwargs}
        options.pop("switch_specs", None)
        options.pop("shared_cache", None)
        return inspect_configuration(
            payload,
            config,
            switch_specs=self.switch_specs,
            shared_cache=self.cache,
            **options,
        )

    def compare(
        self, payload: bytes | bytearray, left: Any, right: Any, **kwargs: Any
    ) -> AttrDict:
        options = {**self.defaults, **kwargs}
        options.pop("switch_specs", None)
        options.pop("shared_cache", None)
        return compare_configurations(
            payload,
            left,
            right,
            switch_specs=self.switch_specs,
            shared_cache=self.cache,
            **options,
        )

    def audit(
        self, payload: bytes | bytearray, configs: Any = None, **kwargs: Any
    ) -> AttrDict:
        options = {**self.defaults, **kwargs}
        options.pop("switch_specs", None)
        options.setdefault("shared_cache", self.cache)
        return audit_configurations(
            payload,
            configs,
            switch_specs=self.switch_specs,
            **options,
        )

    def impacts(self, payload: bytes | bytearray, **kwargs: Any) -> list[AttrDict]:
        options = {**self.defaults, **kwargs}
        options.pop("switch_specs", None)
        options.pop("shared_cache", None)
        return switch_impact_report(
            payload,
            self.switch_specs,
            shared_cache=self.cache,
            **options,
        )

    def volume(
        self, source: str | Path | Sequence[str | Path], **kwargs: Any
    ) -> AttrDict:
        options = {**self.defaults, **kwargs}
        options.pop("switch_specs", None)
        options.setdefault("shared_cache", self.cache)
        return audit_volume(source, switch_specs=self.switch_specs, **options)


EncodingAuditor = SiteAuditor
ConfigurationAuditor = SiteAuditor
EncodingConfigurationMatrix = SiteAuditor


def audit_volume(
    source: str | bytes | Path | Sequence[str | Path],
    configs: Any = None,
    *,
    pattern: str = "*",
    recursive: bool = True,
    **kwargs: Any,
) -> AttrDict:
    """Run configuration audit over one file, directory, or path sequence."""
    if isinstance(source, (bytes, bytearray)):
        report = audit_configurations(source, configs, **kwargs)
        return AttrDict(
            files={"<payload>": report},
            file_count=1,
            reports=[report],
        )

    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_dir():
            paths = sorted(path.rglob(pattern) if recursive else path.glob(pattern))
        else:
            paths = [path]
    else:
        paths = [Path(item) for item in source]

    files: dict[str, AttrDict] = {}
    for path in paths:
        if not path.is_file():
            continue
        payload = path.read_bytes()
        files[str(path)] = audit_configurations(payload, configs, **kwargs)

    return AttrDict(
        files=files,
        file_count=len(files),
        reports=list(files.values()),
    )


def to_jsonable(value: Any, *, include_matches: bool = False) -> Any:
    """Convert audit records to JSON-safe data without native match objects."""
    if isinstance(value, AttrDict):
        data = {}
        for key, item in value.items():
            if key == "metric_cache" and isinstance(item, Mapping):
                data[key] = {
                    name: len(store) if isinstance(store, Mapping) else store
                    for name, store in item.items()
                }
            else:
                data[key] = to_jsonable(item, include_matches=include_matches)
        return data
    if isinstance(value, ResolvedConfig):
        return {
            "name": value.name,
            "enabled": sorted(value.enabled) if value.enabled is not None else None,
            "disabled": sorted(value.disabled),
            "switch_values": dict(value.switch_values),
            "params": dict(value.params),
            "groups": list(value.groups),
            "expected_text": value.expected_text,
            "expected_contains": value.expected_contains,
        }
    if isinstance(value, SwitchSpec):
        return {
            "key": value.key,
            "encodings": sorted(value.encodings),
            "group": value.group,
            "default": value.default,
            "together": value.together,
        }
    if hasattr(value, "encoding") and hasattr(value, "fingerprint"):
        if not include_matches:
            return f"<CharsetMatch {value.encoding}>"
        return {
            "encoding": value.encoding,
            "chaos": value.chaos,
            "coherence": value.coherence,
            "bom": value.bom,
            "fingerprint": value.fingerprint,
            "could_be_from_charset": value.could_be_from_charset,
            "text": str(value),
        }
    if isinstance(value, Mapping):
        return {
            str(key): to_jsonable(item, include_matches=include_matches)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item, include_matches=include_matches) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def load_config_table(path: str | Path) -> Any:
    """Load a JSON table or a flat CSV whose columns match config keys."""
    config_path = Path(path)
    text = config_path.read_text(encoding="utf-8-sig")
    if config_path.suffix.lower() == ".csv":
        from csv import DictReader
        from io import StringIO

        rows: list[dict[str, Any]] = []
        for row in DictReader(StringIO(text)):
            parsed_row: dict[str, Any] = {}
            for key, value in row.items():
                if key is None or value is None or value == "":
                    continue
                stripped = value.strip()
                if stripped[:1] in {"[", "{"}:
                    try:
                        parsed_row[key] = json.loads(stripped)
                        continue
                    except json.JSONDecodeError:
                        pass
                parsed_row[key] = value
            rows.append(parsed_row)
        return rows
    return json.loads(text)


def dump_json(report: Any, path: str | Path | None = None) -> str:
    text = json.dumps(
        to_jsonable(report), ensure_ascii=False, indent=2, sort_keys=False
    )
    if path is not None:
        Path(path).write_text(text + "\n", encoding="utf-8")
    return text


def main(argv: Sequence[str] | None = None) -> int:
    from argparse import ArgumentParser

    parser = ArgumentParser(prog="charset-normalizer-audit")
    parser.add_argument("source", help="Original file or volume directory")
    parser.add_argument("--config", help="JSON configuration table")
    parser.add_argument(
        "--encoding", help="Encoding name to look up in each native result"
    )
    parser.add_argument("--output", help="Write JSON report to this path")
    parser.add_argument(
        "--pattern", default="*", help="Glob pattern when source is a directory"
    )
    args = parser.parse_args(argv)

    table: Any = None
    if args.config:
        table = load_config_table(args.config)

    if isinstance(table, Mapping) and "configs" in table:
        configs = table.get("configs")
        switch_specs = table.get("switches") or table.get("switch_specs")
        encoding_groups = table.get("encoding_groups") or table.get("groups")
        extra = {
            key: table[key]
            for key in ("steps", "chunk_size", "threshold", "language_threshold")
            if key in table
        }
    else:
        configs = table
        switch_specs = None
        encoding_groups = None
        extra = {}

    source_path = Path(args.source)
    if source_path.is_dir():
        report = audit_volume(
            source_path,
            configs,
            pattern=args.pattern,
            encoding=args.encoding,
            switch_specs=switch_specs,
            encoding_groups=encoding_groups,
            **extra,
        )
    else:
        report = audit_configurations(
            source_path.read_bytes(),
            configs,
            encoding=args.encoding,
            switch_specs=switch_specs,
            encoding_groups=encoding_groups,
            **extra,
        )

    text = dump_json(report, args.output)
    if args.output is None:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
