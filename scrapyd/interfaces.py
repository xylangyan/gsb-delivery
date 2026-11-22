from zope.interface import Attribute, Interface


class IEggStorage(Interface):
    """
    A component to store project eggs.
    """

    def put(eggfile, project, version):
        """
        Store the egg (a file object), which represents a ``version`` of the ``project``.
        """

    def get(project, version=None):
        """
        Return ``(version, file)`` for the egg matching the ``project`` and ``version``.

        If ``version`` is ``None``, the latest version and corresponding file are returned.

        If no egg is found, ``(None, None)`` is returned.

        .. tip:: Remember to close the ``file`` when done.
        """

    def get_by_fingerprint(project, fingerprint):
        """
        Return ``(version, file)`` for the egg whose content matches ``fingerprint``.

        Retained eggs (whose version name was withdrawn or republished while jobs were still allowed to use them) are
        considered. If no egg is found, ``(None, None)`` is returned.

        .. tip:: Remember to close the ``file`` when done.
        """

    def fingerprint(project, version):
        """
        Return the content fingerprint (SHA-256 hex digest) of the egg matching the ``project`` and ``version``.
        """

    def acquire(project, version, fingerprint):
        """
        Register that a job is allowed to use the egg's content, retaining it across deletion or republication.
        """

    def release(project, fingerprint):
        """
        Deregister a job's use of the egg's content, deleting the retained content when no job remains.
        """

    def active_fingerprints(project):
        """
        Return the content fingerprints still allowed to be used by accepted jobs.
        """

    def list(project):
        """
        Return all versions of the ``project`` in order, with the latest version last.
        """

    def list_projects():
        """
        Return all projects in storage.

        .. versionadded:: 1.3.0
           Move this logic into the interface and its implementations, to allow customization.
        """

    def delete(project, version=None):
        """
        Delete the egg matching the ``project`` and ``version``. Delete the ``project``, if no versions remains.
        """


class IPoller(Interface):
    """
    A component that tracks capacity for new jobs, and starts jobs when ready.
    """

    queues = Attribute(
        """
        An object (like a ``dict``) with a ``__getitem__`` method that accepts a project's name and returns its
        :py:interface:`spider queue<scrapyd.interfaces.ISpiderQueue>` of pending jobs.
        """
    )

    def poll():
        """
        Called periodically to start jobs if there's capacity.
        """

    def next():
        """
        Return the next pending job.

        It should return a Deferred that will be fired when there's capacity, or already fired if there's capacity.

        The pending job is a ``dict`` containing at least the ``_project`` name, ``_spider`` name and ``_job`` ID.
        The job ID is unique, at least within the project.

        The pending job is later passed to :meth:`scrapyd.interfaces.IEnvironment.get_environment`.

        .. seealso:: :meth:`scrapyd.interfaces.ISpiderQueue.pop`
        """

    def update_projects():
        """
        Called when projects may have changed, to refresh the available projects, including at initialization.
        """


class ISpiderQueue(Interface):
    """
    A component to store pending jobs.

    The ``dict`` keys used by the chosen ``ISpiderQueue`` implementation must match the chosen:

    -  :ref:`launcher` service (which calls :meth:`scrapyd.interfaces.IPoller.next`)
    -  :py:interface:`~scrapyd.interfaces.IEnvironment` implementation (see :meth:`scrapyd.interfaces.IPoller.next`)
    -  :ref:`webservices<config-services>` that schedule, cancel or list pending jobs
    """

    def add(name, priority, **spider_args):
        """
        Add a pending job, given the spider ``name``, crawl ``priority`` and keyword arguments, which might include the
        ``_job`` ID, egg ``_version`` and Scrapy ``settings`` depending on the implementation, with keyword arguments
        that are not recognized by the implementation being treated as spider arguments.

        .. versionchanged:: 1.3.0
           Add the ``priority`` parameter.
        """

    def pop():
        """
        Pop the next pending job. The pending job is a ``dict`` containing the spider ``name``. Depending on the
        implementation, other keys might include the ``_job`` ID, egg ``_version`` and Scrapy ``settings``, with
        keyword arguments that are not recognized by the receiver being treated as spider arguments.
        """

    def list():
        """
        Return the pending jobs.

        .. seealso:: :meth:`scrapyd.interfaces.ISpiderQueue.pop`
        """

    def count():
        """
        Return the number of pending jobs.
        """

    def remove(func):
        """
        Remove pending jobs for which ``func(job)`` is true, and return the number of removed pending jobss.
        """

    def clear():
        """
        Remove all pending jobs.
        """


