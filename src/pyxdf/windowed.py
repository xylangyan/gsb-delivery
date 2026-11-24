from .archive import *  # noqa: F403


def __getattr__(name):
    from . import archive

    return getattr(archive, name)
