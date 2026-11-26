# python-cabarchive

Contributors welcome, either adding new functionality or fixing bugs.

Documentation available here: https://python-cabarchive.readthedocs.io/

See also: https://msdn.microsoft.com/en-us/library/bb417343.aspx

# Repacking into new deliveries

`cabarchive.repack` splits a signed-off single-folder MSZIP cabinet into new
packages (typically two batches) without recompressing what can be copied:
every original CFDATA block has a receipt number (its index in the folder),
and a block is grafted verbatim -- compressed body and checksum preserved --
into a new package only when its *actual decode result* in the new context
matches the bytes needed there, which also settles whether a block borrowing
its predecessor as deflate dictionary may be moved.  Each receipt number may
be spent at most once across all deliveries, and the chosen pair of packages
minimises the total uncompressed bytes that need recompressing.

    python -m cabarchive.repack sheet.json --report report.json

The handover sheet (see the `cabarchive.repack` module docstring for the full
format) fixes the file order of each package, the uncompressed positions where
blocks may be split, and the positions each old block may occupy.  The JSON
report lists where every grafted block landed, why a requested position could
not be used (dictionary changed the output, split not allowed, or the voucher
was spent on the other package), and a verification pass that reopens both
cabinets checking file names, contents and order.

# Release Process

These notes are probably only for the maintainer of this module!

    make pkg
    ./env/bin/twine upload dist/*
