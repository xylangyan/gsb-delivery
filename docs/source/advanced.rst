.. include:: global.rst

Advanced Topics
===============

.. _customizing-middleware:

Customizing Middleware
----------------------

Some of Dramatiq's built-in functionality is actually implemented by the :ref:`default-middleware`.

To customise the middleware Dramatiq uses, there are two typical approaches;

1. Adding extra Middleware
^^^^^^^^^^^^^^^^^^^^^^^^^^

If you wish to leave the :ref:`default-middleware` enabled with their default settings,
and only add extra middleware,
this is done with the |add_middleware| method of the Broker.
This can be used with the :ref:`optional-middleware`, or your own custom |Middleware| subclass.
For example;

.. code-block:: python

   from dramatiq import Broker
   from dramatiq.middleware import CurrentMessage

   broker = Broker()
   broker.add_middleware(CurrentMessage())

In this example the default middleware is unchanged, and an instance of the |CurrentMessage| middleware is added.

2. Providing a list of Middleware Classes
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

If you wish to have full control over the middleware (so the :ref:`default-middleware` is not used)
you must pass a list of Middleware when instantiating your |Broker|.
When you do so, Dramatiq will only use the middleware in the list you pass
(plus any middleware you add to the broker later with the |add_middleware| method).
The list can contain any of the :ref:`default-middleware` or :ref:`optional-middleware`,
or your own custom |Middleware| subclasses.

.. warning::
   Omitting from the list or changing the settings of default middleware can lead to behavior different to what is documented.

For example if you wish to

1. Disable the |Callbacks| and |Pipelines| middleware.
2. Set a custom default :ref:`max_retries<message-retries>` of 5 for the |Retries| middleware.
3. Add in the non-default |CurrentMessage| middleware.

you can do the following:

.. code-block:: python

   import dramatiq
   from dramatiq.middleware import AgeLimit, TimeLimit, ShutdownNotifications, Retries, CurrentMessage

   # Replace `dramatiq.Broker` with a concrete class, e.g. dramatiq.brokers.redis.RedisBroker.
   broker = dramatiq.Broker(
       middleware=[
           AgeLimit(),
           TimeLimit(),
           ShutdownNotifications(),
           # Note: custom default max_retries of 5
           Retries(max_retries=5),
           # Note: non-default middleware class included.
           CurrentMessage(),
           # Note: Callbacks and Pipelines are not included.
           # They will not be added back by dramatiq.
       ],
   )

   dramatiq.set_broker(broker)

Brokers
-------

Multi-tenancy
^^^^^^^^^^^^^

With Dramatiq you can run multiple logical apps on the same broker.
The way you do this is different for each broker, but fairly simple in
each case.

RabbitMQ
~~~~~~~~

RabbitMQ has the concept of `virtual hosts`_ built into it.  They
provide logical grouping and separation of resources.  You can create
virtual hosts using the `rabbitmqctl` command::

  $ rabbitmqctl add_vhost app1
  $ rabbitmqctl set_permissions -p app1 my_user ".*" ".*" ".*"

You can then pass that vhost to |RabbitmqBroker| when you instantiate it.

.. _virtual hosts:  https://www.rabbitmq.com/vhosts.html

Redis
~~~~~

The |RedisBroker| takes a ``namespace`` parameter that you can use to
logically split queues across multiple apps.


Highly Available Queues
^^^^^^^^^^^^^^^^^^^^^^^

RabbitMQ
~~~~~~~~

When running RabbitMQ with a `high availability cluster`_, you can
pass

* a list of dicts of connection parameters (that are passed as kwargs to :external+pika:class:`pika.ConnectionParameters`) or
* a connection URL with one or more targets split by ``;`` or
* a list connection URLs

to |RabbitmqBroker| when instantiating it.  This will make dramatiq
failover if the currently connected node fails.

