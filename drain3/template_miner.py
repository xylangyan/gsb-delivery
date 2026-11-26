# SPDX-License-Identifier: MIT

import base64
import logging
import re
import time
import zlib
from typing import Optional, Mapping, MutableMapping, NamedTuple, Sequence, Tuple, Union

import jsonpickle  # type: ignore[import]
from cachetools import LRUCache, cachedmethod

from drain3.drain import Drain, DrainBase, LogCluster
from drain3.incremental_persistence import IncrementalFilePersistence
from drain3.masking import LogMasker
from drain3.persistence_handler import PersistenceHandler
from drain3.simple_profiler import SimpleProfiler, NullProfiler, Profiler
from drain3.template_miner_config import TemplateMinerConfig


logger = logging.getLogger(__name__)

config_filename = 'drain3.ini'

ExtractedParameter = NamedTuple("ExtractedParameter", [("value", str), ("mask_name", str)])


class TemplateMiner:

    def __init__(self,
                 persistence_handler: Optional[PersistenceHandler] = None,
                 config: Optional[TemplateMinerConfig] = None):
        """
        Wrapper for Drain with persistence and masking support
        :param persistence_handler: The type of persistence to use. When None, no persistence is applied.
        :param config: Configuration object. When none, configuration is loaded from default .ini file (if exist)
        """
        logger.info("Starting Drain3 template miner")

        if config is None:
            logger.info(f"Loading configuration from {config_filename}")
            config = TemplateMinerConfig()
            config.load(config_filename)

        self.config = config

        self.profiler: Profiler = NullProfiler()

        if self.config.profiling_enabled:
            self.profiler = SimpleProfiler()

        self.persistence_handler = persistence_handler

        param_str = f"{self.config.mask_prefix}*{self.config.mask_suffix}"

        # Follow the configuration in the configuration file to instantiate Drain
        # target_obj will be "Drain" if the engine argument is not specified.
        target_obj = self.config.engine
        if target_obj not in ["Drain", "JaccardDrain"]:
            raise ValueError(f"Invalid matched_pattern: {target_obj}, must be either 'Drain' or 'JaccardDrain'")

        self.drain: DrainBase = globals()[target_obj](
            sim_th=self.config.drain_sim_th,
            depth=self.config.drain_depth,
            max_children=self.config.drain_max_children,
            max_clusters=self.config.drain_max_clusters,
            extra_delimiters=self.config.drain_extra_delimiters,
            profiler=self.profiler,
            param_str=param_str,
            parametrize_numeric_tokens=self.config.parametrize_numeric_tokens
        )

        self.masker = LogMasker(self.config.masking_instructions, self.config.mask_prefix, self.config.mask_suffix)
        self.parameter_extraction_cache: MutableMapping[Tuple[str, bool], str] = \
            LRUCache(self.config.parameter_extraction_cache_capacity)
        self.last_save_time = time.time()

        # Incremental ("lightweight") checkpoint mode: only changed clusters are
        # journaled instead of rewriting the whole Drain state on every save.
        self.incremental_persistence = isinstance(persistence_handler, IncrementalFilePersistence)
        self._watermark: Optional[int] = None
        self._last_processed_watermark: Optional[int] = None
        self._checkpoint_seq = 0
        # Clusters whose in-memory state is newer than the last checkpoint.
        # A "none" match still grows a cluster, so its size must be journaled
        # by the next checkpoint even if that checkpoint is triggered elsewhere.
        self._dirty_cluster_ids: set = set()

        if persistence_handler is not None:
            self.load_state()

    def load_state(self) -> None:
        logger.info("Checking for saved state")

        assert self.persistence_handler is not None

        state = self.persistence_handler.load_state()
        if state is not None:
            self._load_snapshot(state)

        if self.incremental_persistence:
            self._replay_journal()
            return

        if state is None:
            logger.info("Saved state not found")

    def _load_snapshot(self, state: bytes) -> None:
        if self.config.snapshot_compress_state:
            state = zlib.decompress(base64.b64decode(state))

        loaded_drain: Drain = jsonpickle.loads(state, keys=True)

        # json-pickle encoded keys as string by default, so we have to convert those back to int
        # this is only relevant for backwards compatibility when loading a snapshot of drain <= v0.9.1
        # which did not use json-pickle's keys=true
        if len(loaded_drain.id_to_cluster) > 0 and isinstance(next(iter(loaded_drain.id_to_cluster.keys())), str):
            loaded_drain.id_to_cluster = {int(k): v for k, v in list(loaded_drain.id_to_cluster.items())}
            if self.config.drain_max_clusters:
                cache: MutableMapping[int, Optional[LogCluster]] = LRUCache(maxsize=self.config.drain_max_clusters)
                cache.update(loaded_drain.id_to_cluster)
                loaded_drain.id_to_cluster = cache

        self.drain.id_to_cluster = loaded_drain.id_to_cluster
        self.drain.clusters_counter = loaded_drain.clusters_counter
        self.drain.root_node = loaded_drain.root_node

        logger.info(f"Restored {len(loaded_drain.clusters)} clusters "
                    f"built from {loaded_drain.get_total_cluster_size()} messages")

    def _replay_journal(self) -> None:
        """Apply every durable incremental checkpoint on top of the base snapshot.

        Replay mirrors what live training does to the Drain state:

        * a newly created cluster is inserted into the prefix tree exactly as in
          :meth:`Drain.add_log_message`;
        * a template/size change only replaces the cluster object, because Drain
          itself never moves an existing cluster in the tree;
        * an LRU-evicted cluster is dropped from ``id_to_cluster`` but its
          (now-stale) tree leaf entry is kept, matching the live engine.
        """
        assert isinstance(self.persistence_handler, IncrementalFilePersistence)

        # The base snapshot may itself carry a watermark (e.g. right after a
        # compaction folded the journal away).
        self._watermark = self.persistence_handler.base_watermark
        tree_cluster_ids = set(self.drain.id_to_cluster.keys())

        for record in self.persistence_handler.journal_records:
            for cluster_data in record["clusters"]:
                cluster_id = cluster_data["cluster_id"]
                cluster = LogCluster(cluster_data["log_template_tokens"], cluster_id)
                cluster.size = cluster_data["size"]
                is_new_cluster = cluster_id not in tree_cluster_ids
                self.drain.id_to_cluster[cluster_id] = cluster
                if is_new_cluster:
                    self.drain.add_seq_to_prefix_tree(self.drain.root_node, cluster)
                    tree_cluster_ids.add(cluster_id)
                self.drain.clusters_counter = max(self.drain.clusters_counter, cluster_id)
            for cluster_id in record.get("deleted_cluster_ids", []):
                self.drain.id_to_cluster.pop(cluster_id, None)
            self._checkpoint_seq = max(self._checkpoint_seq, record["seq"])
            self._watermark = record["watermark"]

        if self._checkpoint_seq:
            logger.info(
                "Replayed %d incremental checkpoint(s), watermark=%s, %d clusters",
                self._checkpoint_seq, self._watermark, len(self.drain.clusters),
            )

    def save_state(self, snapshot_reason: str) -> None:
        assert self.persistence_handler is not None

        state = jsonpickle.dumps(self.drain, keys=True).encode('utf-8')
        if self.config.snapshot_compress_state:
            state = base64.b64encode(zlib.compress(state))

        logger.info(f"Saving state of {len(self.drain.clusters)} clusters "
                    f"with {self.drain.get_total_cluster_size()} messages, {len(state)} bytes, "
                    f"reason: {snapshot_reason}")
        self.persistence_handler.save_state(state)

    def get_watermark(self) -> Optional[int]:
        """Log position covered by the last durable checkpoint.

        The collector should continue reading the original log strictly after
        this position (e.g. line number / offset). ``None`` before any
        checkpoint exists.
        """
        return self._watermark

    def _encode_cluster(self, cluster: LogCluster) -> Mapping[str, Union[int, Sequence[str]]]:
        return {
            "cluster_id": cluster.cluster_id,
            "log_template_tokens": list(cluster.log_template_tokens),
            "size": cluster.size,
        }

    def save_incremental_checkpoint(self,
                                    watermark: int,
                                    changed_cluster_ids: Sequence[int],
                                    deleted_cluster_ids: Sequence[int]) -> None:
        """Persist only the clusters changed since the previous checkpoint.

        The journal record's size is proportional to the actual changes rather
        than to the size of the whole template library.  ``watermark`` records
        how far the collector has fully processed the original log.
        """
        assert isinstance(self.persistence_handler, IncrementalFilePersistence)

        clusters = []
        for cluster_id in changed_cluster_ids:
            cluster = self.drain.id_to_cluster.get(cluster_id)
            if cluster is not None:
                clusters.append(self._encode_cluster(cluster))

        self._checkpoint_seq += 1
        record = {
            "seq": self._checkpoint_seq,
            "watermark": watermark,
            "clusters": clusters,
            "deleted_cluster_ids": list(deleted_cluster_ids),
        }
        self.persistence_handler.append_checkpoint(record)
        self._watermark = watermark
        logger.info(
            "Saved incremental checkpoint seq=%d watermark=%d (%d changed, %d deleted clusters)",
            self._checkpoint_seq, watermark, len(clusters), len(deleted_cluster_ids),
        )

    def begin_checkpoint_compaction(self, watermark: int) -> None:
        """Stage a full snapshot folding every checkpoint up to ``watermark``.

        The staged snapshot is not live until :meth:`commit_checkpoint_compaction`
        runs, so new messages can keep producing journal checkpoints meanwhile.
        """
        assert isinstance(self.persistence_handler, IncrementalFilePersistence)

        state = jsonpickle.dumps(self.drain, keys=True).encode('utf-8')
        if self.config.snapshot_compress_state:
            state = base64.b64encode(zlib.compress(state))

        logger.info(
            "Staging checkpoint compaction at watermark=%d (%d clusters, %d checkpoint(s) to fold)",
            watermark, len(self.drain.clusters), self._checkpoint_seq,
        )
        self.persistence_handler.begin_compaction(state, watermark)

    def commit_checkpoint_compaction(self, watermark: int) -> None:
        """Switch to the staged snapshot, rebasing newer checkpoints onto it.

        Journal checkpoints with a watermark greater than the compaction point
        (saved while compaction was in flight) are retained on top of the new
        base, so a restart after the switch still resumes at the newest
        checkpoint rather than replaying the whole history.
        """
        assert isinstance(self.persistence_handler, IncrementalFilePersistence)

        retained_records = self.persistence_handler.commit_compaction(watermark)
        self._checkpoint_seq = max((record["seq"] for record in retained_records), default=0)
        self._watermark = retained_records[-1]["watermark"] if retained_records else watermark
        # Flush changes processed after the staged snapshot that never triggered
        # a checkpoint of their own (e.g. a "none" match while compaction was
        # in flight), so the new base never hides an un-durable cluster state.
        if self._dirty_cluster_ids and self._last_processed_watermark is not None:
            self.save_incremental_checkpoint(
                self._last_processed_watermark, sorted(self._dirty_cluster_ids), []
            )
            self._watermark = self._last_processed_watermark
        self._dirty_cluster_ids.clear()
        logger.info(
            "Checkpoint compaction switched at watermark=%d, %d newer checkpoint(s) retained, "
            "resume watermark=%s",
            watermark, len(retained_records), self._watermark,
        )

    def compact_checkpoint(self, watermark: int) -> None:
        """Fold all historical checkpoints into a fresh full snapshot.

        Convenience wrapper around :meth:`begin_checkpoint_compaction` and
        :meth:`commit_checkpoint_compaction`.  Afterwards checkpoints up to
        ``watermark`` no longer have to be replayed on restart.
        """
        self.begin_checkpoint_compaction(watermark)
        self.commit_checkpoint_compaction(watermark)

    def get_snapshot_reason(self, change_type: str, cluster_id: int) -> Optional[str]:
        if change_type != "none":
            return f"{change_type} ({cluster_id})"

        diff_time_sec = time.time() - self.last_save_time
        if diff_time_sec >= self.config.snapshot_interval_minutes * 60:
            return "periodic"

        return None

    def add_log_message(self, log_message: str, watermark: Optional[int] = None) -> Mapping[str, Union[str, int]]:
        self.profiler.start_section("total")

        if self.incremental_persistence and watermark is None:
            self.profiler.end_section("total")
            raise ValueError(
                "watermark (position of the processed log message) is required for "
                "incremental checkpoints so the collector knows where to resume after a restart"
            )

        # LRU eviction only exists when the number of clusters is bounded; only
        # pay for the id snapshot in that case, so incremental saves stay
        # proportional to the actual changes.
        before_cluster_ids: Optional[set] = None
        if self.incremental_persistence and self.config.drain_max_clusters is not None:
            before_cluster_ids = set(self.drain.id_to_cluster.keys())

        self.profiler.start_section("mask")
        masked_content = self.masker.mask(log_message)
        self.profiler.end_section()

        self.profiler.start_section("drain")
        cluster, change_type = self.drain.add_log_message(masked_content)
        self.profiler.end_section("drain")
        result: Mapping[str, Union[str, int]] = {
            "change_type": change_type,
            "cluster_id": cluster.cluster_id,
            "cluster_size": cluster.size,
            "template_mined": cluster.get_template(),
            "cluster_count": len(self.drain.clusters)
        }

        if self.incremental_persistence:
            self._last_processed_watermark = watermark
            # The touched cluster changed even when the template did not,
            # because its size grew.
            self._dirty_cluster_ids.add(cluster.cluster_id)

        if self.persistence_handler is not None:
            self.profiler.start_section("save_state")
            snapshot_reason = self.get_snapshot_reason(change_type, cluster.cluster_id)
            if snapshot_reason:
                if self.incremental_persistence:
                    deleted_cluster_ids = []
                    if before_cluster_ids is not None:
                        deleted_cluster_ids = list(
                            before_cluster_ids - set(self.drain.id_to_cluster.keys())
                        )
                    self.save_incremental_checkpoint(
                        watermark, sorted(self._dirty_cluster_ids), deleted_cluster_ids
                    )
                    self._dirty_cluster_ids.clear()
                else:
                    self.save_state(snapshot_reason)
                self.last_save_time = time.time()
            self.profiler.end_section()

        self.profiler.end_section("total")
        self.profiler.report(self.config.profiling_report_sec)
        return result

    def match(self, log_message: str, full_search_strategy: str = "never") -> Optional[LogCluster]:
        """
        Mask log message and match against an already existing cluster.
        Match shall be perfect (sim_th=1.0).
        New cluster will not be created as a result of this call, nor any cluster modifications.

        :param log_message: log message to match
        :param full_search_strategy: when to perform full cluster search.
            (1) "never" is the fastest, will always perform a tree search [O(log(n)] but might produce
            false negatives (wrong mismatches) on some edge cases;
            (2) "fallback" will perform a linear search [O(n)] among all clusters with the same token count, but only in
            case tree search found no match.
            It should not have false negatives, however tree-search may find a non-optimal match with
            more wildcard parameters than necessary;
            (3) "always" is the slowest. It will select the best match among all known clusters, by always evaluating
            all clusters with the same token count, and selecting the cluster with perfect all token match and least
            count of wildcard matches.
        :return: Matched cluster or None if no match found.
        """

        masked_content = self.masker.mask(log_message)
        matched_cluster = self.drain.match(masked_content, full_search_strategy)
        return matched_cluster

    def get_parameter_list(self, log_template: str, log_message: str) -> Sequence[str]:
        """
        Extract parameters from a log message according to a provided template that was generated
        by calling `add_log_message()`.

        This function is deprecated. Please use extract_parameters instead.

        :param log_template: log template corresponding to the log message
        :param log_message: log message to extract parameters from
        :return: An ordered list of parameter values present in the log message.
        """

        extracted_parameters = self.extract_parameters(log_template, log_message, exact_matching=False)
        if not extracted_parameters:
            return []
        return [parameter.value for parameter in extracted_parameters]

    def extract_parameters(self,
                           log_template: str,
                           log_message: str,
                           exact_matching: bool = True) -> Optional[Sequence[ExtractedParameter]]:
        """
        Extract parameters from a log message according to a provided template that was generated
        by calling `add_log_message()`.

        For most accurate results, it is recommended that
        - Each `MaskingInstruction` has a unique `mask_with` value,
        - No `MaskingInstruction` has a `mask_with` value of `*`,
        - The regex-patterns of `MaskingInstruction` do not use unnamed back-references;
          instead use back-references to named groups e.g. `(?P=some-name)`.

        :param log_template: log template corresponding to the log message
        :param log_message: log message to extract parameters from
        :param exact_matching: whether to apply the correct masking-patterns to match parameters, or try to approximate;
            disabling exact_matching may be faster but may lead to situations in which parameters
            are wrongly identified.
        :return: A ordered list of ExtractedParameter for the log message
            or None if log_message does not correspond to log_template.
        """

        for delimiter in self.config.drain_extra_delimiters:
            log_message = re.sub(delimiter, " ", log_message)

        template_regex, param_group_name_to_mask_name = self._get_template_parameter_extraction_regex(
            log_template, exact_matching)

        # Parameters are represented by specific named groups inside template_regex.
        parameter_match = re.match(template_regex, log_message)

        # log template does not match template
        if not parameter_match:
            return None

        # create list of extracted parameters
        extracted_parameters = []
        for group_name, parameter in parameter_match.groupdict().items():
            if group_name in param_group_name_to_mask_name:
                mask_name = param_group_name_to_mask_name[group_name]
                extracted_parameter = ExtractedParameter(parameter, mask_name)
                extracted_parameters.append(extracted_parameter)

        return extracted_parameters

    @cachedmethod(lambda self: self.parameter_extraction_cache)
    def _get_template_parameter_extraction_regex(self,
                                                 log_template: str,
                                                 exact_matching: bool) -> Tuple[str, Mapping[str, str]]:
        param_group_name_to_mask_name = {}
        param_name_counter = [0]

        def get_next_param_name() -> str:
            param_group_name = f"p_{str(param_name_counter[0])}"
            param_name_counter[0] += 1
            return param_group_name

        # Create a named group with the respective patterns for the given mask-name.
        def create_capture_regex(_mask_name: str) -> str:
            allowed_patterns = []
            if exact_matching:
                # get all possible regex patterns from masking instructions that match this mask name
                masking_instructions = self.masker.instructions_by_mask_name(_mask_name)
                for mi in masking_instructions:
                    # MaskingInstruction may already contain named groups.
                    # We replace group names in those named groups, to avoid conflicts due to duplicate names.
                    if hasattr(mi, 'regex') and hasattr(mi, 'pattern'):
                        mi_groups = mi.regex.groupindex.keys()
                        pattern: str = mi.pattern
                    else:
                        # non regex masking instructions - support only non-exact matching
                        mi_groups = []
                        pattern = ".+?"

                    for group_name in mi_groups:
                        param_group_name = get_next_param_name()

                        def replace_captured_param_name(param_pattern: str) -> str:
                            _search_str = param_pattern.format(group_name)
                            _replace_str = param_pattern.format(param_group_name)
                            return pattern.replace(_search_str, _replace_str)

                        pattern = replace_captured_param_name("(?P={}")
                        pattern = replace_captured_param_name("(?P<{}>")

                    # support unnamed back-references in masks (simple cases only)
                    pattern = re.sub(r"\\(?!0)\d{1,2}", r"(?:.+?)", pattern)
                    allowed_patterns.append(pattern)

            if not exact_matching or _mask_name == "*":
                allowed_patterns.append(r".+?")

            # Give each capture group a unique name to avoid conflicts.
            param_group_name = get_next_param_name()
            param_group_name_to_mask_name[param_group_name] = _mask_name
            joined_patterns = "|".join(allowed_patterns)
            capture_regex = f"(?P<{param_group_name}>{joined_patterns})"
            return capture_regex

        # For every mask in the template, replace it with a named group of all
        # possible masking-patterns it could represent (in order).
        mask_names = set(self.masker.mask_names)

        # the Drain catch-all mask
        mask_names.add("*")

        escaped_prefix = re.escape(self.masker.mask_prefix)
        escaped_suffix = re.escape(self.masker.mask_suffix)
        template_regex = re.escape(log_template)

        # replace each mask name with a proper regex that captures it
        for mask_name in mask_names:
            search_str = escaped_prefix + re.escape(mask_name) + escaped_suffix
            while True:
                rep_str = create_capture_regex(mask_name)
                # Replace one-by-one to get a new param group name for each replacement.
                template_regex_new = template_regex.replace(search_str, rep_str, 1)
                # Break when all replaces for this mask are done.
                if template_regex_new == template_regex:
                    break
                template_regex = template_regex_new

        # match also messages with multiple spaces or other whitespace chars between tokens
        template_regex = re.sub(r"\\ ", r"\\s+", template_regex)
        template_regex = f"^{template_regex}$"
        return template_regex, param_group_name_to_mask_name
