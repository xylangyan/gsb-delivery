import io
import json
import os
import subprocess
import sys

from scrapyd.app import application
from scrapyd.eggstorage import egg_fingerprint
from scrapyd.interfaces import IJobRegistry, IPoller
from scrapyd.jobregistry import JobRegistry
from scrapyd.launcher import ScrapyProcessProtocol
from tests import get_egg_data

PROJECT = "mybot"
VERSION = "42"


def schedule(root, request, spider, *, version=None, request_id=None, jobid=None, caller="caller-a"):
    args = {"project": PROJECT, "spider": spider}
    if version is not None:
        args["_version"] = version
    if request_id is not None:
        args["_request_id"] = request_id
    if jobid is not None:
        args["jobid"] = jobid
    request.args = {key.encode(): [value.encode()] for key, value in args.items()}
    request.requestHeaders.setRawHeaders(b"x-scrapyd-caller", [caller.encode()])
    request.method = "POST"
    return json.loads(root.children[b"schedule.json"].render(request))


def runner_list(chdir, fingerprint):
    env = os.environ.copy()
    env["SCRAPY_PROJECT"] = PROJECT
    env["SCRAPYD_EGG_FINGERPRINT"] = fingerprint
    completed = subprocess.run(
        [sys.executable, "-m", "scrapyd.runner", "list", "-s", "LOG_STDOUT=0"],
        cwd=chdir,
        env=env,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    return completed.stdout.decode().splitlines()


def test_bao_scenario(config, root, txrequest, chdir):  # noqa: PLR0915 end-to-end story deliberately linear
    # A publishes H1 (spider_old only) under the default version name 42.
    h1 = get_egg_data("bao_h1")
    root.eggstorage.put(io.BytesIO(h1), PROJECT, VERSION)
    root.update_projects()
    fp_h1 = egg_fingerprint(h1)

    # A accepts the default version while 42 points at H1.
    response_a = schedule(root, txrequest, "spider_old", request_id="req-a", jobid="job-a")
    assert response_a["status"] == "ok"
    assert response_a["version"] == VERSION
    assert response_a["egg_fingerprint"] == fp_h1

    # A second accepted job on H1, to prove the bytes survive until the LAST authorized job ends.
    response_a2 = schedule(root, txrequest, "spider_old", request_id="req-a2", jobid="job-a2")
    assert response_a2["egg_fingerprint"] == fp_h1

    # Someone republishes H2 (spider_new only) under the same version name 42.
    h2 = get_egg_data("bao_h2")
    root.eggstorage.put(io.BytesIO(h2), PROJECT, VERSION)
    root.update_projects()
    fp_h2 = egg_fingerprint(h2)
    assert fp_h1 != fp_h2

    # B accepts the default version now and must bind to H2, not A's H1.
    response_b = schedule(root, txrequest, "spider_new", request_id="req-b", jobid="job-b", caller="caller-b")
    assert response_b["egg_fingerprint"] == fp_h2

    # The republished 42 no longer contains spider_old, so it cannot be scheduled.
    refused = schedule(root, txrequest, "spider_old", request_id="req-late", jobid="job-late")
    assert refused["status"] == "error"
    assert refused["message"] == "spider 'spider_old' not found"

    # A late retry of A's request returns the original H1 binding.
    retry = schedule(root, txrequest, "spider_old", request_id="req-a", jobid="job-other")
    assert retry["jobid"] == "job-a"
    assert retry["egg_fingerprint"] == fp_h1

    # A request that switched to H2 cannot claim A's request ID: it gets A's original binding, no new job.
    impersonation = schedule(root, txrequest, "spider_new", request_id="req-a", jobid="job-fake")
    assert impersonation["jobid"] == "job-a"
    assert impersonation["egg_fingerprint"] == fp_h1
    assert root.jobregistry.get("job-fake") is None

    # An administrator withdraws the version name 42.
    txrequest.args = {b"project": [PROJECT.encode()], b"version": [VERSION.encode()]}
    txrequest.method = "POST"
    deleted = json.loads(root.children[b"delversion.json"].render(txrequest))
    assert deleted["status"] == "ok"
    assert root.eggstorage.list(PROJECT) == []

    # New requests can no longer enqueue against the withdrawn name 42.
    queue = root.poller.queues[PROJECT]
    before = queue.count()
    new_request = schedule(root, txrequest, "spider_new", request_id="req-new", jobid="job-new")
    assert new_request["status"] == "error"
    assert queue.count() == before
    assert root.jobregistry.get("job-new") is None

    # A and B still resolve their own exact bytes by fingerprint, verified through a real runner subprocess.
    assert runner_list(chdir, fp_h1) == ["spider_old"]
    assert runner_list(chdir, fp_h2) == ["spider_new"]

    # The runner refuses content whose identity does not match the accepted fingerprint.
    bad_env = os.environ.copy()
    bad_env["SCRAPY_PROJECT"] = PROJECT
    bad_env["SCRAPYD_EGG_FINGERPRINT"] = "0" * 64
    completed = subprocess.run(
        [sys.executable, "-m", "scrapyd.runner", "list"],
        cwd=chdir,
        env=bad_env,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "EggFingerprintError" in completed.stderr.decode()

    # A's job starts (start evidence persisted), then finishes. H1 bytes remain because A2 still holds a lease.
    root.poller.queues[PROJECT].remove(lambda message: message["_job"] == "job-a")  # dequeued at launch
    root.jobregistry.mark_running("job-a")
    process = ScrapyProcessProtocol(PROJECT, "spider_old", "job-a", env={}, args=[])
    root.launcher.processes[0] = process
    root.launcher._process_finished(process, 0)  # noqa: SLF001
    assert root.eggstorage.get_by_fingerprint(PROJECT, fp_h1)[1] is not None
    assert root.jobregistry.get("job-a")["state"] == JobRegistry.FINISHED

    # A daemon restart: jobs accepted but never started (A2 and B) lack start evidence and are held for coordination.
    restarted = application(config)
    registry = restarted.getComponent(IJobRegistry)
    assert registry.is_held("job-a2")
    assert registry.is_held("job-b")
    # A's job had real start evidence and already finished, so it is not touched by the reconciliation.
    assert registry.get("job-a")["state"] == JobRegistry.FINISHED
    assert registry.has_started("job-a")

    # The poller leaves the held jobs in the queue instead of launching them.
    poller = restarted.getComponent(IPoller)
    waiter = poller.next()
    held_count = poller.queues[PROJECT].count()
    poller.poll()
    assert poller.queues[PROJECT].count() == held_count
    assert {message["_job"] for message in poller.queues[PROJECT].list()} == {"job-a2", "job-b"}
    assert not waiter.called

    # status.json surfaces the held state for coordination.
    txrequest.args = {b"job": [b"job-a2"], b"project": [PROJECT.encode()]}
    txrequest.method = "GET"
    status = json.loads(root.children[b"status.json"].render(txrequest))
    assert status["currstate"] == "held"

    # Coordination: canceling the last H1 job releases its lease, after which H1 is finally cleaned up. H2 stays.
    txrequest.args = {b"project": [PROJECT.encode()], b"job": [b"job-a2"]}
    txrequest.method = "POST"
    cancel = json.loads(root.children[b"cancel.json"].render(txrequest))
    assert cancel["prevstate"] == "pending"
    assert root.eggstorage.get_by_fingerprint(PROJECT, fp_h1) == (None, None)
    assert root.eggstorage.get_by_fingerprint(PROJECT, fp_h2)[1] is not None