.. code-block:: python

   from dramatiq.brokers.rabbitmq import RabbitmqBroker

   # Using a sequence of connection parameters:

   rabbitmq_broker = RabbitmqBroker(parameters=[
       dict(host='node1.foo.net'),
       dict(host='node2.foo.net'),
   ])

   # Using a single string containing multiple targets:

   rabbitmq_broker = RabbitmqBroker(
     url='amqp://node1.foo.net;ampq://node2.foo.net'
   )

   # Using a list of connection URLs:

   rabbitmq_broker = RabbitmqBroker(
     url=['amqp://node1.foo.net', 'ampq://node2.foo.net']
   )

.. _high availability cluster: https://www.rabbitmq.com/ha.html


Other brokers
^^^^^^^^^^^^^

Other broker implementations are available through third-party packages:

- `Amazon SQS`_ with `dramatiq_sqs`_.

.. _Amazon SQS: https://aws.amazon.com/sqs/
.. _dramatiq_sqs: https://github.com/Bogdanp/dramatiq_sqs


Messages
--------

Message Persistence
^^^^^^^^^^^^^^^^^^^

Messages sent to Dramatiq brokers are persisted to disk and survive
across broker reboots.  Exactly how often messages are flushed to disk
depends on your broker.

Messages that have been pulled by workers but not processed are
returned to the broker on graceful shutdown and any messages that are
in flight when a worker is terminated are going to be redelivered
later.  Messages are only ever acknowledged to (removed from) the
broker after they have been successfully processed.

When a worker goes down while processing messages (eg. due to
hardware, power or network failure) then the messages it pulled from
the broker will eventually be re-delivered to it (assuming it
recovers) or another worker.


Message Results
^^^^^^^^^^^^^^^

Dramatiq can store actor return values to Memcached and Redis by
leveraging the |Results| middleware.  In most cases you can get by
without needing this capability so the middleware is not turned on by
default.  When you do need it, however, it's there.

.. _message-interrupts:


Message Interrupts
^^^^^^^^^^^^^^^^^^

Dramatiq may interrupt message processing mid-execution.  This is
achieved by asynchronously raising an exception in the worker thread
that is currently processing the message.

.. attention::
   Currently, interrupts are only supported on CPython and are subject
   to the restrictions of the GIL.  This means the interrupt exception
   will only be raised the next time that thread acquires the GIL, and
   they are unable to cancel system calls.

Interrupts are used by the following middleware:

* |ShutdownNotifications| (raises |Shutdown|)
* |TimeLimit| (raises |TimeLimitExceeded|)

In order to gracefully handle interrupts, wrap the code in a try/except
block, catching the appropriate exception type.  To attempt to requeue
the message, raise an exception to indicate failure.

.. code-block:: python

   import dramatiq
   from dramatiq.middleware import Interrupt

   @dramatiq.actor(max_retries=3, notify_shutdown=True)
   def long_running_task():
       try:
           setup()
           do_work()
       except Shutdown:
           cleanup()
           raise


Accessing Messages from Within Actors
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Actors can access their own messages via the |CurrentMessage|
middleware.  This middleware is not enabled by default, but you can
add it to your broker when you instantiate it or by calling
|add_middleware|::

  from dramatiq.middleware import CurrentMessage


  broker.add_middleware(CurrentMessage())

With this middleware in place, every actor can access its own message
by calling |get_current_message| on the |CurrentMessage| class::

  @dramatiq.actor
  def example():
      print(CurrentMessage.get_current_message())


Enqueueing Messages from Other Languages
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

You can enqueue Dramatiq messages using any language that has bindings
to one of its brokers.  All you have to do is push a JSON-encoded
dictionary containing the following fields to your queue:

.. code-block:: javascript

   {
     "queue_name": "default",     // The name of the queue the message is being pushed on
     "actor_name": "add",         // The name of the actor that should handle this message
     "args": [1, 2],              // A list of positional arguments that are passed to the actor
     "kwargs": {},                // A dictionary of keyword arguments that are passed to the actor
     "options": {},               // Arbitrary options that are used by middleware. Leave this empty
     "message_id": "unique-id",   // A UUID4 value representing the message's unique id in the system
     "message_timestamp": 0,      // The UNIX timestamp in milliseconds representing when the message was first enqueued
   }

