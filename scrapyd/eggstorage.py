import hashlib
import json
import re
import shutil
from glob import escape
from pathlib import Path

from packaging.version import InvalidVersion, Version
from twisted.python import filepath
from zope.interface import implementer

from scrapyd.exceptions import DirectoryTraversalError, EggNotFoundError, ProjectNotFoundError
from scrapyd.interfaces import IEggStorage


def sorted_versions(versions):
    try:
        return sorted(versions, key=Version)
    except InvalidVersion:
        return sorted(versions)


def egg_fingerprint(data):
    """Return the SHA-256 fingerprint of an egg's bytes."""
    return hashlib.sha256(data).hexdigest()


@implementer(IEggStorage)
class FilesystemEggStorage:
    def __init__(self, config):
        self.basedir = config.get("eggs_dir", "eggs")

    def put(self, eggfile, project, version):
        path = self._egg_path(project, version)

        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)

        # Retain the previous bytes if jobs are still allowed to use them, so that republishing a version name cannot
        # change the content of an already accepted job.
        if path.exists():
            fingerprint = self.fingerprint(project, version)
            if self._lease_count(project, fingerprint):
                self._retain(project, fingerprint, path)

        with path.open("wb") as f:
            shutil.copyfileobj(eggfile, f)

    def get(self, project, version=None):
        if version is None:
            try:
                version = self.list(project)[-1]
            except IndexError:
                return None, None
        try:
            return version, self._egg_path(project, version).open("rb")
        except FileNotFoundError:
            # Retained eggs are only reachable through get_by_fingerprint(), so a new request cannot bind to a
            # version name that has been withdrawn.
            return None, None

    def get_by_fingerprint(self, project, fingerprint):
        """Return ``(version, file)`` for the egg with the fingerprint, looking up retained eggs first."""
        retained = self._lease_dir(project) / f"{fingerprint}.egg"
        if retained.exists():
            entry = self._read_leases(project).get(fingerprint)
            version = entry["version"] if entry else fingerprint
            return version, retained.open("rb")

        for version in self.list(project):
            path = self._egg_path(project, version)
            if self._hash(path) == fingerprint:
                return version, path.open("rb")
        return None, None

    def fingerprint(self, project, version):
        """Return the SHA-256 fingerprint of the stored egg."""
        return self._hash(self._egg_path(project, version))

    def acquire(self, project, version, fingerprint):
        """Register that a job is allowed to use the egg's bytes, retaining them across deletion or republication."""
        retained = self._lease_dir(project) / f"{fingerprint}.egg"
        if not self._egg_path(project, version).exists() and not retained.exists():
            raise EggNotFoundError(f"{project}/{version}")

        leases = self._read_leases(project)
        entry = leases.setdefault(fingerprint, {"version": version, "count": 0})
        entry["count"] += 1
        self._write_leases(project, leases)

    def release(self, project, fingerprint):
        """Deregister a job's use of the egg's bytes, deleting the bytes when no job remains."""
        leases = self._read_leases(project)
        entry = leases.get(fingerprint)
        if entry is None:
            return
        entry["count"] -= 1
        if entry["count"] <= 0:
            del leases[fingerprint]
            retained = self._lease_dir(project) / f"{fingerprint}.egg"
            retained.unlink(missing_ok=True)
        if leases:
            self._write_leases(project, leases)
        else:
            self._leases_path(project).unlink(missing_ok=True)
        self._cleanup_state_dir(project)

    def active_fingerprints(self, project):
        """Return the fingerprints still allowed to be used by accepted jobs."""
        return set(self._read_leases(project))

    def list(self, project):
        return sorted_versions([path.stem for path in self._get_path(escape(project)).glob("*.egg")])

    def list_projects(self):
        basedir_path = Path(self.basedir)
        if basedir_path.exists():
            return [path.name for path in basedir_path.iterdir() if path.is_dir()]
        return []

    def delete(self, project, version=None):
        if version is None:
            try:
                for fingerprint in self.active_fingerprints(project):
                    entry = self._read_leases(project)[fingerprint]
                    self._retain(project, fingerprint, self._egg_path(project, entry["version"]))
                if not self.active_fingerprints(project):
                    shutil.rmtree(self._get_path(project))
                else:
                    for path in self._get_path(project).glob("*.egg"):
                        path.unlink()
            except FileNotFoundError as e:
                raise ProjectNotFoundError from e
        else:
            try:
                path = self._egg_path(project, version)
                fingerprint = self.fingerprint(project, version)
                if self._lease_count(project, fingerprint):
                    self._retain(project, fingerprint, path)
                else:
                    path.unlink()
                if not self.list(project) and not self.active_fingerprints(project):
                    self.delete(project)
            except FileNotFoundError as e:
                raise EggNotFoundError from e

    def _retain(self, project, fingerprint, path):
        destination = self._lease_dir(project) / f"{fingerprint}.egg"
        if destination.exists():
            return
        # Move only the bytes that actually match the fingerprint. A version name can point to different bytes than
        # the ones originally accepted (for example, after a republication), in which case the bytes are already
        # retained elsewhere.
        if path.exists() and self._hash(path) == fingerprint:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))

    def _lease_count(self, project, fingerprint):
        entry = self._read_leases(project).get(fingerprint)
        return entry["count"] if entry else 0

    def _lease_dir(self, project):
        return self._get_path(project, ".scrapyd", "leases")

    def _leases_path(self, project):
        return self._get_path(project, ".scrapyd", "leases.json")

    def _read_leases(self, project):
        path = self._leases_path(project)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}

    def _write_leases(self, project, leases):
        state_dir = self._get_path(project, ".scrapyd")
        state_dir.mkdir(parents=True, exist_ok=True)
        self._leases_path(project).write_text(json.dumps(leases, sort_keys=True))

    def _cleanup_state_dir(self, project):
        state_dir = self._get_path(project, ".scrapyd")
        lease_dir = state_dir / "leases"
        if lease_dir.exists() and not any(lease_dir.iterdir()):
            lease_dir.rmdir()
        if state_dir.exists() and not any(state_dir.iterdir()):
            state_dir.rmdir()
        project_dir = self._get_path(project)
        if project_dir.exists() and not any(project_dir.iterdir()):
            project_dir.rmdir()

    @staticmethod
    def _hash(path):
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _egg_path(self, project, version):
        sanitized_version = re.sub(r"[^A-Za-z0-9_-]", "_", version)
        return self._get_path(project, f"{sanitized_version}.egg")

    def _get_path(self, project, *trusted):
        try:
            file = filepath.FilePath(self.basedir).child(project)
        except filepath.InsecurePath as e:
            raise DirectoryTraversalError(project) from e

        return Path(file.path) / Path(*trusted)
