"""
A small local CKAN stand-in for snapshot tests.

One MirrorCKAN instance plays one readable copy of the source
catalog.  It serves dataset pages and attachment content annotated
with the revision ranges the supplier vouches for, supports
re-querying catalog versions pinned at a revision, and records every
request in order so tests can assert the exporter's access pattern.
"""

import copy
import hashlib

from ckanapi.errors import CKANAPIError, NotFound


class MirrorCKAN(object):
    def __init__(self, name):
        self.name = name
        self.requests = []
        self._pages = {}
        self._content = {}

    def add_page(self, page):
        """
        Add a dataset page version.  page carries 'name',
        'revision_id' and 'valid_revisions' = [lo, hi].
        """
        self._pages.setdefault(page['name'], []).append(page)

    def add_content(self, resource_id, content, valid_revisions,
                    sha256=None):
        """
        Add one content version for the original resource id, valid
        over valid_revisions = [lo, hi].  sha256 may be overridden to
        simulate a broken credential.
        """
        if sha256 is None:
            sha256 = hashlib.sha256(content).hexdigest()
        self._content.setdefault(resource_id, []).append({
            'id': resource_id,
            'content': content,
            'sha256': sha256,
            'valid_revisions': list(valid_revisions),
        })

    def call_action(self, action, data_dict, requests_kwargs=None):
        if action == 'package_show':
            return self._package_show(data_dict)
        if action == 'resource_content':
            return self._resource_content(data_dict)
        raise CKANAPIError('unsupported action %r' % (action,))

    def _package_show(self, data_dict):
        name = data_dict.get('id')
        revision = data_dict.get('revision_id')
        self.requests.append(('package_show', name, revision))
        pages = self._pages.get(name)
        if not pages:
            raise NotFound('%s: no dataset %r' % (self.name, name))
        if revision is None:
            return copy.deepcopy(
                max(pages, key=lambda p: p['valid_revisions'][1]))
        for page in pages:
            lo, hi = page['valid_revisions']
            if lo <= revision <= hi:
                return copy.deepcopy(page)
        raise NotFound('%s: no page for %r at revision %d'
            % (self.name, name, revision))

    def _resource_content(self, data_dict):
        resource_id = data_dict.get('id')
        revision = data_dict.get('revision_id')
        self.requests.append(('resource_content', resource_id, revision))
        versions = self._content.get(resource_id)
        if not versions:
            raise NotFound('%s: no content for resource %r'
                % (self.name, resource_id))
        if revision is None:
            return {'id': resource_id, 'versions': copy.deepcopy(versions)}
        for version in versions:
            lo, hi = version['valid_revisions']
            if lo <= revision <= hi:
                return copy.deepcopy(version)
        raise NotFound('%s: no content for resource %r at revision %d'
            % (self.name, resource_id, revision))
