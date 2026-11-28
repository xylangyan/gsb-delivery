"""
Revision-consistent snapshot export from read-only mirror copies of a
CKAN catalog.

The source catalog is kept as two (or more) readable copies that sync
at their own pace, so at any moment the copies may disagree: one copy
already serves the updated dataset page while the copy hosting the
attachments still serves the previous version.  Every page a copy
serves carries the revision range the supplier vouches for
(``valid_revisions``) and attachment content is credentialed under the
*original* resource id with the revision range for which each content
version is valid.

export_snapshot() picks a single catalog revision inside the report
interval for which dataset pages, cross-dataset references and
resource credentials are mutually confirmable, preferring the most
recent such revision, and builds the exported package only from
evidence valid at that revision.  References are resolved by original
resource id, never by URL: a new dataset page and a stale attachment
page may share a URL and even byte-identical content while never
being valid at the same time.

Evidence is the union of what the copies serve: one copy missing a
page entirely (an empty result) never vetoes another copy's valid
evidence for the same range.  When no revision in the interval can be
confirmed, UnconfirmableRange is raised carrying structured evidence
of exactly what is missing.

A ReleaseRegistry may be supplied to make released packages
immutable: once a package has formally gone out, a recomputation that
lands on a non-equivalent version (e.g. because an old page arrived
late) does not move the released product.
"""

import hashlib
import json

from ckanapi.errors import CKANAPIError, NotFound


class UnconfirmableRange(CKANAPIError):
    """
    Raised when no revision in the report interval can be confirmed
    against the evidence collected from the mirror copies.

    The ``evidence`` attribute is a dict with the report interval, the
    per-revision list of what was missing and the empty results
    returned by each copy.
    """
    def __init__(self, message, evidence):
        CKANAPIError.__init__(self, message)
        self.evidence = evidence


def _mirror_name(mirror):
    return getattr(mirror, 'name', None) or mirror.__class__.__name__


def _covers(record, revision):
    lo, hi = record['valid_revisions']
    return lo <= revision <= hi


def _check_credential(version):
    if hashlib.sha256(version['content']).hexdigest() != version['sha256']:
        raise CKANAPIError(
            'content credential mismatch for resource %r'
            % (version.get('id'),))