class ISpiderScheduler(Interface):
    """
    A component to schedule jobs.
    """

    def schedule(project, spider_name, priority, **spider_args):
        """
        Schedule a crawl.

        .. versionchanged:: 1.3.0
           Add the ``priority`` parameter.
        """

    def list_projects():
        """
        Return all projects that can be scheduled.
        """

    def update_projects():
        """
        Called when projects may have changed, to refresh the available projects, including at initialization.
        """


class IEnvironment(Interface):
    """
    A component to generate the environment of jobs.

    The chosen ``IEnvironment`` implementation must match the chosen :ref:`launcher` service.
    """

    def get_settings(message):
        """
        Return the Scrapy settings to use for running the process.

        Depending on the chosen :ref:`launcher`, this would be one of more ``LOG_FILE`` or ``FEEDS``.

        .. versionadded:: 1.4.2
           Support for overriding Scrapy settings via ``SCRAPY_`` environment variables was removed in Scrapy 2.8.

        :param message: the pending job received from the :meth:`scrapyd.interfaces.IPoller.next` method
        """

    def get_environment(message, slot):
        """
        Return the environment variables to use for running the process.

        Depending on the chosen :ref:`launcher`, this would be one of more of ``SCRAPY_PROJECT``,
        ``SCRAPYD_EGG_VERSION`` or ``SCRAPY_SETTINGS_MODULE``.

        :param message: the pending job received from the :meth:`scrapyd.interfaces.IPoller.next` method
        :param slot: the :ref:`launcher` slot for tracking the process
        """


class IJobStorage(Interface):
    """
    A component to store finished jobs.

    .. versionadded:: 1.3.0
    """

    def add(job):
        """
        Add a finished job in the storage.
        """

    def list():
        """
        Return the finished jobs.

        .. seealso:: :meth:`scrapyd.interfaces.IJobStorage.__iter__`
        """

    def __len__():
        """
        Return the number of finished jobs.
        """

    def __iter__():
        """
        Iterate over the finished jobs in reverse order by ``end_time``.

        A job has the attributes ``project``, ``spider``, ``job``, ``start_time`` and ``end_time`` and may have the
        attributes ``args`` (``scrapy crawl`` CLI arguments) and ``env`` (environment variables).
        """


class IJobRegistry(Interface):
    """
    A component that persists the egg content bound to accepted jobs, across daemon restarts.
    """

    def bind(binding):
        """
        Record a newly accepted job's project, spider, resolved version, content fingerprint, caller request ID and
        the response to return for retries of the same request.
        """

    def get(job_id):
        """
        Return the binding for the job ID, or ``None`` if the job is not registered.
        """

    def get_by_request(caller, request_id):
        """
        Return the binding previously created by the caller for the request ID, or ``None`` if none exists.
        """

    def mark_running(job_id):
        """
        Record the evidence that the job's process started.
        """

    def mark_held(job_id):
        """
        Mark a job without start evidence after a daemon restart as held, pending manual coordination.
        """

    def is_held(job_id):
        """
        Return whether the job is held because it has no start evidence after a daemon restart.
        """

    def mark_finished(job_id):
        """
        Record that the job's process ended.
        """

    def release(job_id):
        """
        Remove the job's registration and return the removed binding, or ``None`` if it was not registered.
        """

    def list(state=None):
        """
        Return the registered bindings, optionally filtered by their state.
        """

    def has_started(job_id):
        """
        Return whether evidence exists that the job's process started.
        """
