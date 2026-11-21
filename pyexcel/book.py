"""
pyexcel.book
~~~~~~~~~~~~~~~~~~~

Excel book

:copyright: (c) 2014-2026 by C Wang
:license: New BSD License, see LICENSE for more details
"""

import copy

from pyexcel.sheet import Sheet
from pyexcel._compact import OrderedDict
from pyexcel.internal.meta import BookMeta
from pyexcel.internal.common import SheetIterator

LOCAL_UUID = 0


class Book(BookMeta):
    """
    Read an excel book that has one or more sheets

    For csv file, there will be just one sheet
    """

    def __init__(self, sheets=None, filename="memory", path=None):
        """
        Book constructor

        Selecting a specific book according to filename extension

        :param sheets: a dictionary of data
        :param filename: the physical file
        :param path: the relative path or absolute path
        :param keywords: additional parameters to be passed on
        """
        self.filename = None
        self.__path = None
        self.__sheets = OrderedDict()
        self.__auto_names = {}
        self.init(sheets=sheets, filename=filename, path=path)

    def init(self, sheets=None, filename="memory", path=None):
        """independent function so that it could be called multiple times"""
        self.__path = path
        self.filename = filename
        self.load_from_sheets(sheets)

    def load_from_sheets(self, sheets):
        """
        Load content from existing sheets

        :param dict sheets: a dictionary of sheets. Each sheet is
                            a list of lists
        """
        if sheets is None:
            return
        for name in sheets.keys():
            value = sheets[name]
            if isinstance(value, Sheet):
                sheet = value
                sheet.name = name
            else:
                # array
                sheet = Sheet(value, name)
            # this sheets keep sheet order
            self.__sheets.update({name: sheet})
            # this provide the convenience of access the sheet
            self.__dict__[name.replace(" ", "_")] = sheet
            self._adopt(sheet, name)

    def __iter__(self):
        return SheetIterator(self)

    def __len__(self):
        return len(self.__sheets)

    def sort_sheets(self, key=None, reverse=False):
        sorted_sheet_names = sorted(
            self.__sheets.keys(), key=key, reverse=reverse
        )
        self.__sheets = OrderedDict(
            (sheet_name, self.__sheets[sheet_name])
            for sheet_name in sorted_sheet_names
        )

    def number_of_sheets(self):
        """
        Return the number of sheets
        """
        return len(self)

    def sheet_names(self):
        """
        Return all sheet names
        """
        return list(self.__sheets.keys())

    def sheet_by_name(self, name):
        """
        Get the sheet with the specified name
        """
        return self.__sheets[name]

    def sheet_by_index(self, index):
        """
        Get the sheet with the specified index
        """
        sheet_names = self.sheet_names()
        if index < len(sheet_names):
            sheet_name = sheet_names[index]
            return self.sheet_by_name(sheet_name)

    def remove_sheet(self, sheet):
        """
        Remove a sheet
        """
        if isinstance(sheet, int):
            sheet_names = self.sheet_names()
            if sheet < len(sheet_names):
                sheet_name = sheet_names[sheet]
                self._detach(sheet_name)
            else:
                raise IndexError
        elif isinstance(sheet, str):
            if sheet in self.__sheets:
                self._detach(sheet)
            else:
                raise KeyError
        else:
            raise TypeError

    def __getitem__(self, key):
        """Override operator[]"""
        if isinstance(key, int):
            return self.sheet_by_index(key)
        return self.sheet_by_name(key)

    def __delitem__(self, other):
        """
        Override del book[index]
        """
        self.remove_sheet(other)
        return self

    def __add__(self, other):
        """
        Override operator +

        example::

            book3 = book1 + book2
            book3 = book1 + book2["Sheet 1"]

        """
        output = Book()
        output._absorb(self)
        output += other
        return output

    def __iadd__(self, other):
        """
        Operator overloading +=

        example::

            book += book2
            book += book2["Sheet1"]

        """
        if isinstance(other, Book):
            self._merge_book(other)
        elif isinstance(other, Sheet):
            self._add_a_sheet(other)
        else:
            raise TypeError
        return self

    def _absorb(self, source):
        """
        Copy all sheets from the source book into this one

        The naming rules of operator + are applied: the sheet of a
        single sheet book is renamed after the book's file name.
        """
        names = source.sheet_names()
        single_sheet = len(names) == 1
        for name in names:
            sheet = source.sheet_by_name(name)
            auto_base = source._auto_base(name)
            if single_sheet:
                new_name = f"{source.filename}_{name}"
                self._insert_sheet(
                    new_name,
                    sheet,
                    genuine=False,
                    base=auto_base if auto_base else name,
                )
            elif auto_base is not None:
                self._insert_sheet(
                    name, sheet, genuine=False, base=auto_base
                )
            else:
                self._insert_sheet(name, sheet)

    def _merge_book(self, other):
        """
        Merge another book into this one, in place

        Genuine sheet names, i.e. the original ones and the ones
        chosen by the user, are reserved up front and win over
        auto-assigned ones, so that a fixed sequence of source
        books always yields the same sheet names.
        """
        names = other.sheet_names()
        single_sheet = len(names) == 1
        preferred = {}
        genuine = {}
        for name in names:
            auto_base = other._auto_base(name)
            if single_sheet:
                preferred[name] = f"{other.filename}_{name}"
                genuine[name] = False
            else:
                preferred[name] = name
                genuine[name] = auto_base is None
        reserved = set(preferred[name] for name in names if genuine[name])
        for existing in list(self.__sheets.keys()):
            if existing in reserved and existing in self.__auto_names:
                base = self.__auto_names[existing]
                taken = (set(self.__sheets.keys()) - {existing}) | reserved
                new_name = _free_name(base, taken)
                self._rekey_sheet(existing, new_name, auto_base=base)
        for name in names:
            desired = preferred[name]
            reserved.discard(desired)
            taken = set(self.__sheets.keys()) | reserved
            auto_base = other._auto_base(name)
            base = auto_base if auto_base else name
            if desired in taken:
                new_name = _free_name(name, taken)
                self._insert_sheet(
                    new_name,
                    other.sheet_by_name(name),
                    genuine=False,
                    base=base,
                )
            elif genuine[name]:
                self._insert_sheet(desired, other.sheet_by_name(name))
            else:
                self._insert_sheet(
                    desired,
                    other.sheet_by_name(name),
                    genuine=False,
                    base=base,
                )

    def _add_a_sheet(self, sheet):
        """
        Add a sheet to the book

        :param sheet: an instance of Sheet
        """
        new_key = _free_name(sheet.name, set(self.__sheets.keys()))
        if new_key == sheet.name:
            self._insert_sheet(new_key, sheet)
        else:
            self._insert_sheet(
                new_key, sheet, genuine=False, base=sheet.name
            )

    def _insert_sheet(self, name, sheet, genuine=True, base=None):
        """
        Insert an independent copy of the sheet under the given name
        """
        new_sheet = Sheet(copy.deepcopy(sheet.array), name)
        self.__sheets[name] = new_sheet
        self.__dict__[name.replace(" ", "_")] = new_sheet
        self._adopt(new_sheet, name, genuine=genuine, base=base)

    def _adopt(self, sheet, name, genuine=True, base=None):
        """
        Register a sheet so that later renames stay in sync
        """
        sheet._parent_book = self
        if genuine:
            self.__auto_names.pop(name, None)
        else:
            self.__auto_names[name] = base if base is not None else name

    def _detach(self, name):
        """
        Remove the named sheet from the book's index
        """
        removed = self.__sheets.pop(name)
        self.__auto_names.pop(name, None)
        if getattr(removed, "_parent_book", None) is self:
            removed._parent_book = None

    def _auto_base(self, name):
        """
        Return the original name of an auto-assigned sheet name
        """
        return self.__auto_names.get(name)

    def _rename_sheet(self, sheet, new_name):
        """
        Keep the book's index in sync with a sheet's new name
        """
        old_name = None
        for key, value in self.__sheets.items():
            if value is sheet:
                old_name = key
                break
        if old_name is None or old_name == new_name:
            return
        if new_name in self.__sheets:
            raise ValueError(f"Sheet name '{new_name}' already exists")
        self._rekey_sheet(old_name, new_name)

    def _rekey_sheet(self, old_name, new_name, auto_base=None):
        """
        Rename a sheet in the book's index, keeping the sheet order
        """
        sheet = self.__sheets[old_name]
        renamed = OrderedDict()
        for key, value in self.__sheets.items():
            if key == old_name:
                renamed[new_name] = value
            else:
                renamed[key] = value
        self.__sheets = renamed
        old_attribute = old_name.replace(" ", "_")
        if self.__dict__.get(old_attribute) is sheet:
            del self.__dict__[old_attribute]
        self.__dict__[new_name.replace(" ", "_")] = sheet
        sheet._name = new_name
        self.__auto_names.pop(old_name, None)
        if auto_base is None:
            self.__auto_names.pop(new_name, None)
        else:
            self.__auto_names[new_name] = auto_base

    def to_dict(self):
        """Convert the book to a dictionary"""
        the_dict = OrderedDict()
        for sheet in self:
            the_dict.update({sheet.name: sheet.array})
        return the_dict


def to_book(bookstream):
    """Convert a bookstream to Book"""
    if isinstance(bookstream, Book):
        return bookstream
    return Book(
        bookstream.to_dict(),
        filename=bookstream.filename,
        path=bookstream.path,
    )


def local_uuid():
    """create home made uuid"""
    global LOCAL_UUID
    LOCAL_UUID = LOCAL_UUID + 1
    return LOCAL_UUID


def _free_name(base, taken):
    """Return base if not taken, else the first free base_N name.

    The outcome depends only on the given names, so that a fixed
    sequence of source books always yields the same sheet names.
    """
    if base not in taken:
        return base
    index = 1
    while f"{base}_{index}" in taken:
        index += 1
    return f"{base}_{index}"
