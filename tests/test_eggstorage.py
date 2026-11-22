import io
from contextlib import closing
from pathlib import Path

import pytest
from zope.interface import implementer
from zope.interface.verify import verifyObject

from scrapyd.app import application
from scrapyd.config import Config
from scrapyd.eggstorage import FilesystemEggStorage, egg_fingerprint, sorted_versions
from scrapyd.exceptions import DirectoryTraversalError, EggNotFoundError, ProjectNotFoundError
from scrapyd.interfaces import IEggStorage


@implementer(IEggStorage)
class MockEggStorage:
    def __init__(self, config):
        self.config = config

    def put(self, eggfile, project, version):
        pass

    def get(self, project, version=None):
        pass

    def list(self, project):
        pass

    def list_projects(self):
        return ["hello_world"]

    def delete(self, project, version=None):
        pass


@pytest.fixture
def eggstorage(tmpdir):
    return FilesystemEggStorage(Config(values={"eggs_dir": tmpdir}))


@pytest.mark.parametrize(
    ("versions", "expected"),
    [
        # letter
        (["zzz", "b", "ddd", "a", "x"], ["a", "b", "ddd", "x", "zzz"]),
        # number
        (["10", "1", "9"], ["1", "9", "10"]),
        # "r" number
        (["r10", "r1", "r9"], ["r1", "r10", "r9"]),
        # version
        (["2.11", "2.01", "2.9"], ["2.01", "2.9", "2.11"]),
        # number and letter
        (["123456789", "b3b8fd2"], ["123456789", "b3b8fd2"]),
    ],
)
def test_sorted_versions(versions, expected):
    assert sorted_versions(versions) == expected


def test_config(chdir):
    config = Config()
    config.cp.set("scrapyd", "eggstorage", "tests.test_eggstorage.MockEggStorage")

    app = application(config)
    eggstorage = app.getComponent(IEggStorage)

    assert isinstance(eggstorage, MockEggStorage)
    assert eggstorage.list_projects() == ["hello_world"]


def test_interface(eggstorage):
    verifyObject(IEggStorage, eggstorage)


def test_put_secure(eggstorage):
    with pytest.raises(DirectoryTraversalError) as exc:
        eggstorage.put(io.BytesIO(b"data"), "../p", "v")  # version is sanitized

    assert str(exc.value) == "../p"


def test_get_secure(eggstorage):
    with pytest.raises(DirectoryTraversalError) as exc:
        eggstorage.get("../p", "v")  # version is sanitized

    assert str(exc.value) == "../p"


def test_list_secure_join(eggstorage):
    with pytest.raises(DirectoryTraversalError) as exc:
        eggstorage.list("../p")

    assert str(exc.value) == "../p"


def test_list_secure_glob(eggstorage):
    eggstorage.put(io.BytesIO(b"data"), "mybot", "01")

    assert eggstorage.list("*") == []  # ["01"] if * weren't escaped


def test_delete_secure(eggstorage):
    with pytest.raises(DirectoryTraversalError) as exc:
        eggstorage.delete("../p", "v")  # version is sanitized

    assert str(exc.value) == "../p"


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        (None, (None, None)),
        ("nonexistent", (None, None)),
        ("01", (None, None)),
    ],
)
def test_get_empty(eggstorage, version, expected):
    assert eggstorage.get("mybot", version) == expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        (None, ("03", b"egg03")),
        ("nonexistent", (None, None)),
        ("01", ("01", b"egg01")),
    ],
)
def test_get_many(eggstorage, version, expected):
    eggstorage.put(io.BytesIO(b"egg01"), "mybot", "01")
    eggstorage.put(io.BytesIO(b"egg03"), "mybot", "03")
    eggstorage.put(io.BytesIO(b"egg02"), "mybot", "02")

    version, data = eggstorage.get("mybot", version)
    if data is not None:
        with closing(data):
            data = data.read()

    assert (version, data) == expected


