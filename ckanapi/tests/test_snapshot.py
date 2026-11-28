import hashlib
import unittest

from ckanapi.errors import CKANAPIError, NotFound
from ckanapi.snapshot import (
    UnconfirmableRange, ReleaseRegistry, export_snapshot,
    package_fingerprint)
from ckanapi.tests.mock.mirror_ckan import MirrorCKAN


ATT_URL = 'http://files.example/att.csv'

A_OLD = {
    'id': 'aaa',
    'name': 'a',
    'title': 'Dataset A',
    'revision_id': 3,
    'valid_revisions': [1, 4],
    'resources': [],
    'references': [{'resource_id': 'res-1', 'url': ATT_URL}],
}
A_NEW = {
    'id': 'aaa',
    'name': 'a',
    'title': 'Dataset A',
    'revision_id': 7,
    'valid_revisions': [5, 9],
    'resources': [],
    'references': [{'resource_id': 'res-2', 'url': ATT_URL}],
}
B_OLD = {
    'id': 'bbb',
    'name': 'b',
    'title': 'Dataset B',
    'revision_id': 2,
    'valid_revisions': [1, 4],
    'resources': [{'id': 'res-1', 'url': ATT_URL, 'format': 'csv'}],
}
B_NEW = {
    'id': 'bbb',
    'name': 'b',
    'title': 'Dataset B',
    'revision_id': 8,
    'valid_revisions': [5, 9],
    'resources': [{'id': 'res-2', 'url': ATT_URL, 'format': 'csv'}],
}

CONTENT_V1 = b'attachment-bytes-v1'
CONTENT_V2 = b'attachment-bytes-v2'


def full_mirror(name):
    """
    A copy holding the complete old and new catalog: A and B pages for
    revisions [1,4] and [5,9], plus content for both resources.
    """
    mirror = MirrorCKAN(name)
    for page in (A_OLD, A_NEW, B_OLD, B_NEW):
        mirror.add_page(page)
    mirror.add_content('res-1', CONTENT_V1, [1, 4])
    mirror.add_content('res-2', CONTENT_V2, [5, 9])
    return mirror


def stale_mirror(name, res1_content=CONTENT_V1):
    """
    A copy where A is fully updated but the attachment host still
    serves the previous version: A's new page is there, B only has
    the old page and the old resource under the shared URL.
    """
    mirror = MirrorCKAN(name)
    for page in (A_OLD, A_NEW, B_OLD):
        mirror.add_page(page)
    mirror.add_content('res-1', res1_content, [1, 4])
    return mirror


