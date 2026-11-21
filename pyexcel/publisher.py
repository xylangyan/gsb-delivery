"""
pyexcel.publisher
~~~~~~~~~~~~~~~~~~

Publish selected sheets of a workbook as independent downstream files.

Each selected sheet is streamed out of the source workbook in batches and
written to a private temporary file first. Only after every sheet has been
converted are the temporary files swapped in for the stable target paths.
If either the conversion or the publication stage fails, the output
directory is rolled back to the state it had before the run started and no
private intermediate file is left behind.
"""

import os

from pyexcel import iget_book
from pyexcel.internal.garbagecollector import free_resources
from pyexcel.plugins.sources.file_output import WriteSheetToFile

STAGE_CONVERSION = "conversion"
STAGE_PUBLICATION = "publication"


class PublicationError(Exception):
    """Raised when a sheet cannot be converted or published.

    :param str stage: either :data:`STAGE_CONVERSION` or
                      :data:`STAGE_PUBLICATION`
    :param str sheet_name: the name of the sheet being processed
    """

    def __init__(self, stage, sheet_name, cause):
        self.stage = stage
        self.sheet_name = sheet_name
        self.cause = cause
        super().__init__(
            f"{stage} failed for sheet '{sheet_name}': {cause}"
        )


class SheetPublication:
    """Binding between one source sheet name and its target file path."""

    def __init__(self, sheet_name, target_file, file_type="csv"):
        self.sheet_name = sheet_name
        self.target_file = target_file
        self.file_type = file_type


def publish_sheets(workbook_file, selections, **keywords):
    """Publish selected workbook sheets as independent files.

    :param str workbook_file: path of the source workbook
    :param selections: :class:`SheetPublication` instances, one per
                       selected sheet. Processing follows the given order
    :param keywords: extra renderer keywords forwarded to the csv writer
    :returns: the target file paths in the same order as ``selections``
    :raises PublicationError: with the failing stage and sheet name when a
                              conversion or a target file replacement fails
    """
    selections = list(selections)
    targets = [selection.target_file for selection in selections]
    snapshot = _snapshot_directory(targets)
    pending = []
    book_stream = None
    try:
        book_stream = iget_book(file_name=workbook_file)
        for selection in selections:
            temp_file = _private_temp_path(selection.target_file)
            pending.append(temp_file)
            try:
                sheet_stream = book_stream[selection.sheet_name]
            except (KeyError, TypeError) as exc:
                raise PublicationError(
                    STAGE_CONVERSION, selection.sheet_name, exc
                )
            try:
                source = WriteSheetToFile(
                    file_name=temp_file,
                    force_file_type=selection.file_type,
                    **keywords,
                )
                source.write_data(sheet_stream)
            except Exception as exc:
                raise PublicationError(
                    STAGE_CONVERSION, selection.sheet_name, exc
                )
        for selection, temp_file in zip(selections, pending):
            try:
                os.replace(temp_file, selection.target_file)
            except OSError as exc:
                raise PublicationError(
                    STAGE_PUBLICATION, selection.sheet_name, exc
                )
    except Exception:
        _rollback(targets, snapshot, pending)
        raise
    finally:
        if book_stream is not None:
            try:
                free_resources()
            except Exception:
                pass
        _cleanup_private_files(pending)
    return targets


def _snapshot_directory(target_files):
    snapshot = {}
    for target_file in target_files:
        target_file = os.path.abspath(target_file)
        if os.path.exists(target_file):
            with open(target_file, "rb") as handle:
                snapshot[target_file] = handle.read()
        else:
            snapshot[target_file] = None
    return snapshot


def _private_temp_path(target_file):
    directory = os.path.dirname(os.path.abspath(target_file)) or "."
    return os.path.join(
        directory, f".{os.path.basename(target_file)}.pubtmp"
    )


def _rollback(target_files, snapshot, pending_files):
    for target_file in target_files:
        abs_target = os.path.abspath(target_file)
        original = snapshot[abs_target]
        if original is None:
            if os.path.exists(abs_target):
                os.remove(abs_target)
        else:
            with open(abs_target, "wb") as handle:
                handle.write(original)
    _cleanup_private_files(pending_files)


def _cleanup_private_files(pending_files):
    for temp_file in pending_files:
        try:
            os.remove(temp_file)
        except FileNotFoundError:
            pass