@pytest.mark.parametrize(
    ("versions", "expected"),
    [(["ddd", "abc", "bcaa"], ["abc", "bcaa", "ddd"]), (["9", "2", "200", "3", "4"], ["2", "3", "4", "9", "200"])],
)
def test_list(eggstorage, versions, expected):
    assert eggstorage.list("mybot") == []

    for version in versions:
        eggstorage.put(io.BytesIO(b"egg01"), "mybot", version)

    assert eggstorage.list("mybot") == expected


def test_list_glob(eggstorage):
    directory = Path(eggstorage.basedir) / "mybot"
    directory.mkdir(parents=True)
    (directory / "other").write_bytes(b"")

    assert eggstorage.list("mybot") == []  # "other" without "*.egg" glob


def test_list_projects(eggstorage):
    (Path(eggstorage.basedir) / "other").write_bytes(b"")

    assert eggstorage.list_projects() == []  # "other" without isdir() filter

    eggstorage.put(io.BytesIO(b"egg01"), "mybot", "01")

    assert eggstorage.list_projects() == ["mybot"]


def test_delete_project(eggstorage):
    eggstorage.put(io.BytesIO(b"egg01"), "mybot", "01")
    eggstorage.put(io.BytesIO(b"egg03"), "mybot", "03")
    eggstorage.put(io.BytesIO(b"egg02"), "mybot", "02")

    assert eggstorage.list("mybot") == ["01", "02", "03"]

    eggstorage.delete("mybot")

    assert eggstorage.list("mybot") == []


def test_delete_vesrion(eggstorage):
    eggstorage.put(io.BytesIO(b"egg01"), "mybot", "01")
    eggstorage.put(io.BytesIO(b"egg03"), "mybot", "03")
    eggstorage.put(io.BytesIO(b"egg02"), "mybot", "02")

    assert eggstorage.list("mybot") == ["01", "02", "03"]

    eggstorage.delete("mybot", "02")

    assert eggstorage.list("mybot") == ["01", "03"]

    eggstorage.delete("mybot", "03")

    assert eggstorage.list("mybot") == ["01"]

    eggstorage.delete("mybot", "01")

    assert eggstorage.list("mybot") == []
    assert not (Path(eggstorage.basedir) / "mybot").exists()


def test_delete_nonexistent_project(eggstorage):
    with pytest.raises(ProjectNotFoundError):
        eggstorage.delete("mybot")


def test_delete_nonexistent_version(eggstorage):
    with pytest.raises(EggNotFoundError):
        eggstorage.delete("mybot", "01")

    eggstorage.put(io.BytesIO(b"egg01"), "mybot", "01")

    with pytest.raises(EggNotFoundError):
        eggstorage.delete("mybot", "02")


def test_fingerprint(eggstorage):
    eggstorage.put(io.BytesIO(b"egg01"), "mybot", "01")

    assert eggstorage.fingerprint("mybot", "01") == egg_fingerprint(b"egg01")


def test_acquire_retains_overwritten_version(eggstorage):
    eggstorage.put(io.BytesIO(b"egg-h1"), "mybot", "42")
    h1_fingerprint = eggstorage.fingerprint("mybot", "42")
    eggstorage.acquire("mybot", "42", h1_fingerprint)

    # Republishing the same version name while the lease is active.
    eggstorage.put(io.BytesIO(b"egg-h2"), "mybot", "42")
    h2_fingerprint = eggstorage.fingerprint("mybot", "42")

    assert h1_fingerprint != h2_fingerprint
    version, retained = eggstorage.get_by_fingerprint("mybot", h1_fingerprint)
    with closing(retained):
        assert (version, retained.read()) == ("42", b"egg-h1")

    eggstorage.release("mybot", h1_fingerprint)
    assert eggstorage.get_by_fingerprint("mybot", h1_fingerprint) == (None, None)


