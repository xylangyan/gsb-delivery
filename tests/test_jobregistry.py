from zope.interface.verify import verifyObject

from scrapyd.config import Config
from scrapyd.interfaces import IJobRegistry
from scrapyd.jobregistry import JobRegistry


def make_registry(tmpdir):
    return JobRegistry(Config(values={"dbs_dir": str(tmpdir)}))


def binding(job_id="j1", request_id="r1", caller="caller-a"):
    return {
        "job_id": job_id,
        "project": "mybot",
        "spider": "spider_old",
        "version": "42",
        "fingerprint": "fp-h1",
        "caller": caller,
        "request_id": request_id,
        "response": {"jobid": job_id, "version": "42", "egg_fingerprint": "fp-h1"},
    }


def test_interface(tmpdir):
    verifyObject(IJobRegistry, make_registry(tmpdir))


def test_bind_and_get(tmpdir):
    registry = make_registry(tmpdir)

    assert registry.bind(binding())
    stored = registry.get("j1")

    assert stored["project"] == "mybot"
    assert stored["state"] == JobRegistry.PENDING
    assert stored["started_at"] is None
    assert stored["response"]["jobid"] == "j1"


def test_bind_duplicate_job_id(tmpdir):
    registry = make_registry(tmpdir)

    assert registry.bind(binding())
    assert not registry.bind(binding())


def test_get_by_request(tmpdir):
    registry = make_registry(tmpdir)
    registry.bind(binding())

    assert registry.get_by_request("caller-a", "r1")["job_id"] == "j1"
    assert registry.get_by_request("caller-a", "other") is None
    assert registry.get_by_request("caller-b", "r1") is None


def test_mark_running_sets_start_evidence(tmpdir):
    registry = make_registry(tmpdir)
    registry.bind(binding())

    assert not registry.has_started("j1")
    registry.mark_running("j1")

    assert registry.has_started("j1")
    assert registry.get("j1")["state"] == JobRegistry.RUNNING


def test_mark_finished_keeps_binding(tmpdir):
    registry = make_registry(tmpdir)
    registry.bind(binding())
    registry.mark_finished("j1")

    assert registry.get("j1")["state"] == JobRegistry.FINISHED


def test_release(tmpdir):
    registry = make_registry(tmpdir)
    registry.bind(binding())

    assert registry.release("j1")["job_id"] == "j1"
    assert registry.release("j1") is None
    assert registry.get("j1") is None


def test_restart_holds_pending_without_start_evidence(tmpdir):
    first = make_registry(tmpdir)
    first.bind(binding("started", "r-started"))
    first.mark_running("started")
    first.bind(binding("never-started", "r-pending"))

    # A new registry instance models a daemon restart over the same database.
    restarted = make_registry(tmpdir)

    assert restarted.is_held("never-started")
    assert restarted.get("never-started")["state"] == JobRegistry.HELD
    assert not restarted.is_held("started")
    assert restarted.get("started")["state"] == JobRegistry.RUNNING
    assert restarted.has_started("started")


def test_list_filtered_by_state(tmpdir):
    registry = make_registry(tmpdir)
    registry.bind(binding("j1", "r1"))
    registry.bind(binding("j2", "r2"))
    registry.mark_held("j2")

    assert {row["job_id"] for row in registry.list()} == {"j1", "j2"}
    assert [row["job_id"] for row in registry.list(JobRegistry.HELD)] == ["j2"]