class TestSnapshotExport(unittest.TestCase):
    def test_request_order_and_final_package(self):
        copy1 = full_mirror('copy-1')
        copy2 = full_mirror('copy-2')
        package = export_snapshot([copy1, copy2], ['a', 'b'], [1, 9])

        # most recent confirmable revision in the report interval
        self.assertEqual(package['revision'], 9)
        self.assertEqual(package['report_interval'], [1, 9])
        self.assertEqual(
            [d['revision_id'] for d in package['datasets']], [7, 8])
        self.assertEqual(package['resources'], [{
            'id': 'res-2',
            'sha256': hashlib.sha256(CONTENT_V2).hexdigest(),
            'content': CONTENT_V2,
        }])
        self.assertEqual(package['evidence'], {
            'mirrors': ['copy-1', 'copy-2'],
            'gaps': [],
            'unconfirmable': [],
        })

        # deterministic request order on both copies: dataset pages in
        # export order, then content credentials by original resource id
        expected = [
            ('package_show', 'a', None),
            ('package_show', 'b', None),
            ('resource_content', 'res-2', None),
        ]
        self.assertEqual(copy1.requests, expected)
        self.assertEqual(copy2.requests, expected)

    def test_repeatable_view(self):
        first = export_snapshot(
            [full_mirror('copy-1'), full_mirror('copy-2')],
            ['a', 'b'], [1, 9])
        second = export_snapshot(
            [full_mirror('copy-1'), full_mirror('copy-2')],
            ['a', 'b'], [1, 9])
        self.assertEqual(first, second)

    def test_requeryable_catalog_versions(self):
        mirror = full_mirror('copy-1')
        page1 = mirror.call_action('package_show',
            {'id': 'a', 'revision_id': 4})
        page2 = mirror.call_action('package_show',
            {'id': 'a', 'revision_id': 4})
        self.assertEqual(page1, page2)
        self.assertEqual(page1['revision_id'], 3)
        latest = mirror.call_action('package_show', {'id': 'a'})
        self.assertEqual(latest['revision_id'], 7)
        versions = mirror.call_action('resource_content', {'id': 'res-1'})
        self.assertEqual(
            versions['versions'][0]['valid_revisions'], [1, 4])
        self.assertRaises(NotFound, mirror.call_action,
            'package_show', {'id': 'a', 'revision_id': 0})

    def test_never_simultaneously_valid_despite_shared_url(self):
        # A's new page is read first, B's old page second; the old
        # attachment is still queryable under the same URL and is
        # byte-identical to the new one.  A URL-based export would
        # look complete, but by original resource id and revision
        # ranges A's new page and B's old page were never valid at
        # the same time.
        copy1 = stale_mirror('copy-1', res1_content=CONTENT_V2)
        copy2 = stale_mirror('copy-2', res1_content=CONTENT_V2)
        package = export_snapshot([copy1, copy2], ['a', 'b'], [1, 9])

        # the exporter falls back to the newest mutually confirmable
        # revision instead of emitting the seemingly complete mix
        self.assertEqual(package['revision'], 4)
        self.assertEqual(
            [d['revision_id'] for d in package['datasets']], [3, 2])
        self.assertEqual(
            [r['id'] for r in package['resources']], ['res-1'])

        # the unconfirmable part of the interval is reported as evidence
        unconfirmable = package['evidence']['unconfirmable']
        self.assertEqual(
            [u['revision'] for u in unconfirmable], [9, 8, 7, 6, 5])
        self.assertIn(
            {'dataset': 'a', 'missing': 'reference', 'resource': 'res-2'},
            unconfirmable[0]['missing'])

        # request order: latest pages first, then catalog versions
        # re-queried pinned at each candidate revision
        self.assertEqual(copy1.requests, [
            ('package_show', 'a', None),
            ('package_show', 'b', None),
            ('resource_content', 'res-2', None),
            ('resource_content', 'res-1', None),
            ('package_show', 'b', 9),
            ('package_show', 'b', 8),
            ('package_show', 'b', 7),
            ('package_show', 'b', 6),
            ('package_show', 'b', 5),
            ('package_show', 'a', 4),
        ])

    def test_empty_result_does_not_veto(self):
        # copy-2 is missing the whole B page and its content; copy-1's
        # valid evidence for the same range still confirms the export
        copy1 = full_mirror('copy-1')
        copy2 = MirrorCKAN('copy-2')
        copy2.add_page(A_OLD)
        copy2.add_page(A_NEW)
        package = export_snapshot([copy1, copy2], ['a', 'b'], [1, 9])

        self.assertEqual(package['revision'], 9)
        self.assertEqual(
            [r['id'] for r in package['resources']], ['res-2'])
        self.assertEqual(package['evidence']['gaps'], [
            {'mirror': 'copy-2', 'dataset': 'b',
             'missing': 'dataset_page'},
            {'mirror': 'copy-2', 'resource': 'res-2',
             'missing': 'resource_content'},
        ])
        self.assertEqual(copy2.requests, [
            ('package_show', 'a', None),
            ('package_show', 'b', None),
            ('resource_content', 'res-2', None),
        ])

    def test_unconfirmable_range_evidence(self):
        # no copy can back A's new reference, and the old A page is
        # gone entirely: nothing in the interval is confirmable
        copy1 = MirrorCKAN('copy-1')
        copy1.add_page(A_NEW)
        copy1.add_page(B_OLD)
        copy1.add_content('res-1', CONTENT_V1, [1, 4])
        copy2 = MirrorCKAN('copy-2')

        try:
            export_snapshot([copy1, copy2], ['a', 'b'], [1, 9])
        except UnconfirmableRange as e:
            evidence = e.evidence
        else:
            self.fail('expected UnconfirmableRange')

        self.assertEqual(evidence['report_interval'], [1, 9])
        self.assertEqual(
            [u['revision'] for u in evidence['unconfirmable']],
            [9, 8, 7, 6, 5, 4, 3, 2, 1])
        recent = evidence['unconfirmable'][0]['missing']
        self.assertIn(
            {'dataset': 'b', 'missing': 'dataset_page'}, recent)
        self.assertIn(
            {'dataset': 'a', 'missing': 'reference', 'resource': 'res-2'},
            recent)
        self.assertIn(
            {'resource': 'res-2', 'missing': 'resource_content'}, recent)
        old = evidence['unconfirmable'][-1]['missing']
        self.assertEqual(old, [
            {'dataset': 'a', 'missing': 'dataset_page'}])
        self.assertIn(
            {'mirror': 'copy-2', 'dataset': 'a', 'missing': 'dataset_page'},
            evidence['gaps'])

    def test_real_attachments_resolved_by_resource_id(self):
        copy1 = full_mirror('copy-1')
        copy2 = full_mirror('copy-2')
        package = export_snapshot([copy1, copy2], ['a', 'b'], [1, 9])

        # the package carries the real attachment bytes credentialed
        # under the original resource id, not whatever the shared URL
        # happens to serve
        self.assertEqual(package['resources'], [{
            'id': 'res-2',
            'sha256': hashlib.sha256(CONTENT_V2).hexdigest(),
            'content': CONTENT_V2,
        }])
        self.assertNotIn(CONTENT_V1,
            [r['content'] for r in package['resources']])

    def test_content_credential_mismatch_rejected(self):
        copy1 = full_mirror('copy-1')
        copy1.add_content('res-2', CONTENT_V2, [5, 9], sha256='0' * 64)
        self.assertRaises(CKANAPIError, export_snapshot,
            [copy1], ['a', 'b'], [1, 9])

    def test_late_old_page_does_not_move_release(self):
        registry = ReleaseRegistry()

        def presync(name):
            # both copies agree on the new catalog; the old B page
            # has not synced yet
            mirror = MirrorCKAN(name)
            mirror.add_page(A_OLD)
            mirror.add_page(A_NEW)
            mirror.add_page(B_NEW)
            mirror.add_content('res-2', CONTENT_V2, [5, 9])
            return mirror

        copy1 = presync('copy-1')
        copy2 = presync('copy-2')
        released = export_snapshot([copy1, copy2], ['a', 'b'], [1, 9],
            registry=registry, release_name='nightly')
        self.assertEqual(released['revision'], 9)

        # the old B page and its content arrive late on both copies:
        # the released package is undisturbed
        for mirror in (copy1, copy2):
            mirror.add_page(B_OLD)
            mirror.add_content('res-1', CONTENT_V1, [1, 4])
        again = export_snapshot([copy1, copy2], ['a', 'b'], [1, 9],
            registry=registry, release_name='nightly')
        self.assertEqual(again, released)

        # later the copies have lost the new-catalog evidence and only
        # the late old pages remain: recomputation lands on the older,
        # non-equivalent revision...
        stale = MirrorCKAN('copy-stale')
        stale.add_page(A_OLD)
        stale.add_page(A_NEW)
        stale.add_page(B_OLD)
        stale.add_content('res-1', CONTENT_V1, [1, 4])
        recomputed = export_snapshot([stale], ['a', 'b'], [1, 9])
        self.assertEqual(recomputed['revision'], 4)
        self.assertNotEqual(
            package_fingerprint(recomputed),
            package_fingerprint(released))

        # ...but the released product is not moved to it
        delivered = export_snapshot([stale], ['a', 'b'], [1, 9],
            registry=registry, release_name='nightly')
        self.assertIs(delivered, released)
        self.assertEqual(delivered['revision'], 9)