def test_acquire_retains_deleted_version_and_blocks_name_lookup(eggstorage):
    eggstorage.put(io.BytesIO(b"egg-h1"), "mybot", "42")
    h1_fingerprint = eggstorage.fingerprint("mybot", "42")
    eggstorage.acquire("mybot", "42", h1_fingerprint)

    eggstorage.delete("mybot", "42")

    # A new request cannot bind to the withdrawn version name, nor see it in the version list.
    assert eggstorage.get("mybot", "42") == (None, None)
    assert eggstorage.list("mybot") == []
    # An accepted job still retrieves its exact content by fingerprint.
    version, retained = eggstorage.get_by_fingerprint("mybot", h1_fingerprint)
    with closing(retained):
        assert (version, retained.read()) == ("42", b"egg-h1")

    eggstorage.release("mybot", h1_fingerprint)
    assert not (Path(eggstorage.basedir) / "mybot").exists()


def test_delete_without_lease_removes_bytes_immediately(eggstorage):
    eggstorage.put(io.BytesIO(b"egg-h1"), "mybot", "42")
    fingerprint = eggstorage.fingerprint("mybot", "42")

    eggstorage.delete("mybot", "42")

    assert eggstorage.get_by_fingerprint("mybot", fingerprint) == (None, None)
    assert not (Path(eggstorage.basedir) / "mybot").exists()


def test_release_keeps_egg_until_last_job(eggstorage):
    eggstorage.put(io.BytesIO(b"egg-h1"), "mybot", "42")
    fingerprint = eggstorage.fingerprint("mybot", "42")
    eggstorage.acquire("mybot", "42", fingerprint)
    eggstorage.acquire("mybot", "42", fingerprint)
    eggstorage.delete("mybot", "42")

    eggstorage.release("mybot", fingerprint)
    assert eggstorage.get_by_fingerprint("mybot", fingerprint)[1] is not None

    eggstorage.release("mybot", fingerprint)
    assert eggstorage.get_by_fingerprint("mybot", fingerprint) == (None, None)


def test_delete_project_retains_leased_eggs(eggstorage):
    eggstorage.put(io.BytesIO(b"egg-h1"), "mybot", "42")
    fingerprint = eggstorage.fingerprint("mybot", "42")
    eggstorage.acquire("mybot", "42", fingerprint)

    eggstorage.delete("mybot")

    assert eggstorage.list("mybot") == []
    version, retained = eggstorage.get_by_fingerprint("mybot", fingerprint)
    with closing(retained):
        assert (version, retained.read()) == ("42", b"egg-h1")

    eggstorage.release("mybot", fingerprint)
    assert not (Path(eggstorage.basedir) / "mybot").exists()


def test_delete_project_after_republication_keeps_both_contents(eggstorage):
    eggstorage.put(io.BytesIO(b"egg-h1"), "mybot", "42")
    fp_h1 = eggstorage.fingerprint("mybot", "42")
    eggstorage.acquire("mybot", "42", fp_h1)

    eggstorage.put(io.BytesIO(b"egg-h2"), "mybot", "42")
    fp_h2 = eggstorage.fingerprint("mybot", "42")
    eggstorage.acquire("mybot", "42", fp_h2)

    eggstorage.delete("mybot")

    _, h1 = eggstorage.get_by_fingerprint("mybot", fp_h1)
    _, h2 = eggstorage.get_by_fingerprint("mybot", fp_h2)
    with closing(h1), closing(h2):
        assert h1.read() == b"egg-h1"
        assert h2.read() == b"egg-h2"

    eggstorage.release("mybot", fp_h1)
    eggstorage.release("mybot", fp_h2)
    assert not (Path(eggstorage.basedir) / "mybot").exists()


def test_acquire_unknown_egg(eggstorage):
    with pytest.raises(EggNotFoundError):
        eggstorage.acquire("mybot", "42", "missing")
