API
===

.. currentmodule:: s3fs.core

.. autosummary::
   S3FileSystem
   S3FileSystem.cat
   S3FileSystem.du
   S3FileSystem.exists
   S3FileSystem.find
   S3FileSystem.get
   S3FileSystem.glob
   S3FileSystem.info
   S3FileSystem.ls
   S3FileSystem.merge_sources
   S3FileSystem.mkdir
   S3FileSystem.mv
   S3FileSystem.open
   S3FileSystem.plan_merge
   S3FileSystem.put
   S3FileSystem.read_block
   S3FileSystem.rm
   S3FileSystem.tail
   S3FileSystem.touch

.. currentmodule:: s3fs.compose

.. autosummary::
   SourceRange
   PartConfirmation
   MergePlan
   MergeResult
   MergeExecutionError
   plan_merge
   execute_merge

.. autosummary::
   S3File
   S3File.close
   S3File.flush
   S3File.info
   S3File.read
   S3File.seek
   S3File.tell
   S3File.write

.. currentmodule:: s3fs.mapping

.. autosummary::
   S3Map

.. currentmodule:: s3fs.core

.. autoclass:: S3FileSystem
   :members:
   :inherited-members:

.. autoclass:: S3File
   :members:
   :inherited-members:

.. currentmodule:: s3fs.compose

.. autoclass:: SourceRange

.. autoclass:: PartConfirmation

.. autoclass:: MergePlan
   :members:

.. autoclass:: MergeResult
   :members:

.. autoclass:: MergeExecutionError

.. autofunction:: plan_merge

.. autofunction:: execute_merge

.. currentmodule:: s3fs.mapping

.. autofunction:: S3Map

.. currentmodule:: s3fs.utils

.. autoclass:: ParamKwargsHelper

.. autoclass:: SSEParams
