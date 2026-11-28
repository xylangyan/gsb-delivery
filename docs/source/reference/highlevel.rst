.. _api_highlevel:

High-level functions API
************************

.. _api_extract_text:

extract_text
============

.. currentmodule:: pdfminer.high_level
.. autofunction:: extract_text


.. _api_extract_text_to_fp:

extract_text_to_fp
==================

.. currentmodule:: pdfminer.high_level
.. autofunction:: extract_text_to_fp


.. _api_extract_pages:

extract_pages
=============

.. currentmodule:: pdfminer.high_level
.. autofunction:: extract_pages


.. _api_extract_receipt_columns:

extract_receipt_columns
=======================

.. currentmodule:: pdfminer.high_level
.. autofunction:: extract_receipt_columns

The correction-aware session and its data types live in
:mod:`pdfminer.receipt` and are documented below.

.. currentmodule:: pdfminer.receipt

.. autoclass:: ReceiptColumnSession
   :members:

.. autoclass:: CorrectionRegistry
   :members:

.. autoclass:: FontIdentity

.. autoclass:: UnicodeCandidate

.. autoclass:: ColumnReissue

.. autoclass:: UnicodeConflict

.. autoclass:: GlyphRecord
