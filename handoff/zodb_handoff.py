#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from persistent import Persistent
from persistent.mapping import PersistentMapping
from ZODB.Connection import TransactionMetaData
from ZODB.FileStorage import FileIterator, FileStorage
from ZODB.TimeStamp import TimeStamp
from ZODB._compat import PersistentUnpickler
from ZODB.blob import FilesystemHelper, is_blob_record
from ZODB.serialize import referencesf
from ZODB.utils import get_pickle_metadata, p64


SCHEMA = "zodb-incremental-handoff/1"
ISSUE_STATUSES = {"missing_version", "missing_blob", "unparseable", "deleted_reference"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(canonical_json(value))
    os.replace(temporary, path)


def write_bytes_immutable(path: Path, data: bytes) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite immutable artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o444)
    except OSError:
        pass


def parse_oid(value) -> bytes:
    if isinstance(value, bytes):
        return value
    text = str(value).strip()
    if text.startswith("0x"):
        text = text[2:]
    return bytes.fromhex(text.zfill(16))


def oid_hex(value: bytes) -> str:
    return value.hex()


def normalize_tid(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.hex()
    text = str(value).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    return text.zfill(16)


def next_tid(tid: bytes) -> bytes:
    stamp = TimeStamp(tid)
    return stamp.laterThan(stamp).raw()


class HandoffError(Exception):
    pass


class MissingRevision(HandoffError):
    def __init__(self, issue: dict):
        super().__init__(issue.get("message", "missing revision"))
        self.issue = issue


class Ref:
    __slots__ = ("oid",)

    def __init__(self, oid: bytes):
        self.oid = oid


class SafeStub:
    def __new__(cls, *args):
        instance = object.__new__(cls)
        instance.init_args = args
        instance.state = None
        return instance

    def __setstate__(self, state):
        self.state = state


def persistent_reference(reference):
    if isinstance(reference, Ref):
        return reference
    if isinstance(reference, bytes):
        return Ref(reference)
    if isinstance(reference, str):
        return Ref(reference.encode("ascii"))
    if isinstance(reference, tuple):
        if reference and isinstance(reference[0], bytes):
            return Ref(reference[0])
        if reference and isinstance(reference[0], str):
            return Ref(reference[0].encode("ascii"))
    if isinstance(reference, list) and reference:
        for item in reference[1:] if reference and reference[0] in {"m", "n", "w"} else reference:
            if isinstance(item, (bytes, str)) and len(item) in {8, 16}:
                value = item.encode("ascii") if isinstance(item, str) else item
                if len(value) == 8:
                    return Ref(value)
            if isinstance(item, tuple):
                found = persistent_reference(item)
                if found is not None:
                    return found
    return None


def safe_load_state(data: bytes):
    unpickler = PersistentUnpickler(
        lambda module, name: SafeStub,
        persistent_reference,
        __import__("io").BytesIO(data),
    )
    unpickler.noload()
    return unpickler.load()


def lookup_root_value(state, root: dict):
    current = state
    key = root.get("key")
    path = list(root.get("path", []))
    tokens = ([key] if key is not None else []) + path
    for token in tokens:
        if isinstance(current, SafeStub):
            current = current.state
        if isinstance(current, dict):
            if token in current:
                current = current[token]
                continue
            data = current.get("data")
            if isinstance(data, dict) and token in data:
                current = data[token]
                continue
        try:
            current = getattr(current, token)
        except AttributeError as exc:
            raise KeyError(token) from exc
    if isinstance(current, Ref):
        return current.oid
    if isinstance(current, bytes) and len(current) == 8:
        return current
    raise HandoffError(f"root {root.get('label')!r} did not resolve to an OID")


class Source:
    def __init__(self, config: dict, order_dir: Path):
        self.name = config["name"]
        self.fs_path = self._resolve(config["fs_path"], order_dir)
        self.blob_dir = self._resolve(config.get("blob_dir"), order_dir)
        self.verified = bool(config.get("verified", False))
        self.borrow = bool(config.get("borrow", config.get("role") == "archive"))
        self.role = config.get("role", "primary" if config.get("primary") else "source")
        self.storage = None
        self.helper = None
        if self.blob_dir:
            layout = config.get("blob_layout", "automatic")
            self.helper = FilesystemHelper(str(self.blob_dir), layout)

    @staticmethod
    def _resolve(value, order_dir: Path):
        if not value:
            return None
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = order_dir / path
        return path.resolve()

    def open_storage(self) -> FileStorage:
        if self.storage is None:
            if not self.fs_path.exists():
                raise HandoffError(f"FileStorage not found for source {self.name}: {self.fs_path}")
            self.storage = FileStorage(str(self.fs_path), read_only=True)
        return self.storage

    def close(self):
        if self.storage is not None:
            self.storage.close()
            self.storage = None

    def blob_filename(self, oid: bytes, tid: bytes) -> Path | None:
        if self.helper is None:
            return None
        return Path(self.helper.getBlobFilename(oid, tid))


class Catalog:
    def __init__(self, sources: list[Source]):
        self.sources = sources
        self.transactions: dict[str, dict] = {}
        self.versions: dict[tuple[str, str], dict] = {}
        self.oid_tids: dict[str, list[str]] = defaultdict(list)
        self.source_errors: list[dict] = []

    def scan(self):
        for source in self.sources:
            self._scan_source(source)
        for oid, tids in self.oid_tids.items():
            self.oid_tids[oid] = sorted(set(tids))
        return self

    def _scan_source(self, source: Source):
        if not source.fs_path.exists():
            self.source_errors.append(
                {"source": source.name, "code": "FS_NOT_FOUND", "location": str(source.fs_path)}
            )
            return
        try:
            iterator = FileIterator(str(source.fs_path))
        except Exception as exc:
            self.source_errors.append(
                {"source": source.name, "code": "FS_OPEN_FAILED", "location": str(source.fs_path),
                 "message": str(exc)}
            )
            return
        try:
            for transaction in iterator:
                tid = oid_hex(transaction.tid)
                txn = self.transactions.setdefault(tid, {
                    "tid": tid,
                    "user_hex": bytes(transaction.user).hex(),
                    "description_hex": bytes(transaction.description).hex(),
                    "extension_hex": bytes(transaction.extension).hex(),
                    "description_text": bytes(transaction.description).decode("utf-8", "replace"),
                    "sources": {},
                })
                record_digests = []
                records = iter(transaction)
                while True:
                    try:
                        record = next(records)
                    except StopIteration:
                        break
                    except Exception as exc:
                        self.source_errors.append({
                            "source": source.name, "tid": tid,
                            "code": "RECORD_ITERATION_FAILED", "message": str(exc),
                        })
                        break
                    oid = oid_hex(record.oid)
                    record_tid = oid_hex(record.tid)
                    key = (oid, record_tid)
                    data_hash = None
                    size = None
                    deletion = record.data is None
                    if isinstance(record.data, bytes):
                        data_hash = sha256_bytes(record.data)
                        size = len(record.data)
                    data_txn = oid_hex(record.data_txn) if record.data_txn else None
                    location = {
                        "source": source.name,
                        "fs_path": str(source.fs_path),
                        "record_offset": getattr(record, "pos", None),
                        "data_txn": data_txn,
                        "deletion": deletion,
                        "data_sha256": data_hash,
                        "size": size,
                    }
                    version = self.versions.setdefault(key, {
                        "oid": oid,
                        "tid": record_tid,
                        "data_sha256_values": set(),
                        "locations": [],
                    })
                    if data_hash:
                        version["data_sha256_values"].add(data_hash)
                    version["locations"].append(location)
                    self.oid_tids[oid].append(record_tid)
                    record_digests.append([oid, record_tid, data_hash or ("deleted" if deletion else "unresolved"), data_txn])
                txn["sources"][source.name] = {
                    "history_digest": sha256_bytes(canonical_json(record_digests)),
                    "record_count": len(record_digests),
                }
        finally:
            iterator.close()

    def latest_revision(self, oid: bytes, checkpoint_tid: str):
        oid_value = oid_hex(oid)
        candidates = self.oid_tids.get(oid_value, [])
        eligible = [tid for tid in candidates if tid <= checkpoint_tid]
        if not eligible:
            return None
        revision_tid = max(eligible)
        return self.versions[(oid_value, revision_tid)]

    def payload(self, version: dict):
        oid = parse_oid(version["oid"])
        tid = parse_oid(version["tid"])
        hashes = version.get("data_sha256_values", set())
        expected_hash = next(iter(hashes)) if len(hashes) == 1 else None
        if len(hashes) > 1:
            raise MissingRevision({
                "code": "VERSION_HASH_CONFLICT",
                "oid": version["oid"], "rev_tid": version["tid"],
                "message": "sources disagree on logical payload for same OID/TID",
                "locations": version["locations"],
            })

        errors = []
        for location in version["locations"]:
            source = self.source_by_name(location["source"])
            try:
                data = source.open_storage().loadSerial(oid, tid)
                actual_hash = sha256_bytes(data)
                if expected_hash and actual_hash != expected_hash:
                    errors.append({**location, "error": "hash mismatch after loadSerial"})
                    continue
                return data, {
                    "source": source.name,
                    "fs_path": str(source.fs_path),
                    "payload_sha256": actual_hash,
                    "record_offset": location.get("record_offset"),
                    "data_txn": location.get("data_txn"),
                    "borrowed": source.role == "archive" or source.borrow,
                }
            except Exception as exc:
                errors.append({**location, "error": f"{type(exc).__name__}: {exc}"})

        origin_tid = None
        for location in version["locations"]:
            if location.get("data_txn"):
                origin_tid = location["data_txn"]
                break
        if origin_tid:
            origin = self.versions.get((version["oid"], origin_tid))
            if origin:
                for location in origin["locations"]:
                    source = self.source_by_name(location["source"])
                    if not source.verified or not source.borrow:
                        continue
                    try:
                        data, provenance = self.payload(origin)
                        if expected_hash and sha256_bytes(data) != expected_hash:
                            continue
                        provenance.update({
                            "borrowed": True,
                            "borrow_reason": "exact backpointer/data_txn version",
                            "requested_rev_tid": version["tid"],
                            "byte_origin_tid": origin_tid,
                        })
                        return data, provenance
                    except MissingRevision:
                        pass

        searched = []
        for source in self.sources:
            searched.append({"source": source.name, "fs_path": str(source.fs_path),
                            "load": f"FileStorage.loadSerial({version['oid']}, {version['tid']})"})
        raise MissingRevision({
            "code": "MISSING_VERSION",
            "oid": version["oid"], "rev_tid": version["tid"],
            "data_txn": origin_tid,
            "message": "no exact OID/TID payload is readable; current value was not used",
            "original_locations": version["locations"],
            "load_errors": errors,
            "searched": searched,
        })

    def source_by_name(self, name: str) -> Source:
        for source in self.sources:
            if source.name == name:
                return source
        raise HandoffError(f"unknown source {name}")

    def find_blob(self, oid: bytes, revision_tid: str, origin_tid: str | None = None):
        serials = []
        for value in [revision_tid, origin_tid]:
            if value and value not in serials:
                serials.append(value)
        searched = []
        matches = []
        for serial_hex in serials:
            serial = parse_oid(serial_hex)
            for source in self.sources:
                path = source.blob_filename(oid, serial)
                if path is None:
                    continue
                searched.append({"source": source.name, "path": str(path)})
                if path.is_file():
                    matches.append({
                        "source": source.name,
                        "path": str(path),
                        "sha256": sha256_file(path),
                        "size": path.stat().st_size,
                        "lookup_tid": serial_hex,
                        "borrowed": source.role == "archive" or source.borrow or serial_hex != revision_tid,
                    })
        if not matches:
            return None, searched, []
        hashes = {item["sha256"] for item in matches}
        if len(hashes) > 1:
            return None, searched, [{
                "code": "BLOB_HASH_CONFLICT",
                "oid": oid_hex(oid),
                "rev_tid": revision_tid,
                "message": "verified sources disagree on blob bytes for exact OID/TID",
                "locations": matches,
            }]
        result = dict(matches[0])
        result["replica_locations"] = [dict(item) for item in matches]
        return result, searched, []


class Extractor:
    def __init__(self, order: dict, order_path: Path):
        self.order = order
        self.order_path = order_path
        order_dir = order_path.parent.resolve()
        self.sources = [Source(item, order_dir) for item in order.get("sources", [])]
        if not self.sources:
            raise HandoffError("order.sources must contain at least one FileStorage source")
        self.catalog = Catalog(self.sources).scan()
        self.payload_cache: dict[tuple[str, str], tuple[bytes, dict]] = {}

    def close(self):
        for source in self.sources:
            source.close()

    def transaction_tids(self, through: str | None = None):
        range_config = self.order.get("range", {})
        start = normalize_tid(range_config.get("start_tid"))
        end = normalize_tid(through or range_config.get("through_tid"))
        tids = sorted(self.catalog.transactions)
        if start:
            tids = [tid for tid in tids if tid >= start]
        if end:
            tids = [tid for tid in tids if tid <= end]
        return tids

    def expected_transaction_tids(self, through: str | None = None):
        range_config = self.order.get("range", {})
        explicit = range_config.get("tids") or self.order.get("sealed_tids")
        if explicit is None:
            explicit = self.order.get("common_history_tids")
        if not explicit:
            return self.transaction_tids(through)
        start = normalize_tid(range_config.get("start_tid"))
        end = normalize_tid(through or range_config.get("through_tid"))
        tids = sorted(set(normalize_tid(tid) for tid in explicit))
        if start:
            tids = [tid for tid in tids if tid >= start]
        if end:
            tids = [tid for tid in tids if tid <= end]
        return tids

    def common_history_report(self):
        expected = self.order.get("common_history_tids")
        report = {"verified": True, "tids": [], "problems": []}
        if expected:
            for raw_tid in expected:
                tid = normalize_tid(raw_tid)
                report["tids"].append(tid)
                txn = self.catalog.transactions.get(tid)
                if txn is None:
                    report["verified"] = False
                    report["problems"].append({"tid": tid, "code": "TXN_NOT_FOUND"})
                    continue
                verified_sources = [s.name for s in self.sources if s.verified]
                missing = [name for name in verified_sources if name not in txn["sources"]]
                if missing:
                    report["verified"] = False
                    report["problems"].append({"tid": tid, "code": "TXN_MISSING_FROM_SOURCE",
                                               "sources": missing})
        return report

    def resolve_root(self, root: dict, checkpoint_tid: str, issues: list[dict]):
        label = root.get("label") or root.get("key") or root.get("oid")
        if root.get("oid"):
            oid = parse_oid(root["oid"])
            return oid, oid_hex(oid), None
        root_oid = parse_oid(root.get("root_oid", "0x00"))
        version = self.catalog.latest_revision(root_oid, checkpoint_tid)
        if version is None:
            issue = {
                "code": "MISSING_ROOT_VERSION", "root_label": label,
                "checkpoint_tid": checkpoint_tid, "oid": oid_hex(root_oid),
                "message": "ZODB root record has no version at or before checkpoint",
                "searched": [str(s.fs_path) for s in self.sources],
            }
            issues.append(issue)
            return None, None, issue
        try:
            data, provenance = self.get_payload(version)
            value = lookup_root_value(safe_load_state(data), root)
            return value, oid_hex(root_oid), None
        except MissingRevision as missing:
            issue = {**missing.issue, "root_label": label, "checkpoint_tid": checkpoint_tid}
            issues.append(issue)
            return None, oid_hex(root_oid), issue
        except KeyError:
            return None, oid_hex(root_oid), None
        except Exception as exc:
            issue = {
                "code": "ROOT_DECODE_FAILED", "root_label": label,
                "checkpoint_tid": checkpoint_tid, "oid": oid_hex(root_oid),
                "message": f"{type(exc).__name__}: {exc}",
            }
            issues.append(issue)
            return None, oid_hex(root_oid), issue

    def get_payload(self, version: dict):
        key = (version["oid"], version["tid"])
        if key not in self.payload_cache:
            self.payload_cache[key] = self.catalog.payload(version)
        return self.payload_cache[key]

    def build_checkpoint(self, root: dict, checkpoint_tid: str):
        label = root.get("label") or root.get("key") or root.get("oid")
        issues: list[dict] = []
        anchor, root_record_oid, root_issue = self.resolve_root(root, checkpoint_tid, issues)
        result = {
            "label": label,
            "spec": root,
            "checkpoint_tid": checkpoint_tid,
            "root_record_oid": root_record_oid,
            "anchor_oid": oid_hex(anchor) if anchor else None,
            "status": "absent",
            "objects": [],
            "edges": [],
            "issue_ids": [],
        }
        if anchor is None:
            if root_issue is not None:
                result["status"] = "incomplete"
            return result, issues

        seen = {oid_hex(anchor)}
        queue = [anchor]
        objects = {}
        edges = set()
        while queue:
            oid = queue.pop()
            oid_value = oid_hex(oid)
            version = self.catalog.latest_revision(oid, checkpoint_tid)
            if version is None:
                issues.append({
                    "code": "MISSING_VERSION", "root_label": label,
                    "checkpoint_tid": checkpoint_tid, "oid": oid_value,
                    "wanted_before_tid": checkpoint_tid,
                    "message": "referenced OID has no readable revision at or before checkpoint",
                    "known_tids": self.catalog.oid_tids.get(oid_value, []),
                    "searched": [{"source": s.name, "fs_path": str(s.fs_path)} for s in self.sources],
                })
                continue
            if version["locations"] and all(loc.get("deletion") for loc in version["locations"]) and len(version.get("data_sha256_values", set())) == 0:
                issues.append({
                    "code": "DELETED_REFERENCE", "root_label": label,
                    "checkpoint_tid": checkpoint_tid, "oid": oid_value,
                    "rev_tid": version["tid"],
                    "message": "reference reaches a deleted/undone object record",
                    "original_locations": version["locations"],
                })
                continue
            try:
                data, provenance = self.get_payload(version)
            except MissingRevision as missing:
                issues.append({**missing.issue, "root_label": label,
                               "checkpoint_tid": checkpoint_tid})
                continue

            try:
                ref_oids = referencesf(data)
            except Exception as exc:
                issues.append({
                    "code": "UNPARSEABLE_PICKLE", "root_label": label,
                    "checkpoint_tid": checkpoint_tid, "oid": oid_value,
                    "rev_tid": version["tid"], "message": str(exc),
                    "original_locations": version["locations"],
                })
                ref_oids = []

            class_name = None
            try:
                module_name, class_name_value = get_pickle_metadata(data)
                class_name = f"{module_name}.{class_name_value}"
            except Exception:
                class_name = None

            origin_tid = provenance.get("data_txn")
            blob_info, searched_blob_paths, blob_conflicts = self.catalog.find_blob(
                oid, version["tid"], provenance.get("byte_origin_tid") or origin_tid
            )
            blob_status = "not_expected"
            for conflict in blob_conflicts:
                issues.append({**conflict, "root_label": label,
                               "checkpoint_tid": checkpoint_tid})
            if blob_conflicts:
                blob_status = "conflict"
            elif is_blob_record(data) or blob_info is not None:
                if blob_info is None:
                    blob_status = "missing"
                    issues.append({
                        "code": "MISSING_BLOB", "root_label": label,
                        "checkpoint_tid": checkpoint_tid, "oid": oid_value,
                        "rev_tid": version["tid"], "data_txn": origin_tid,
                        "message": "blob record is reachable but no matching committed blob file exists",
                        "original_locations": version["locations"],
                        "searched_blob_paths": searched_blob_paths,
                    })
                else:
                    blob_status = "present"

            view = {
                "oid": oid_value,
                "rev_tid": version["tid"],
                "class_name": class_name,
                "size": len(data),
                "payload_sha256": provenance["payload_sha256"],
                "provenance": provenance,
                "reference_oids": sorted(oid_hex(ref) for ref in ref_oids),
                "blob_status": blob_status,
            }
            if blob_info:
                view["blob"] = blob_info
            objects[oid_value] = view

            for ref in ref_oids:
                ref_value = oid_hex(ref)
                edges.add((oid_value, version["tid"], ref_value))
                if ref_value not in seen:
                    seen.add(ref_value)
                    queue.append(ref)

        result["objects"] = [objects[key] for key in sorted(objects)]
        result["edges"] = [
            {"from_oid": source_oid, "from_rev_tid": rev, "to_oid": target_oid}
            for source_oid, rev, target_oid in sorted(edges)
        ]
        if any(issue.get("root_label") == label and issue.get("code") in
               {"MISSING_VERSION", "MISSING_BLOB", "BLOB_HASH_CONFLICT", "UNPARSEABLE_PICKLE",
                "MISSING_ROOT_VERSION"}
               for issue in issues):
            result["status"] = "incomplete"
        else:
            result["status"] = "verified"
        return result, issues


class AnchorRef(Persistent):
    def __getnewargs__(self):
        return ()


class SelfTestInspection(Persistent):
    def __init__(self, code, body, attachment):
        self.code = code
        self.body = body
        self.attachment = attachment


class RootJar:
    xrefs = True
    databases = {}

    def new_oid(self):
        return p64(99)

    def db(self):
        return self

    def register(self, obj):
        return None

    def get_connection(self, name):
        return None


def encode_root_binding(mapping: dict[str, str]) -> bytes:
    from ZODB.serialize import ObjectWriter

    root = PersistentMapping()
    jar = RootJar()
    root._p_oid = p64(0)
    root._p_jar = jar
    for key, oid_text in sorted(mapping.items()):
        ref = AnchorRef.__new__(AnchorRef)
        ref._p_oid = parse_oid(oid_text)
        ref._p_jar = jar
        root[key] = ref
    return ObjectWriter(root).serialize(root)


def store_paths(archive: Path, sha: str):
    return archive / "store" / "objects" / sha[:2] / sha[2:], archive / "store" / "blobs" / sha[:2] / sha[2:]


def write_payload_store(archive: Path, data: bytes) -> str:
    digest = sha256_bytes(data)
    path, _ = store_paths(archive, digest)
    if not path.exists():
        write_bytes_immutable(path, data)
    return digest


def write_blob_store_from_file(archive: Path, source_path: str, digest: str) -> str:
    _, path = store_paths(archive, digest)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        shutil.copyfile(source_path, temporary)
        os.replace(temporary, path)
    return digest


def load_archive(archive_dir: Path) -> dict:
    path = archive_dir / "archive.json"
    if not path.exists():
        raise HandoffError(f"archive not found: {path}")
    return json.loads(path.read_text("utf-8"))


def save_archive(archive_dir: Path, archive: dict) -> None:
    write_json(archive_dir / "archive.json", archive)


def receipt_hash(receipt: dict) -> str:
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    return sha256_bytes(canonical_json(unsigned))


def build_archive(order_path: Path, archive_dir: Path | None, through: str | None):
    order = json.loads(order_path.read_text("utf-8"))
    if archive_dir is None:
        output = Path(order.get("output_dir", "handoff_archive")).expanduser()
        if not output.is_absolute():
            output = order_path.parent / output
        archive_dir = output
    archive_dir = archive_dir.expanduser().resolve()
    archive_dir.mkdir(parents=True, exist_ok=True)
    write_json(archive_dir / "order.resolved.json", order)

    if (archive_dir / "archive.json").exists():
        archive = load_archive(archive_dir)
    else:
        archive = {
            "schema": SCHEMA,
            "case_id": order.get("case_id", archive_dir.name),
            "order_path": str(order_path.resolve()),
            "sources": order["sources"],
            "roots": order.get("roots", []),
            "range": order.get("range", {}),
            "common_history": None,
            "batches": [],
            "revisions": {},
            "root_bindings": {},
        }

    extractor = Extractor(order, order_path)
    archive["common_history"] = extractor.common_history_report()
    existing_tids = {item["tid"] for item in archive["batches"]}
    existing_count = len(archive["batches"])
    parent_receipt = archive["batches"][-1]["receipt_sha256"] if archive["batches"] else None

    for checkpoint_tid in extractor.expected_transaction_tids(through):
        if checkpoint_tid in existing_tids:
            continue
        seq = len(archive["batches"]) + 1
        batch_id = f"{seq:06d}-{checkpoint_tid}"
        all_issues = []
        checkpoints = []
        writes = []
        seen_writes = set()
        transaction_record = extractor.catalog.transactions.get(checkpoint_tid)

        if transaction_record is None:
            all_issues.append({
                "id": "ISS-0001",
                "code": "MISSING_TRANSACTION",
                "checkpoint_tid": checkpoint_tid,
                "message": "sealed TID is absent from every configured FileStorage source",
                "searched": [
                    {"source": source.name, "fs_path": str(source.fs_path),
                     "blob_dir": str(source.blob_dir) if source.blob_dir else None}
                    for source in extractor.sources
                ],
            })

        for root in order.get("roots", []) if transaction_record else []:
            checkpoint, issues = extractor.build_checkpoint(root, checkpoint_tid)
            for issue in issues:
                issue["id"] = f"ISS-{len(all_issues) + 1:04d}"
                all_issues.append(issue)
                checkpoint["issue_ids"].append(issue["id"])
            checkpoints.append(checkpoint)

            for obj in checkpoint["objects"]:
                rev_key = f"{obj['oid']}:{obj['rev_tid']}"
                provenance = obj["provenance"]
                version = extractor.catalog.versions[(obj["oid"], obj["rev_tid"])]
                data, _ = extractor.get_payload(version)
                payload_sha = write_payload_store(archive_dir, data)
                blob_sha = None
                blob_size = None
                if obj["blob_status"] == "present":
                    blob_sha = write_blob_store_from_file(
                        archive_dir, obj["blob"]["path"], obj["blob"]["sha256"]
                    )
                    blob_size = obj["blob"]["size"]
                existing_revision = archive["revisions"].get(rev_key)
                revision = archive["revisions"].setdefault(rev_key, {
                    "oid": obj["oid"],
                    "rev_tid": obj["rev_tid"],
                    "payload_sha256": payload_sha,
                    "size": obj["size"],
                    "first_batch": seq,
                    "provenance": provenance,
                    "blob_sha256": None,
                    "blob_size": None,
                    "blob_status": "missing",
                })
                if blob_sha:
                    revision["blob_sha256"] = blob_sha
                    revision["blob_size"] = blob_size
                    revision["blob_status"] = "present"
                elif obj["blob_status"] == "conflict":
                    revision["blob_status"] = "conflict"
                else:
                    revision["blob_status"] = "missing" if obj["blob_status"] == "missing" else "not_expected"

                if existing_revision is None:
                    writes.append({
                        "kind": "revision",
                        "rev_key": rev_key,
                        "oid": obj["oid"],
                        "serial_tid": obj["rev_tid"],
                        "payload_sha256": payload_sha,
                        "blob_sha256": revision.get("blob_sha256"),
                        "origin_tid": provenance.get("byte_origin_tid") or provenance.get("data_txn"),
                        "provenance": provenance,
                    })
                elif blob_sha and not existing_revision.get("blob_sha256"):
                    writes.append({
                        "kind": "blob",
                        "rev_key": rev_key,
                        "oid": obj["oid"],
                        "serial_tid": obj["rev_tid"],
                        "blob_sha256": blob_sha,
                    })

        bindings_by_root_record = defaultdict(dict)
        for checkpoint in checkpoints:
            spec = checkpoint["spec"]
            if not spec.get("key") or not checkpoint.get("anchor_oid"):
                continue
            root_oid = checkpoint.get("root_record_oid") or oid_hex(p64(0))
            bindings_by_root_record[root_oid][spec["key"]] = checkpoint["anchor_oid"]

        for root_oid, mapping in sorted(bindings_by_root_record.items()):
            mapping_hash = sha256_bytes(canonical_json(mapping))
            previous = archive.get("root_bindings", {}).get(root_oid)
            if previous and previous.get("mapping_sha256") == mapping_hash:
                continue
            data = encode_root_binding(mapping)
            payload_sha = write_payload_store(archive_dir, data)
            rev_key = f"{root_oid}:{checkpoint_tid}"
            writes.append({
                "kind": "root_binding",
                "rev_key": rev_key,
                "oid": root_oid,
                "serial_tid": checkpoint_tid,
                "payload_sha256": payload_sha,
                "mapping": mapping,
                "generated": True,
            })
            archive.setdefault("root_bindings", {})[root_oid] = {
                "mapping": mapping,
                "mapping_sha256": mapping_hash,
                "rev_tid": checkpoint_tid,
                "batch": seq,
            }
            archive["revisions"][rev_key] = {
                "oid": root_oid,
                "rev_tid": checkpoint_tid,
                "payload_sha256": payload_sha,
                "generated_root_binding": True,
                "first_batch": seq,
            }

        txn = transaction_record or {}
        batch = {
            "schema": SCHEMA,
            "seq": seq,
            "batch_id": batch_id,
            "tid": checkpoint_tid,
            "parent_receipt_sha256": parent_receipt,
            "missing_transaction": transaction_record is None,
            "transaction": {
                "tid": checkpoint_tid,
                "user_hex": txn.get("user_hex", ""),
                "description_hex": txn.get("description_hex", ""),
                "extension_hex": txn.get("extension_hex", ""),
                "description_text": txn.get("description_text", ""),
                "source_digests": txn.get("sources", {}),
            },
            "status": "partial" if all_issues else "verified",
            "checkpoints": checkpoints,
            "writes": writes,
            "issues": all_issues,
        }
        batch_bytes = canonical_json(batch)
        batch_path = archive_dir / "batches" / batch_id / "batch.json"
        write_bytes_immutable(batch_path, batch_bytes)
        receipt = {
            "schema": SCHEMA,
            "seq": seq,
            "batch_id": batch_id,
            "tid": checkpoint_tid,
            "batch_sha256": sha256_bytes(batch_bytes),
            "parent_receipt_sha256": parent_receipt,
            "status": batch["status"],
            "write_count": len(writes),
            "issue_count": len(all_issues),
            "root_status": {item["label"]: item["status"] for item in checkpoints},
        }
        receipt["receipt_sha256"] = receipt_hash(receipt)
        write_json(archive_dir / "batches" / batch_id / "receipt.json", receipt)
        archive["batches"].append({
            "seq": seq,
            "batch_id": batch_id,
            "tid": checkpoint_tid,
            "status": batch["status"],
            "receipt_sha256": receipt["receipt_sha256"],
            "path": str(batch_path.relative_to(archive_dir)),
        })
        parent_receipt = receipt["receipt_sha256"]
        save_archive(archive_dir, archive)

    extractor.close()
    return archive_dir


def read_batch(archive_dir: Path, batch_ref: dict):
    batch_path = archive_dir / batch_ref["path"]
    batch = json.loads(batch_path.read_text("utf-8"))
    receipt_path = batch_path.parent / "receipt.json"
    receipt = json.loads(receipt_path.read_text("utf-8"))
    return batch, receipt, batch_path


def verify_archive(archive_dir: Path, through: int | None = None):
    archive = load_archive(archive_dir)
    errors = []
    warnings = []
    batches = archive["batches"]
    if through is not None:
        batches = batches[:through]
    parent_hash = None
    for index, batch_ref in enumerate(batches, start=1):
        try:
            batch, receipt, batch_path = read_batch(archive_dir, batch_ref)
        except Exception as exc:
            errors.append({"batch": batch_ref.get("batch_id"), "code": "BATCH_READ_FAILED",
                           "message": str(exc)})
            continue
        batch_bytes = batch_path.read_bytes()
        if sha256_bytes(batch_bytes) != receipt.get("batch_sha256"):
            errors.append({"batch": batch["batch_id"], "code": "BATCH_HASH_MISMATCH"})
        expected_receipt_hash = receipt_hash(receipt)
        if expected_receipt_hash != receipt.get("receipt_sha256"):
            errors.append({"batch": batch["batch_id"], "code": "RECEIPT_HASH_MISMATCH"})
        if receipt.get("parent_receipt_sha256") != parent_hash:
            errors.append({"batch": batch["batch_id"], "code": "PARENT_CHAIN_MISMATCH"})
        parent_hash = receipt.get("receipt_sha256")
        if batch.get("seq") != index:
            errors.append({"batch": batch.get("batch_id"), "code": "SEQ_GAP",
                           "expected_seq": index, "actual_seq": batch.get("seq")})

        for write in batch.get("writes", []):
            if write.get("payload_sha256"):
                object_path, _ = store_paths(archive_dir, write["payload_sha256"])
                if not object_path.is_file() or sha256_file(object_path) != write["payload_sha256"]:
                    errors.append({"batch": batch["batch_id"], "code": "PAYLOAD_STORE_MISMATCH",
                                   "rev": write.get("rev_key")})
            if write.get("blob_sha256"):
                _, blob_path = store_paths(archive_dir, write["blob_sha256"])
                if not blob_path.is_file() or sha256_file(blob_path) != write["blob_sha256"]:
                    errors.append({"batch": batch["batch_id"], "code": "BLOB_STORE_MISMATCH",
                                   "rev": write.get("rev_key")})

        for checkpoint in batch.get("checkpoints", []):
            objects = {item["oid"]: item for item in checkpoint.get("objects", [])}
            for obj in checkpoint.get("objects", []):
                revision = archive.get("revisions", {}).get(f"{obj['oid']}:{obj['rev_tid']}")
                if revision is None:
                    errors.append({"batch": batch["batch_id"], "code": "MISSING_INDEXED_REVISION",
                                   "oid": obj["oid"], "rev": obj["rev_tid"]})
                    continue
                object_path, _ = store_paths(archive_dir, obj["payload_sha256"])
                if not object_path.is_file():
                    errors.append({"batch": batch["batch_id"], "code": "MISSING_PAYLOAD_FILE",
                                   "oid": obj["oid"], "rev": obj["rev_tid"]})
                if obj.get("blob_status") == "present":
                    blob_hash = obj["blob"]["sha256"]
                    _, blob_path = store_paths(archive_dir, blob_hash)
                    if not blob_path.is_file():
                        errors.append({"batch": batch["batch_id"], "code": "MISSING_BLOB_FILE",
                                       "oid": obj["oid"], "rev": obj["rev_tid"]})
            for edge in checkpoint.get("edges", []):
                if edge["to_oid"] not in objects:
                    issue_codes = {issue.get("code") for issue in batch.get("issues", [])}
                    expected_gap_codes = {
                        "MISSING_VERSION", "MISSING_ROOT_VERSION", "DELETED_REFERENCE",
                        "UNPARSEABLE_PICKLE",
                    }
                    if not (issue_codes & expected_gap_codes):
                        errors.append({"batch": batch["batch_id"], "code": "EDGE_TARGET_NOT_VISIBLE",
                                       "edge": edge, "root": checkpoint.get("label")})

        if batch.get("status") == "partial":
            warnings.append({"batch": batch["batch_id"], "issue_count": len(batch.get("issues", []))})

    return {
        "ok": not errors,
        "archive": str(archive_dir),
        "batches_checked": len(batches),
        "errors": errors,
        "partial_batches": warnings,
    }


def verify_replayed_target(archive_dir: Path, target_fs: Path, target_blob_dir: Path | None,
                           through: int | None = None):
    static_report = verify_archive(archive_dir, through)
    errors = list(static_report["errors"])
    target_gaps = []
    archive = load_archive(archive_dir)
    if not target_fs.exists():
        return {**static_report, "ok": False, "errors": errors + [{"code": "TARGET_FS_NOT_FOUND",
                                                                    "path": str(target_fs)}]}
    storage = FileStorage(str(target_fs), read_only=True)
    helper = FilesystemHelper(str(target_blob_dir), "automatic") if target_blob_dir else None
    batches = archive["batches"]
    if through is not None:
        batches = batches[:through]
    try:
        for batch_ref in batches:
            batch, _, _ = read_batch(archive_dir, batch_ref)
            after_tid = next_tid(parse_oid(batch["tid"]))
            for checkpoint in batch.get("checkpoints", []):
                spec = checkpoint.get("spec", {})
                if spec.get("key"):
                    root_oid = parse_oid(checkpoint.get("root_record_oid") or "0x00")
                    loaded = storage.loadBefore(root_oid, after_tid)
                    if loaded is None:
                        if checkpoint.get("status") == "incomplete":
                            target_gaps.append({
                                "batch": batch["batch_id"], "root": checkpoint["label"],
                                "code": "TARGET_ROOT_NOT_REPLAYABLE_FROM_DECLARED_GAP",
                            })
                        elif checkpoint["status"] != "absent":
                            errors.append({"batch": batch["batch_id"], "root": checkpoint["label"],
                                           "code": "TARGET_ROOT_RECORD_ABSENT"})
                        continue
                    data, _, _ = loaded
                    try:
                        actual_oid = lookup_root_value(safe_load_state(data), spec)
                    except KeyError:
                        actual_oid = None
                    except Exception as exc:
                        errors.append({"batch": batch["batch_id"], "root": checkpoint["label"],
                                       "code": "TARGET_ROOT_DECODE_FAILED", "message": str(exc)})
                        continue
                    expected = parse_oid(checkpoint["anchor_oid"]) if checkpoint.get("anchor_oid") else None
                    if actual_oid != expected:
                        errors.append({"batch": batch["batch_id"], "root": checkpoint["label"],
                                       "code": "TARGET_ROOT_MISMATCH",
                                       "expected": checkpoint.get("anchor_oid"),
                                       "actual": oid_hex(actual_oid) if actual_oid else None})
                for obj in checkpoint.get("objects", []):
                    if not obj.get("payload_sha256"):
                        continue
                    oid = parse_oid(obj["oid"])
                    loaded = storage.loadBefore(oid, after_tid)
                    if loaded is None:
                        errors.append({"batch": batch["batch_id"], "root": checkpoint["label"],
                                       "code": "TARGET_OBJECT_ABSENT", "oid": obj["oid"],
                                       "rev": obj["rev_tid"]})
                        continue
                    data, serial, _end = loaded
                    if oid_hex(serial) != obj["rev_tid"]:
                        errors.append({"batch": batch["batch_id"], "root": checkpoint["label"],
                                       "code": "TARGET_REVISION_MISMATCH", "oid": obj["oid"],
                                       "expected_rev": obj["rev_tid"],
                                       "actual_rev": oid_hex(serial)})
                    if sha256_bytes(data) != obj["payload_sha256"]:
                        errors.append({"batch": batch["batch_id"], "root": checkpoint["label"],
                                       "code": "TARGET_PAYLOAD_MISMATCH", "oid": obj["oid"],
                                       "rev": obj["rev_tid"]})
                    if obj.get("blob_status") == "present" and helper is not None:
                        blob_path = Path(helper.getBlobFilename(oid, serial))
                        if not blob_path.is_file():
                            errors.append({"batch": batch["batch_id"], "root": checkpoint["label"],
                                           "code": "TARGET_BLOB_ABSENT", "oid": obj["oid"],
                                           "rev": obj["rev_tid"], "path": str(blob_path)})
                        elif sha256_file(blob_path) != obj["blob"]["sha256"]:
                            errors.append({"batch": batch["batch_id"], "root": checkpoint["label"],
                                           "code": "TARGET_BLOB_MISMATCH", "oid": obj["oid"],
                                           "rev": obj["rev_tid"]})
    finally:
        storage.close()
    return {"ok": not errors, "archive": str(archive_dir), "target_fs": str(target_fs),
            "batches_checked": len(batches), "errors": errors, "target_gaps": target_gaps,
            "partial_batches": static_report["partial_batches"]}


def replay_archive(archive_dir: Path, target_fs: Path, target_blob_dir: Path | None,
                   through: int | None):
    archive = load_archive(archive_dir)
    batches = archive["batches"]
    if through is not None:
        batches = batches[:through]
    target_fs.parent.mkdir(parents=True, exist_ok=True)
    state_path = Path(str(target_fs) + ".handoff-state.json")
    state = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {"applied": []}
    applied = {item["batch_id"]: item for item in state.get("applied", [])}
    applied_txn_tids = {item["tid"] for item in state.get("applied", [])}
    helper = None
    if target_blob_dir:
        target_blob_dir.mkdir(parents=True, exist_ok=True)
        helper = FilesystemHelper(str(target_blob_dir), "bushy")
        helper.create()
    storage = FileStorage(str(target_fs), create=False)
    try:
        for batch_ref in batches:
            batch, receipt, _ = read_batch(archive_dir, batch_ref)
            prior = applied.get(batch["batch_id"])
            if prior:
                if prior.get("receipt_sha256") != receipt["receipt_sha256"]:
                    raise HandoffError(f"receipt changed for already applied batch {batch['batch_id']}")
                continue
            if batch.get("missing_transaction"):
                applied[batch["batch_id"]] = {
                    "batch_id": batch["batch_id"], "seq": batch["seq"], "tid": batch["tid"],
                    "receipt_sha256": receipt["receipt_sha256"], "gap": True,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                }
                state["applied"] = sorted(applied.values(), key=lambda item: item["seq"])
                write_json(state_path, state)
                continue
            tid = parse_oid(batch["tid"])
            metadata = batch.get("transaction", {})
            transaction = TransactionMetaData(
                bytes.fromhex(metadata.get("user_hex", "")),
                bytes.fromhex(metadata.get("description_hex", "")),
                bytes.fromhex(metadata.get("extension_hex", "")),
            )
            storage.tpc_begin(transaction, tid=tid)
            try:
                for write in batch.get("writes", []):
                    oid = parse_oid(write["oid"])
                    serial = parse_oid(write["serial_tid"])
                    if write.get("kind") == "blob":
                        _write_target_blob(archive_dir, helper, write, oid, serial)
                        continue
                    object_path, _ = store_paths(archive_dir, write["payload_sha256"])
                    data = object_path.read_bytes()
                    origin_tid = write.get("origin_tid")
                    prev_txn = parse_oid(origin_tid) if origin_tid and origin_tid in applied_txn_tids else None
                    storage.restore(oid, serial, data, b"", prev_txn, transaction)
                    if write.get("blob_sha256"):
                        _write_target_blob(archive_dir, helper, write, oid, serial)
                storage.tpc_vote(transaction)
                storage.tpc_finish(transaction)
            except Exception:
                try:
                    storage.tpc_abort(transaction)
                except Exception:
                    pass
                raise
            applied[batch["batch_id"]] = {
                "batch_id": batch["batch_id"],
                "seq": batch["seq"],
                "tid": batch["tid"],
                "receipt_sha256": receipt["receipt_sha256"],
                "applied_at": datetime.now(timezone.utc).isoformat(),
            }
            state["applied"] = sorted(applied.values(), key=lambda item: item["seq"])
            applied_txn_tids.add(batch["tid"])
            write_json(state_path, state)
    finally:
        storage.close()
    return verify_replayed_target(archive_dir, target_fs, target_blob_dir, through)


def _write_target_blob(archive_dir: Path, helper: FilesystemHelper | None, write: dict,
                       oid: bytes, serial: bytes):
    if helper is None or not write.get("blob_sha256"):
        return
    _, source_blob = store_paths(archive_dir, write["blob_sha256"])
    target_blob = Path(helper.getBlobFilename(oid, serial))
    target_blob.parent.mkdir(parents=True, exist_ok=True)
    if not target_blob.exists() or sha256_file(target_blob) != write["blob_sha256"]:
        shutil.copyfile(source_blob, target_blob)


def acknowledge_batch(archive_dir: Path, batch_number: int, by: str, target_fs: Path | None,
                      target_blob_dir: Path | None):
    archive = load_archive(archive_dir)
    if batch_number < 1 or batch_number > len(archive["batches"]):
        raise HandoffError(f"batch {batch_number} does not exist")
    batch_ref = archive["batches"][batch_number - 1]
    batch, receipt, _ = read_batch(archive_dir, batch_ref)
    ack_path = archive_dir / "acknowledgements" / f"{batch['batch_id']}.json"
    if ack_path.exists():
        raise HandoffError(f"acknowledgement already exists and is immutable: {ack_path}")
    replay_report = None
    if target_fs:
        replay_report = verify_replayed_target(archive_dir, target_fs, target_blob_dir, batch_number)
        if not replay_report["ok"]:
            raise HandoffError("refusing acknowledgement because replay verification failed")
    ack = {
        "schema": SCHEMA,
        "batch_id": batch["batch_id"],
        "seq": batch["seq"],
        "tid": batch["tid"],
        "receipt_sha256": receipt["receipt_sha256"],
        "batch_sha256": receipt["batch_sha256"],
        "acknowledged_by": by,
        "acknowledged_at": datetime.now(timezone.utc).isoformat(),
        "replay_report": replay_report,
    }
    ack["ack_sha256"] = sha256_bytes(canonical_json({k: v for k, v in ack.items() if k != "ack_sha256"}))
    write_json(ack_path, ack)
    try:
        os.chmod(ack_path, 0o444)
    except OSError:
        pass
    return ack_path


def run_selftest(keep: bool = False):
    import transaction
    from ZODB import DB
    from ZODB.blob import Blob, BlobStorage

    base = Path(tempfile.mkdtemp(prefix="selftest-", dir=str(REPO_ROOT / "handoff")))
    succeeded = False
    try:
        primary_fs = base / "primary" / "Data.fs"
        primary_blobs = base / "primary" / "blobs"
        primary_blobs.mkdir(parents=True)
        storage = BlobStorage(str(primary_blobs), FileStorage(str(primary_fs)))
        db = DB(storage)
        connection = db.open()
        root = connection.root()

        def make_blob(content: bytes):
            blob = Blob()
            with blob.open("w") as handle:
                handle.write(content)
            return blob

        p_obj = SelfTestInspection("P", "p1", make_blob(b"p1\n"))
        d_obj = SelfTestInspection("D", "d1", make_blob(b"d1\n"))
        root["P"] = p_obj
        root["R1"] = p_obj
        root["D"] = d_obj
        transaction.get().note("selftest T1")
        transaction.commit()
        t1 = oid_hex(db.lastTransaction())

        p_obj = root["P"]
        p_obj.body = "p2"
        p_obj.attachment = make_blob(b"p2\n")
        del root["D"]
        transaction.get().note("selftest T2")
        transaction.commit()
        t2 = oid_hex(db.lastTransaction())

        db.undo(db.undoLog(0, 5)[0]["id"])
        transaction.get().note("selftest T3 undo")
        transaction.commit()
        t3 = oid_hex(db.lastTransaction())
        db.close()

        shutil.copytree(base / "primary", base / "archive")
        order = {
            "case_id": "SELFTEST",
            "output_dir": str(base / "handoff"),
            "range": {"start_tid": t1, "through_tid": t3},
            "common_history_tids": [t1, t2, t3],
            "sources": [
                {"name": "primary", "role": "primary", "verified": True,
                 "fs_path": str(primary_fs), "blob_dir": str(primary_blobs)},
                {"name": "archive", "role": "archive", "verified": True, "borrow": True,
                 "fs_path": str(base / "archive" / "Data.fs"),
                 "blob_dir": str(base / "archive" / "blobs")},
            ],
            "roots": [
                {"label": "P", "key": "P"},
                {"label": "R1", "key": "R1"},
                {"label": "D", "key": "D"},
            ],
        }
        order_path = base / "order.json"
        write_json(order_path, order)
        archive_dir = base / "handoff"
        target_fs = base / "target" / "Data.fs"
        target_blobs = base / "target" / "blobs"

        build_archive(order_path, archive_dir, "0x" + t2)
        replay2 = replay_archive(archive_dir, target_fs, target_blobs, 2)
        if not replay2["ok"]:
            raise HandoffError(f"two-batch replay failed: {replay2['errors']}")
        acknowledge_batch(archive_dir, 2, "selftest", target_fs, target_blobs)
        receipt_path = next((archive_dir / "batches").glob("000002-*/receipt.json"))
        receipt_before = sha256_file(receipt_path)

        build_archive(order_path, archive_dir, "0x" + t3)
        receipt_after = sha256_file(receipt_path)
        if receipt_before != receipt_after:
            raise HandoffError("batch 2 receipt changed after batch 3 was appended")
        replay3 = replay_archive(archive_dir, target_fs, target_blobs, 3)
        if not replay3["ok"]:
            raise HandoffError(f"three-batch replay failed: {replay3['errors']}")

        target_storage = BlobStorage(str(target_blobs), FileStorage(str(target_fs), read_only=True))
        target_db = DB(target_storage)
        try:
            for tid, expected_body, expected_d in [
                (t1, "p1", True), (t2, "p2", False), (t3, "p1", True)
            ]:
                historical = target_db.open(at=parse_oid(tid))
                historical_root = historical.root()
                if historical_root["P"].body != expected_body:
                    raise HandoffError(f"bad body at {tid}")
                if (expected_body.encode() + b"\n") not in historical_root["P"].attachment.open().read():
                    raise HandoffError(f"bad blob at {tid}")
                if ("D" in historical_root) is not expected_d:
                    raise HandoffError(f"bad D visibility at {tid}")
                if historical_root["R1"]._p_oid != historical_root["P"]._p_oid:
                    raise HandoffError(f"shared root reference lost at {tid}")
                historical.close()
        finally:
            target_db.close()
        succeeded = True
        return {"ok": True, "dir": str(base), "tids": {"t1": t1, "t2": t2, "t3": t3}}
    finally:
        if not keep and succeeded:
            shutil.rmtree(base, ignore_errors=True)


def build_parser():
    parser = argparse.ArgumentParser(description="Replayable ZODB root-scoped incremental handoff")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="build append-only handoff batches")
    build.add_argument("--order", required=True, type=Path)
    build.add_argument("--archive", type=Path)
    build.add_argument("--through-tid")

    verify = sub.add_parser("verify", help="verify archive content and root closure")
    verify.add_argument("--archive", required=True, type=Path)
    verify.add_argument("--through", type=int)

    replay = sub.add_parser("replay", help="replay batches into a new FileStorage")
    replay.add_argument("--archive", required=True, type=Path)
    replay.add_argument("--target-fs", required=True, type=Path)
    replay.add_argument("--target-blobs", type=Path)
    replay.add_argument("--through", type=int)

    target_verify = sub.add_parser("verify-target", help="verify replayed FileStorage at each TID")
    target_verify.add_argument("--archive", required=True, type=Path)
    target_verify.add_argument("--target-fs", required=True, type=Path)
    target_verify.add_argument("--target-blobs", type=Path)
    target_verify.add_argument("--through", type=int)

    ack = sub.add_parser("ack", help="write an immutable receiver acknowledgement")
    ack.add_argument("--archive", required=True, type=Path)
    ack.add_argument("--batch", required=True, type=int)
    ack.add_argument("--by", default="receiver")
    ack.add_argument("--target-fs", type=Path)
    ack.add_argument("--target-blobs", type=Path)

    selftest = sub.add_parser("selftest", help="create a fixture and exercise all guarantees")
    selftest.add_argument("--keep", action="store_true")

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "build":
        archive = build_archive(args.order, args.archive, args.through_tid)
        report = verify_archive(archive)
        print(json.dumps({"archive": str(archive), "verification": report}, ensure_ascii=False, indent=2))
        if not report["ok"]:
            return 4
        return 3 if report["partial_batches"] else 0
    if args.command == "verify":
        report = verify_archive(args.archive, args.through)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ok"]:
            return 4
        return 3 if report["partial_batches"] else 0
    if args.command == "replay":
        report = replay_archive(args.archive, args.target_fs.resolve(),
                                args.target_blobs.resolve() if args.target_blobs else None,
                                args.through)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ok"]:
            return 5
        return 3 if report.get("partial_batches") else 0
    if args.command == "verify-target":
        report = verify_replayed_target(args.archive, args.target_fs.resolve(),
                                        args.target_blobs.resolve() if args.target_blobs else None,
                                        args.through)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ok"]:
            return 5
        return 3 if report.get("partial_batches") else 0
    if args.command == "ack":
        path = acknowledge_batch(args.archive, args.batch, args.by,
                                 args.target_fs.resolve() if args.target_fs else None,
                                 args.target_blobs.resolve() if args.target_blobs else None)
        print(json.dumps({"acknowledgement": str(path)}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "selftest":
        report = run_selftest(args.keep)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HandoffError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2),
              file=sys.stderr)
        raise SystemExit(2)