Using RabbitMQ
~~~~~~~~~~~~~~

Assuming you want to enqueue a message on a queue named ``default``,
publish a persistent message to that queue in RabbitMQ.

Using Redis
~~~~~~~~~~~

Add a field to the ``options`` dictionary of the message called
``redis_message_id`` that is a different UUID4 value than
``message_id``:

.. code-block:: javascript

   {
     ...
     "options": { "redis_message_id": "unique-id-2"},
     ...
   }

Assuming you want to enqueue a message on a queue named ``default``,
run::

  > HSET dramatiq:default.msgs $YOUR_REDIS_MESSAGE_ID $YOUR_MESSAGE_PAYLOAD
  > RPUSH dramatiq:default $YOUR_REDIS_MESSAGE_ID

``$YOUR_REDIS_MESSAGE_ID`` is the ``redis_message_id`` in the ``options``
field of the message payload.

Workers
-------

Worker Exit Codes
^^^^^^^^^^^^^^^^^

Dramatiq uses process exit codes to denote several scenarios:

=====  ========================================================================================
Code   Description
=====  ========================================================================================
``0``  Returned when the process exits gracefully.
``1``  Returned when the process is killed.
``2``  Returned when a module cannot be imported or when a command line argument is invalid.
``3``  Returned when a broker connection cannot be established during worker startup.
``4``  Returned when a PID file is set and Dramatiq is already running.
=====  ========================================================================================


Controlling Workers
^^^^^^^^^^^^^^^^^^^

The main Dramatiq process responds to several signals::

  $ kill -TERM [master-process-pid]

``INT`` and ``TERM``
~~~~~~~~~~~~~~~~~~~~

Sending an ``INT`` or ``TERM`` signal to the main process triggers
graceful shutdown.  Consumer threads will stop receiving new work and
worker threads will finish processing the work they have in flight
before shutting down.  Any tasks still in worker memory at this point
are re-queued on the broker.

If you send a second ``INT`` or ``TERM`` signal then the worker
processes will be killed immediately.

``HUP``
~~~~~~~

Sending ``HUP`` to the main process triggers a graceful shutdown
followed by a reload of the workers.  This is useful if you want to
reload code without completely restarting the main process.


Using gevent
^^^^^^^^^^^^

Dramatiq comes with a CLI utility called ``dramatiq-gevent`` that can
run workers under gevent_.  The following invocation would run 8
worker processes with 250 greenlets per process for a total of 2k
lightweight worker threads::

  $ dramatiq-gevent my_app -p 8 -t 250

If your tasks spend most of their time doing network IO and don't
depend on C extensions to execute those network calls then using
gevent could provide a significant performance improvement.

I suggest at least experimenting with it to see if it fits your use
case.

.. _prometheus-metrics:

Prometheus Metrics
^^^^^^^^^^^^^^^^^^

Prometheus metrics are automatically exported by workers whenever you
run them using the command line utility (assuming you're using the
Prometheus middleware).  By default, the exposition server listens on
port ``9191`` so you can tell Prometheus to scrape that or you can
specify what host and port it should listen on by setting the
``dramatiq_prom_host`` and ``dramatiq_prom_port`` environment
variables.

The following metrics are exported:

``dramatiq_messages_total``
  A *counter* for the total number of messages processed.

``dramatiq_message_errors_total``
  A *counter* for the total number of errored messages.

``dramatiq_message_retries_total``
  A *counter* for the total number of retried messages.

``dramatiq_message_rejects_total``
  A *counter* for the total number of dead-lettered messages.

``dramatiq_messages_inprogress``
  A *gauge* for the number of messages currently being processed.

``dramatiq_delayed_messages_inprogress``
  A *gauge* for the number of delayed messages currently in memory.

``dramatiq_message_duration_milliseconds``
  A *histogram* for the time spent processing messages.

All metrics define labels for ``queue_name`` and ``actor_name``.

Grafana Dashboard
~~~~~~~~~~~~~~~~~

You can find a Grafana dashboard that displays these metrics here_.