def export_snapshot(mirrors, dataset_names, report_interval,
                    registry=None, release_name=None):
    """
    Build a revision-consistent snapshot package for dataset_names
    from the readable catalog copies in mirrors.

    mirrors are queried in the order given, datasets in the order
    named; report_interval is a (lo, hi) pair of catalog revisions the
    snapshot must fall into.  Returns the package dict, or the
    previously released package when a registry is given and the
    recomputed package is not equivalent to the release.
    """
    mirrors = list(mirrors)
    lo, hi = report_interval

    pages = {name: [] for name in dataset_names}
    content = {}
    gaps = []

    def ensure_content(resource_id):
        """
        Collect all content change ranges the copies serve for
        resource_id.  Discovery is authoritative: a copy that has no
        content for the original resource id simply contributes a gap.
        """
        if resource_id in content:
            return
        content[resource_id] = []
        for mirror in mirrors:
            try:
                record = mirror.call_action(
                    'resource_content', {'id': resource_id})
            except NotFound:
                gaps.append({
                    'mirror': _mirror_name(mirror),
                    'resource': resource_id,
                    'missing': 'resource_content',
                })
                continue
            for version in record['versions']:
                _check_credential(version)
                content[resource_id].append((mirror, version))

    # discovery pass: latest page of every dataset from every copy
    for name in dataset_names:
        for mirror in mirrors:
            try:
                page = mirror.call_action('package_show', {'id': name})
            except NotFound:
                gaps.append({
                    'mirror': _mirror_name(mirror),
                    'dataset': name,
                    'missing': 'dataset_page',
                })
            else:
                pages[name].append((mirror, page))

    # content credentials for every resource referenced so far, in the
    # order resources first appear across the discovered pages
    for name in dataset_names:
        for _, page in pages[name]:
            for res in page.get('resources', []):
                ensure_content(res['id'])
            for ref in page.get('references', []):
                ensure_content(ref['resource_id'])

    missed_pages = set()

    def ensure_page(name, revision):
        """
        Re-query the copies for a catalog version of name pinned at
        revision, unless one of the pages already collected covers it.
        """
        if any(_covers(p, revision) for _, p in pages[name]):
            return
        if (name, revision) in missed_pages:
            return
        found = False
        for mirror in mirrors:
            try:
                page = mirror.call_action('package_show',
                    {'id': name, 'revision_id': revision})
            except NotFound:
                gaps.append({
                    'mirror': _mirror_name(mirror),
                    'dataset': name,
                    'missing': 'dataset_page',
                    'revision': revision,
                })
            else:
                pages[name].append((mirror, page))
                found = True
        if not found:
            missed_pages.add((name, revision))

    # selection pass: most recent revision in the report interval for
    # which entries, references and resource credentials all confirm
    unconfirmable = []
    chosen = None
    for revision in range(hi, lo - 1, -1):
        missing = []
        for name in dataset_names:
            ensure_page(name, revision)
        covering = {}
        for name in dataset_names:
            covering[name] = [p for _, p in pages[name]
                if _covers(p, revision)]
            if not covering[name]:
                missing.append({'dataset': name, 'missing': 'dataset_page'})
        available = set()
        for name in dataset_names:
            for page in covering[name]:
                for res in page.get('resources', []):
                    available.add(res['id'])
        needed = []
        for name in dataset_names:
            for page in covering[name]:
                for res in page.get('resources', []):
                    if res['id'] not in needed:
                        needed.append(res['id'])
                for ref in page.get('references', []):
                    rid = ref['resource_id']
                    if rid not in available:
                        missing.append({
                            'dataset': name,
                            'missing': 'reference',
                            'resource': rid,
                        })
                    if rid not in needed:
                        needed.append(rid)
        for rid in needed:
            ensure_content(rid)
            if not any(_covers(v, revision) for _, v in content[rid]):
                missing.append({
                    'resource': rid,
                    'missing': 'resource_content',
                })
        if not missing:
            chosen = revision
            break
        unconfirmable.append({'revision': revision, 'missing': missing})

    if chosen is None:
        raise UnconfirmableRange(
            'no confirmable revision in report interval %r'
            % (list(report_interval),),
            evidence={
                'report_interval': [lo, hi],
                'unconfirmable': unconfirmable,
                'gaps': gaps,
            })

    def select(evidence, key):
        """
        Most recent evidence covering the chosen revision; ties break
        towards the copy that answered first, keeping the view
        repeatable.
        """
        best = None
        for index, (mirror, record) in enumerate(evidence):
            if not _covers(record, chosen):
                continue
            candidate = (key(record), -index)
            if best is None or candidate > best[0]:
                best = (candidate, record)
        return best[1]

    datasets = []
    needed = []
    for name in dataset_names:
        page = select(pages[name], lambda p: p['revision_id'])
        datasets.append(page)
        for res in page.get('resources', []):
            if res['id'] not in needed:
                needed.append(res['id'])
        for ref in page.get('references', []):
            if ref['resource_id'] not in needed:
                needed.append(ref['resource_id'])

    resources = []
    for rid in needed:
        version = select(content[rid], lambda v: v['valid_revisions'][0])
        resources.append({
            'id': rid,
            'sha256': version['sha256'],
            'content': version['content'],
        })

    package = {
        'revision': chosen,
        'report_interval': [lo, hi],
        'datasets': datasets,
        'resources': resources,
        'evidence': {
            'mirrors': [_mirror_name(m) for m in mirrors],
            'gaps': gaps,
            'unconfirmable': unconfirmable,
        },
    }
    if registry is not None:
        return registry.release(release_name, package)
    return package


def package_fingerprint(package):
    """
    Content-equivalence hash of a snapshot package: dataset pages and
    resource credentials compared by content, ignoring revision
    bookkeeping.
    """
    def page_content(page):
        return {k: v for k, v in page.items()
            if k not in ('revision_id', 'valid_revisions')}
    canonical = {
        'datasets': [page_content(p) for p in package['datasets']],
        'resources': sorted(
            (r['id'], r['sha256']) for r in package['resources']),
    }
    return hashlib.sha256(json.dumps(
        canonical, sort_keys=True).encode('utf-8')).hexdigest()


class ReleaseRegistry(object):
    """
    Record of formally released snapshot packages.

    A release that has gone out is never moved to another version:
    equivalent recomputations get the canonical released package back
    unchanged, and a recomputation that lands on a non-equivalent
    version (e.g. because an old page arrived late) does not move the
    released product either.
    """
    def __init__(self):
        self._released = {}

    def release(self, name, package):
        fingerprint = package_fingerprint(package)
        existing = self._released.get(name)
        if existing is not None:
            return existing[1]
        self._released[name] = (fingerprint, package)
        return package
