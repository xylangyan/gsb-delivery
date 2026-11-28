=========
deepmerge
=========

.. image:: https://img.shields.io/pypi/v/deepmerge.svg
   :target: https://pypi.org/project/deepmerge/

.. image:: https://img.shields.io/pypi/status/deepmerge.svg
   :target: https://pypi.org/project/deepmerge/

.. image:: https://img.shields.io/pypi/pyversions/pillar.svg
   :target: https://github.com/toumorokoshi/deepmerge

.. image:: https://img.shields.io/github/license/toumorokoshi/deepmerge.svg
   :target: https://github.com/toumorokoshi/deepmerge

.. image:: https://github.com/toumorokoshi/deepmerge/actions/workflows/python-package.yaml/badge.svg
    :target: https://github.com/toumorokoshi/deepmerge/actions/workflows/python-package.yaml

A tool to handle merging of nested data structures in Python.

------------
Installation
------------

deepmerge is available on `pypi <https://pypi.org/project/deepmerge/>`_:

.. code-block:: bash

   pip install deepmerge

-------
Example
-------

**Generic Strategy**

.. code-block:: python

    from deepmerge import always_merger

    base = {"foo": ["bar"]}
    next = {"foo": ["baz"]}

    expected_result = {'foo': ['bar', 'baz']}
    result = always_merger.merge(base, next)

    assert expected_result == result


**Custom Strategy**

.. code-block:: python

    from deepmerge import Merger

    my_merger = Merger(
        # pass in a list of tuple, with the
        # strategies you are looking to apply
        # to each type.
        [
            (list, ["append"]),
            (dict, ["merge"]),
            (set, ["union"])
        ],
        # next, choose the fallback strategies,
        # applied to all other types:
        ["override"],
        # finally, choose the strategies in
        # the case where the types conflict:
        ["override"]
    )
    base = {"foo": ["bar"]}
    next = {"bar": "baz"}
    my_merger.merge(base, next)
    assert base == {"foo": ["bar"], "bar": "baz"}


You can also pass in your own merge functions, instead of a string.

For more information, see the `docs <https://deepmerge.readthedocs.io/en/latest/>`_

------------------------------
Upstream merge planning
------------------------------

When several adjacent packets may be merged *before* they reach the consumer,
plain ``merge`` is not enough: the built-in strategies are not associative, and
a value that compares equal can still have come from the wrong original packet.
``deepmerge.planner`` checks candidate packages against the true serial order
(including per-field origin ids) and produces a send-minimal dispatch plan,
optionally across two station levels and two blueprint versions::

    python -m deepmerge.demo
    python -m deepmerge.plan examples/work_order.json

See :doc:`docs/planning` for the work-order format.

------------------
Supported Versions
------------------

deepmerge is supported on Python 3.8+.

For older Python versions the last supported version of deepmerge is listed
below:

- 3.7 : 1.1.1