.. _here: https://grafana.com/grafana/dashboards/3692-dramatiq/

.. _gotchas-with-prometheus:

Gotchas with Prometheus
~~~~~~~~~~~~~~~~~~~~~~~

The Prometheus client for Python is a bit finicky when it comes to
exporting metrics from a multi-process configuration.  If your own app
uses Prometheus, then you should export ``prometheus_multiproc_dir``
and ``dramatiq_prom_db`` environment variables -- both pointing to an
existing folder -- and remove any files in that folder before running
Dramatiq.  For example::

  mkdir -p /tmp/dramatiq-prometheus \
    && rm -r /tmp/dramatiq-prometheus/* \
    && env prometheus_multiproc_dir=/tmp/dramatiq-prometheus \
           dramatiq_prom_db=/tmp/dramatiq-prometheus \
           dramatiq app

If you don't do this, then metrics will likely fail to export properly.

Workflows
---------

Nested Pipelines and Groups
^^^^^^^^^^^^^^^^^^^^^^^^^^^

Dramatiq supports running tasks in parallel via |group| and in
sequence via |pipeline|, but it does not provide a built-in way to run
a pipeline of groups out of the box. If you need more advanced
orchestration capabilities, such as nesting chains and groups of tasks
at arbitrary depth, you may find the dramatiq-workflow_ package
useful.

.. _dramatiq-workflow: https://github.com/Outset-AI/dramatiq-workflow


Versioned Pipelines
-------------------

Dramatiq's regular :class:`pipeline <dramatiq.pipeline>` stores the next
message inside ``pipe_target``.  That is convenient during normal operation,
but it makes rolling upgrades awkward: a message created before an upgrade
still points at the old actor and the old queue.  The opt-in
:mod:`dramatiq.migration` module describes stages, versions and live workers
so a run can move to a new route without losing results that were already
produced by the previous version.

The subsystem is entirely opt-in.  Pipelines that are not managed by a
:class:`~dramatiq.migration.MigrationPipeline` keep behaving exactly like
the built-in pipelines described in the cookbook.

Declaring stages and workers
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Stages are declared once and name the queues used by each version.  Workers
register the queue they consume together with the versions they support:

.. code-block:: python

   from dramatiq.migration import MigrationPipeline, MigrationRegistry

   registry = MigrationRegistry()
   registry.register_stage(
       "parse",
       versions=("v1", "v2"),
       queues={"v1": "parse-old", "v2": "parse-new"},
       actors={"v1": "parse_v1", "v2": "parse_v2"},
       successor="validate",
   )
   registry.register_stage(
       "validate",
       versions=("v1", "v2"),
       queues={"v1": "validate-old", "v2": "validate-new"},
       actors={"v1": "validate_v1", "v2": "validate_v2"},
   )
   pipeline = MigrationPipeline(registry=registry, first_stage="parse")

   registry.register_worker("parse-old", ("v1",), stage="parse")
   registry.register_worker("validate-old", ("v1",), stage="validate")

:meth:`~dramatiq.migration.MigrationRegistry.route` prefers the newest
version that a live worker declares.  An older implementation never accepts
a payload written by a newer one; newer implementations accept older payloads
unless a stage narrows that with ``accepts=``.

Run-scoped receipts
^^^^^^^^^^^^^^^^^^^

Results are committed as :class:`~dramatiq.migration.Receipt` objects keyed by
``(run_id, stage)``.  Commits are conditional:

* a newer version replaces an older receipt;
* an older version arriving later is rejected as ``stale_version`` and can
  never enqueue its successor;
* identical results are idempotent duplicates;
* callers may pass ``expected=`` a prior receipt for compare-and-set.

The default :class:`~dramatiq.migration.InMemoryReceiptStore` is suitable for
tests and a single process.  Production deployments should provide a
``ReceiptStore`` backed by shared storage that implements the same
compare-and-set contract.

Old results during a rollout
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

:meth:`~dramatiq.migration.MigrationPipeline.adopt_result` records a result
that an old worker already produced, and
:meth:`~dramatiq.migration.MigrationPipeline.reroute` (or ``switch_stage``)
routes an existing stage to a new version while reusing that result verbatim.
Explicit ``None`` values are preserved:

.. code-block:: python

   run = pipeline.start(10.01, run_id="R1", defaults={"currency": "CNY"})
   pipeline.adopt_result("R1", "parse", "v1", {"amount": 10.01, "currency": None})

   registry.register_worker("parse-new", ("v2",), stage="parse")
   registry.register_worker("validate-new", ("v2",), stage="validate")
   pipeline.reroute("R1", "validate", version="v2")

In-flight legacy chains
^^^^^^^^^^^^^^^^^^^^^^^

An old message that is still waiting in a queue already contains the
successor's actor, queue and arguments in ``pipe_target``.  Use
:func:`~dramatiq.migration.get_pipe_target` and
:func:`~dramatiq.migration.pipe_target_payload` to read that payload without
re-running the predecessor:

.. code-block:: python

   target = get_pipe_target(old_message)
   payload = pipe_target_payload(old_message)
   pipeline.enqueue("R1", "validate", payload, payload_version="v1")

Payload helpers
^^^^^^^^^^^^^^^

Use :func:`~dramatiq.migration.has_field`,
:func:`~dramatiq.migration.get_field`,
:func:`~dramatiq.migration.missing_fields` and
:func:`~dramatiq.migration.null_fields` to distinguish an explicit ``None``
from an absent field.  :func:`~dramatiq.migration.resolve_currency` (and the
more general ``resolve_default``) fills an absent field from the run's
``defaults`` but leaves an explicit ``None`` untouched.
:func:`~dramatiq.migration.yuan_to_cents` converts yuan to integer cents
using decimal arithmetic: ``10.01`` becomes ``1001``.

Pending migration
^^^^^^^^^^^^^^^^^

If no live worker can accept the next stage, routing marks that stage
``pending_migration`` instead of dropping the run or sending it to an old
queue.  Every upstream receipt stays in the store.  Once a compatible worker
registers, call :meth:`~dramatiq.migration.MigrationPipeline.resume` to
continue from the retained result.

Workers
^^^^^^^

:class:`~dramatiq.migration.MigrationMiddleware` bridges real actor messages
to the coordinator.  It registers its queues on boot, commits results through
the receipt store and only enqueues the successor when the commit succeeded.
Messages that carry no migration options are ignored.  The built-in
:class:`~dramatiq.middleware.Pipelines` middleware also refuses to follow
``pipe_target`` on messages that belong to a migration run, so a late old
worker can neither overwrite a new result nor dispatch into an old validate
queue.

A complete run
^^^^^^^^^^^^^^

The following sketch shows the two runs from a rollout: ``R1`` reuses a parse
result produced by the old worker, while ``R2`` is reprocessed by the new
worker because its old parse has not finished yet.

.. code-block:: python

   registry.register_worker("parse-old", ("v1",), stage="parse")
   registry.register_worker("validate-old", ("v1",), stage="validate")

   r1 = pipeline.start(10.01, run_id="R1", defaults={"currency": "CNY"})
   pipeline.adopt_result("R1", "parse", "v1", {"amount": 10.01, "currency": None})

   r2 = pipeline.start(10.01, run_id="R2", defaults={"currency": "CNY"})
   # R2's old parse is still queued; the rollout then registers v2 workers.

   registry.register_worker("parse-new", ("v2",), stage="parse")
   registry.register_worker("validate-new", ("v2",), stage="validate")

   pipeline.reroute("R1", "validate", version="v2")
   pipeline.reroute("R2", "parse", version="v2")

   # The new parse implementation:
   payload = {"amount_cents": yuan_to_cents(10.01)}
   payload["currency"] = resolve_currency(payload, run=pipeline.get_run("R2"))
   pipeline.submit_result("R2", "parse", "v2", payload)

   # A late result from the old worker is rejected and cannot fan out.
   late = pipeline.submit_result("R2", "parse", "v1", {"amount": 10.01, "currency": None})
   assert late.stale and late.dispatch is None
