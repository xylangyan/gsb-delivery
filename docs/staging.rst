.. _staging:

Staging review queue
====================

The :mod:`redbird.staging` module implements a two-layer staging
area to inspect rows before they are ingested:

- a **draft layer**: a :class:`redbird.repos.csv.CSVFileRepo` with
  unreviewed CSV draft rows
- a **verified layer**: a :class:`redbird.repos.sqlalchemy.SQLRepo`
  (SQLite) with reviewed records. Any record in this layer,
  including revoked ones, shadows the draft carrying the same
  business key.

Rows are served round-robin by account: an account consumes its
turn only when a row is actually yielded for it. Within an account,
rows go from high to low priority; accounts rotate in registration
order.

Cursors are persisted in a small SQLite control database with an
agreed retention period, so review sessions survive process
restarts. Registering a batch builds a reusable index of the draft
rows, hence neither the first page nor page turns rescan the CSV
file to determine the next account. Both layers stay ordinary
repositories: explicit backend filtering (``repo.filter_by(...)``)
keeps working unchanged.

.. code-block:: python

    from redbird.repos import CSVFileRepo, SQLRepo
    from redbird.staging import StagingQueue

    drafts = CSVFileRepo(filename="drafts.csv", model=Draft)
    verified = SQLRepo(model=Verified, table="verified",
                       conn_string="sqlite:///verified.db",
                       if_missing="create", id_field="key")

    queue = StagingQueue(drafts, verified, control="control.db")
    queue.register_batch([Draft(key="A1", account="A", priority=10)])

    page = queue.next_page(2)                   # first page
    page = queue.next_page(2, cursor=page.cursor)  # turn the page
    queue.explain("A1")                         # why a key is (not) listed

.. autoclass:: redbird.staging.StagingQueue
    :members:

.. autoclass:: redbird.staging.Page

.. autoclass:: redbird.staging.Explanation
