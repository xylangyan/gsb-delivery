"""SAX stack implementation."""

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("saxpy")
except PackageNotFoundError:  # package is not installed (e.g. running from a source checkout)
    __version__ = "0.0.0"

__author__ = "Pavel Senin <seninp@gmail.com>"

_EXPORTS = {
    "RRADiscord": ("saxpy.rra", "RRADiscord"),
    "RepairGrammar": ("saxpy.repair_rle", "RepairGrammar"),
    "RepairRule": ("saxpy.repair", "RepairRule"),
    "bags_to_tfidf": ("saxpy.saxvsm", "bags_to_tfidf"),
    "classify_series": ("saxpy.saxvsm", "classify_series"),
    "classification_accuracy": ("saxpy.saxvsm", "classification_accuracy"),
    "cosine_distance": ("saxpy.saxvsm", "cosine_distance"),
    "cosine_similarity": ("saxpy.saxvsm", "cosine_similarity"),
    "cuts_for_asize": ("saxpy.alphabet", "cuts_for_asize"),
    "cv_error": ("saxpy.saxvsm_optimize", "cv_error"),
    "ecg0606_path": ("saxpy.datasets", "ecg0606_path"),
    "find_discords_brute_force": ("saxpy.discord", "find_discords_brute_force"),
    "find_discords_hotsax": ("saxpy.hotsax", "find_discords_hotsax"),
    "find_discords_rra": ("saxpy.rra", "find_discords_rra"),
    "load_ucr_data": ("saxpy.saxvsm", "load_ucr_data"),
    "manyseries_to_wordbag": ("saxpy.saxvsm", "manyseries_to_wordbag"),
    "optimize_parameters": ("saxpy.saxvsm_optimize", "optimize_parameters"),
    "paa": ("saxpy.paa", "paa"),
    "repair_grammar_from_runs": ("saxpy.repair_rle", "repair_grammar_from_runs"),
    "rle_to_repair_grammar": ("saxpy.repair_rle", "rle_to_repair_grammar"),
    "rule_expansion_length": ("saxpy.repair_rle", "rule_expansion_length"),
    "rule_intervals_in_window": ("saxpy.repair_rle", "rule_intervals_in_window"),
    "rule_occurrences_in_window": ("saxpy.repair_rle", "rule_occurrences_in_window"),
    "sax_by_chunking": ("saxpy.sax", "sax_by_chunking"),
    "sax_via_window": ("saxpy.sax", "sax_via_window"),
    "series_to_wordbag": ("saxpy.saxvsm", "series_to_wordbag"),
    "str_to_repair_grammar": ("saxpy.repair", "str_to_repair_grammar"),
    "str_to_repair_grammar_rle": ("saxpy.repair_rle", "str_to_repair_grammar_rle"),
    "train_tfidf": ("saxpy.saxvsm", "train_tfidf"),
    "ts_to_string": ("saxpy.sax", "ts_to_string"),
    "words_in_window": ("saxpy.repair_rle", "words_in_window"),
    "znorm": ("saxpy.znorm", "znorm"),
}

__all__ = list(_EXPORTS.keys())


def __getattr__(name: str):
    if name in _EXPORTS:
        module_name, attr_name = _EXPORTS[name]
        return getattr(import_module(module_name), attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
