"""
pyexcel.exceptions
~~~~~~~~~~~~~~~~~~~

Exceptions appeared in pyexcel

:copyright: (c) 2014-2026 by C Wang
:license: New BSD License
"""


class UnknownParameters(Exception):
    """Unknown parameter(s) were given to the signature functions"""

    pass


class FileTypeNotSupported(Exception):
    """A file type is not supported"""

    pass


class UpgradePlugin(Exception):
    """Please upgrade your plugin"""

    pass


class CatalogIncomplete(Exception):
    """A group catalog was not completely scanned and cannot be delivered"""

    pass


class UnknownGroup(Exception):
    """The requested group is not present in the catalog"""

    pass


class GroupNotFound(KeyError):
    """The requested group is not present in a recovery report"""

    pass
