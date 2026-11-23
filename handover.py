"""Single-pass selective repacking of tar streams, with hardlink resolution.

Scenario: a tar stream is handed over once (no rewinding, bounded staging
memory).  A pick list selects members by ``path#occurrence`` (the N-th entry
carrying that pathname in stream order, 1-based).  Some entries are hard
links; the source's rule is that a link refers to the most recent preceding
entry with the target's name, or - if the name has not appeared yet - to the
first entry with that name that arrives later.  Links may point at other
links, forming chains.

This module scans the stream once, keeps as many bodies as the staging
budget allows, then writes the selected members into a new, self-contained
archive.  Selected members that resolve to the same physical body are
written as one regular file plus hard links, so the relation is visible when
the new package is re-read; independent bodies stay independent even if
their contents happen to be identical.  Cyclic or dangling links that are
unrelated to the pick list do not block the delivery of the other members.
If a needed body was evicted from staging, the failure report names the
selected member, the link chain it follows, and the original occurrence
whose body was lost.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass

from libarchive import ffi
from libarchive.entry import ArchiveEntry, FileType
from libarchive.ffi import write_header, write_finish_entry
from libarchive.read import stream_reader, memory_reader, file_reader
from libarchive.write import file_writer

DEFAULT_BUFFER_LIMIT = 64 * 1024 * 1024


@dataclass(frozen=True)
class Pick:
    """One item of the pick list: the `occurrence`-th entry named `pathname`."""
    pathname: str
    occurrence: int = 1

    def __post_init__(self):
        if self.occurrence < 1:
            raise ValueError('occurrence is 1-based, got %r' % self.occurrence)

    @property
    def label(self):
        return '%s#%d' % (self.pathname, self.occurrence)

    @classmethod
    def parse(cls, text):
        """Parse `PATH#OCC`; without `#OCC` the first occurrence is meant."""
        name, sep, num = text.rpartition('#')
        if sep and num.isdigit():
            return cls(name, int(num))
        return cls(text, 1)


@dataclass
class EntryRecord:
    """Metadata captured for one entry while scanning the stream."""
    seq: int
    pathname: str
    occurrence: int
    is_link: bool
    link_target: str | None
    is_regular: bool
    filetype: int
    size: int
    perm: int
    uid: int
    gid: int
    uname: str | None
    gname: str | None
    mtime: float | None
    body_evicted: bool = False

    @property
    def label(self):
        return '%s#%d' % (self.pathname, self.occurrence)


@dataclass
class Eviction:
    """Record of a body dropped from staging because of the budget."""
    pathname: str
    occurrence: int
    size: int
    protected: bool

    @property
    def label(self):
        return '%s#%d' % (self.pathname, self.occurrence)


@dataclass
class Delivery:
    """A pick-list member that made it into the new package."""
    pick: Pick
    out_path: str
    kind: str  # 'file' | 'link'
    source: EntryRecord  # the original occurrence holding the body
    chain: list  # EntryRecords from the selected member to the source
    link_target: str | None = None


@dataclass
class Failure:
    """A pick-list member that could not be delivered, with the trace."""
    pick: Pick
    reason: str  # 'missing' | 'cycle' | 'dangling' | 'body-lost' | 'not-regular'
    chain: list  # EntryRecords followed before the failure
    detail: str

    def describe(self):
        chain = ' -> '.join(rec.label for rec in self.chain)
        if chain:
            return '%s: %s (%s)' % (self.pick.label, chain, self.detail)
        return '%s: %s' % (self.pick.label, self.detail)


class ResolutionError(Exception):
    """Raised when a link chain cannot be followed to a regular file."""

    def __init__(self, reason, chain, detail):
        super().__init__(detail)
        self.reason = reason
        self.chain = chain
        self.detail = detail


class ScanResult:
    """Everything gathered from the single pass over the source stream."""

    def __init__(self, buffer_limit):
        self.buffer_limit = buffer_limit
        self.entries = []
        self.by_name = defaultdict(list)
        self.bodies = {}  # seq -> bytes, for bodies still in staging
        self.evicted = []

    def find(self, pathname, occurrence):
        records = self.by_name.get(pathname)
        if records and occurrence <= len(records):
            return records[occurrence - 1]
        return None

    def body_of(self, record):
        return self.bodies.get(record.seq)


def scan_stream(stream, picks, buffer_limit=DEFAULT_BUFFER_LIMIT,
                header_codec='utf-8'):
    """Read the source tar stream exactly once and stage what may be needed.

    `picks` is an iterable of `Pick`.  Bodies of regular entries are kept
    while they fit in `buffer_limit` bytes; when the budget is exceeded the
    oldest unprotected body is evicted first (protected = directly picked or
    already known to be the link target of a picked member).  Because the
    pick list is known before the links are seen, any earlier body may still
    become the target of a later picked link, so eviction is a last resort
    and is always recorded in `ScanResult.evicted`.
    """
    if buffer_limit < 0:
        raise ValueError('buffer_limit must be >= 0')
    scan = ScanResult(buffer_limit)
    pick_set = {(p.pathname, p.occurrence) for p in picks}
    protected = set()
    demanded_next = set()  # names whose first future occurrence is needed
    demand_done = set()
    buffered = 0

    def process_demand(rec):
        if rec.seq in demand_done:
            return
        demand_done.add(rec.seq)
        if rec.is_link:
            demand_target(rec.link_target, rec.seq)
        elif rec.is_regular:
            protected.add(rec.seq)

    def demand_target(tname, at_seq):
        prior = None
        for rec in scan.by_name.get(tname, ()):
            if rec.seq < at_seq:
                prior = rec
            else:
                break
        if prior is not None:
            process_demand(prior)
        else:
            demanded_next.add(tname)

    def evict_one():
        nonlocal buffered
        for seq in sorted(scan.bodies):
            if seq not in protected:
                break
        else:
            seq = min(scan.bodies)
        rec = scan.entries[seq]
        scan.evicted.append(Eviction(
            rec.pathname, rec.occurrence, len(scan.bodies[seq]),
            protected=seq in protected,
        ))
        rec.body_evicted = True
        buffered -= len(scan.bodies.pop(seq))

    with stream_reader(stream, header_codec=header_codec) as archive:
        for entry in archive:
            pathname = entry.pathname
            occurrence = len(scan.by_name[pathname]) + 1
            is_link = entry.islnk
            record = EntryRecord(
                seq=len(scan.entries),
                pathname=pathname,
                occurrence=occurrence,
                is_link=is_link,
                link_target=entry.linkpath if is_link else None,
                is_regular=entry.isreg and not is_link,
                filetype=entry.filetype,
                size=entry.size or 0,
                perm=entry.perm,
                uid=entry.uid,
                gid=entry.gid,
                uname=entry.uname,
                gname=entry.gname,
                mtime=entry.mtime,
            )
            scan.entries.append(record)
            scan.by_name[pathname].append(record)
            if (pathname, occurrence) in pick_set or pathname in demanded_next:
                demanded_next.discard(pathname)
                process_demand(record)
            if record.is_regular:
                body = b''.join(entry.get_blocks())
                scan.bodies[record.seq] = body
                buffered += len(body)
                while buffered > buffer_limit:
                    evict_one()
    return scan


def scan_file(path, picks, buffer_limit=DEFAULT_BUFFER_LIMIT,
              header_codec='utf-8'):
    with open(path, 'rb') as f:
        return scan_stream(f, picks, buffer_limit, header_codec)


def resolve(scan, record):
    """Follow the source's hardlink rule from `record` to a non-link entry.

    A link points at the most recent preceding entry with the target's name,
    or at the first later one if the name has not appeared yet.  Returns
    `(final_record, chain)`.  Raises `ResolutionError` on cycles, dangling
    targets, or if the chain ends at a non-regular entry.
    """
    chain = [record]
    seen = {record.seq}
    current = record
    while current.is_link:
        tname = current.link_target
        candidates = scan.by_name.get(tname, [])
        prior = [r for r in candidates if r.seq < current.seq]
        if prior:
            nxt = prior[-1]
        else:
            later = [r for r in candidates if r.seq > current.seq]
            if not later:
                raise ResolutionError(
                    'dangling', chain,
                    'no entry named %r ever appeared' % tname,
                )
            nxt = later[0]
        if nxt.seq in seen:
            chain.append(nxt)
            raise ResolutionError(
                'cycle', chain, 'the links form a cycle'
            )
        chain.append(nxt)
        seen.add(nxt.seq)
        current = nxt
    if not current.is_regular:
        raise ResolutionError(
            'not-regular', chain,
            'the chain ends at a non-regular entry (%s)' % current.label,
        )
    return current, chain


def _entry_attributes(record):
    attrs = {'perm': record.perm, 'uid': record.uid, 'gid': record.gid}
    if record.mtime is not None:
        attrs['mtime'] = record.mtime
    if record.uname:
        attrs['uname'] = record.uname
    if record.gname:
        attrs['gname'] = record.gname
    return attrs


def write_hardlink_entry(archive_write, pathname, target, record=None,
                         header_codec='utf-8'):
    """Write one hardlink entry (`pathname` -> `target`) to an ArchiveWrite."""
    attrs = _entry_attributes(record) if record else {}
    entry = ArchiveEntry(
        pathname=pathname, filetype=FileType.REGULAR_FILE, size=0,
        header_codec=header_codec, **attrs
    )
    ffi.entry_set_hardlink(entry._entry_p, target.encode(header_codec))
    write_header(archive_write._pointer, entry._entry_p)
    write_finish_entry(archive_write._pointer)


def _write_regular_entry(archive_write, record, body):
    attrs = _entry_attributes(record)
    permission = attrs.pop('perm')
    archive_write.add_file_from_memory(
        record.pathname, len(body), body, permission=permission, **attrs
    )


def build_package(scan, picks, archive_write):
    """Write the deliverable picked members to `archive_write`.

    Members resolving to the same physical body are emitted as one regular
    file (the first one in pick-list order) plus hard links pointing at it,
    so the relation survives re-reading.  Independent bodies are emitted as
    independent files even when their contents are identical.

    Returns `(deliveries, failures)`.
    """
    deliveries = []
    failures = []
    carriers = {}  # source seq -> output pathname of the regular file
    for pick in picks:
        record = scan.find(pick.pathname, pick.occurrence)
        if record is None:
            failures.append(Failure(
                pick, 'missing', [],
                'no occurrence %d of %r in the stream'
                % (pick.occurrence, pick.pathname),
            ))
            continue
        try:
            source, chain = resolve(scan, record)
        except ResolutionError as e:
            failures.append(Failure(pick, e.reason, e.chain, e.detail))
            continue
        body = scan.body_of(source)
        if body is None:
            failures.append(Failure(
                pick, 'body-lost', chain,
                'the body of %s (%d bytes) was evicted from staging'
                % (source.label, source.size),
            ))
            continue
        if source.seq in carriers:
            target = carriers[source.seq]
            write_hardlink_entry(
                archive_write, record.pathname, target, record,
            )
            deliveries.append(Delivery(
                pick, record.pathname, 'link', source, chain, target,
            ))
        else:
            _write_regular_entry(archive_write, record, body)
            carriers[source.seq] = record.pathname
            deliveries.append(
                Delivery(pick, record.pathname, 'file', source, chain)
            )
    return deliveries, failures


def verify_package(scan, deliveries, buf=None, path=None,
                   header_codec='utf-8'):
    """Re-read the new package and check it against the pick-list results.

    Confirms that every delivered member is present in order, that regular
    entries carry the exact source body, and that hardlink entries point at
    the carrier of the same physical body (no more, no less).  Returns a
    list of problem descriptions; empty means the package checks out.
    """
    problems = []
    if buf is not None:
        reader = memory_reader(buf, header_codec=header_codec)
    else:
        reader = file_reader(path, header_codec=header_codec)
    got = []
    with reader as archive:
        for entry in archive:
            content = (
                b''.join(entry.get_blocks())
                if entry.isreg and not entry.islnk else None
            )
            got.append((entry, content))
    if len(got) != len(deliveries):
        problems.append(
            'package holds %d entries, expected %d'
            % (len(got), len(deliveries))
        )
    carriers = {}
    for delivery, (entry, content) in zip(deliveries, got):
        label = delivery.pick.label
        if entry.pathname != delivery.out_path:
            problems.append(
                '%s: packaged as %r instead of %r'
                % (label, entry.pathname, delivery.out_path)
            )
            continue
        if delivery.kind == 'file':
            if entry.islnk or not entry.isreg:
                problems.append('%s: not a regular file in the package' % label)
                continue
            expected = scan.body_of(delivery.source)
            if content != expected:
                problems.append(
                    '%s: content differs from the body of %s'
                    % (label, delivery.source.label)
                )
            if delivery.source.seq in carriers:
                problems.append(
                    '%s: body of %s packaged as a file twice'
                    % (label, delivery.source.label)
                )
            carriers[delivery.source.seq] = entry.pathname
        elif delivery.kind == 'link':
            if not entry.islnk:
                problems.append('%s: not a hard link in the package' % label)
                continue
            carrier = carriers.get(delivery.source.seq)
            if carrier is None:
                problems.append(
                    '%s: link appears before the carrier of %s'
                    % (label, delivery.source.label)
                )
            elif entry.linkpath != carrier:
                problems.append(
                    '%s: points at %r instead of %r'
                    % (label, entry.linkpath, carrier)
                )
    return problems


def run(input_path, output_path, picks, buffer_limit=DEFAULT_BUFFER_LIMIT,
        format_name='ustar', filter_name=None, header_codec='utf-8', out=None):
    """Whole handover: scan once, build the package, verify it, report."""
    out = out or sys.stdout
    scan = scan_file(input_path, picks, buffer_limit, header_codec)
    with file_writer(output_path, format_name, filter_name,
                     header_codec=header_codec) as archive:
        deliveries, failures = build_package(scan, picks, archive)
    problems = verify_package(scan, deliveries, path=output_path,
                              header_codec=header_codec)
    for delivery in deliveries:
        chain = ' -> '.join(rec.label for rec in delivery.chain)
        print('delivered %s as %s %r (body of %s via %s)'
              % (delivery.pick.label, delivery.kind, delivery.out_path,
                 delivery.source.label, chain), file=out)
    for failure in failures:
        print('FAILED %s' % failure.describe(), file=out)
    for problem in problems:
        print('VERIFY %s' % problem, file=out)
    if not failures and not problems:
        print('all %d picked member(s) delivered and verified'
              % len(deliveries), file=out)
    return not failures and not problems


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='handover',
        description='Repack picked members of a tar stream, read once, '
                    'into a self-contained archive.',
    )
    parser.add_argument('input', help='source tar stream (read exactly once)')
    parser.add_argument('output', help='path of the package to write')
    parser.add_argument(
        '--pick', action='append', required=True, metavar='PATH#OCC',
        help='member to pick, by pathname and 1-based occurrence; '
             'repeatable, output follows this order',
    )
    parser.add_argument(
        '--buffer-limit', type=int, default=DEFAULT_BUFFER_LIMIT,
        metavar='BYTES', help='staging budget for bodies (default: %(default)s)',
    )
    parser.add_argument('--format', default='ustar', dest='format_name',
                        help='output archive format (default: %(default)s)')
    parser.add_argument('--filter', default=None, dest='filter_name',
                        help='output compression filter')
    args = parser.parse_args(argv)
    picks = [Pick.parse(text) for text in args.pick]
    ok = run(args.input, args.output, picks, args.buffer_limit,
             args.format_name, args.filter_name)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
