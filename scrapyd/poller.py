from twisted.internet.defer import DeferredQueue, inlineCallbacks, maybeDeferred
from zope.interface import implementer

from scrapyd.interfaces import IPoller
from scrapyd.utils import get_spider_queues, initialize_component


@implementer(IPoller)
class QueuePoller:
    def __init__(self, config):
        self.config = config
        self.update_projects()
        self.dq = DeferredQueue()
        self.registry = initialize_component(config, "jobregistry", "scrapyd.jobregistry.JobRegistry")

    @inlineCallbacks
    def poll(self):
        for project, queue in self.queues.items():
            while (yield maybeDeferred(queue.count)):
                # If the "waiting" backlog is empty (that is, if the maximum number of Scrapy processes are running):
                if not self.dq.waiting:
                    return
                messages = queue.list()
                head = messages[0]
                job_id = head.get("_job")
                if job_id is not None and self.registry.is_held(job_id):
                    # Skip jobs held after a restart without start evidence, leaving them in the queue for coordination.
                    launchable = next(
                        (message for message in messages if not self.registry.is_held(message.get("_job"))),
                        None,
                    )
                    if launchable is None:
                        break
                    queue.remove(lambda message, target=launchable["_job"]: message.get("_job") == target)
                    message = launchable.copy()
                else:
                    message = yield maybeDeferred(queue.pop)
                    # The message can be None if, for example, two Scrapyd instances share a spider queue database.
                    if message is None:
                        continue
                message["_project"] = project
                message["_spider"] = message.pop("name")
                # Pop a dummy item from the "waiting" backlog. and fire the message's callbacks.
                self.dq.put(message)

    def next(self):
        """
        Add a dummy item to the "waiting" backlog (based on Twisted's implementation of DeferredQueue).
        """
        return self.dq.get()

    def update_projects(self):
        self.queues = get_spider_queues(self.config)
