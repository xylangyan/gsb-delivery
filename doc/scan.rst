Scan-angle windows
==================

Radar archives normally store reflectivity once, in acquisition order.  The
physical scan number is the stable key for that data, while the corrected scan
angle and the quality-control (QC) receipt arrive later, in releases.  A single
release may correct several old scans at once.

h5netcdf adds an optional, persistent index for reading a scan-angle window by
release.  It is available on the ``h5py`` backend only.

Building the index
------------------

The index is built once from the archive columns::

    import h5netcdf

    with h5netcdf.File("radar.nc", "r+") as f:
        f.create_scan_index()

By default the ``scan_angle``, ``qc`` and ``reflectivity`` variables are
detected and the physical scan number is the row position.  Pass
``angle=...``, ``qc=...``, ``reflectivity=...`` or ``scans=...`` to name the
variables explicitly.  The index lives in the private
``/_h5netcdf_scan_index`` group.

Publishing a release
--------------------

A release carries angle calibrations and QC receipts for scans, old or new::

    with h5netcdf.File("radar.nc", "r+") as f:
        release = f.publish_scan_release(
            scans=[0, 2, 1, 3],
            angles=[31.0, 30.0, None, None],
            qcs=[None, None, 8.0, -9999.0],
        )

``None`` keeps the value already stored for that scan.  ``-9999`` is the
default QC fill value and marks a *confirmed missing* receipt.  Only scans
which have received a receipt appear in windows.  Mapping-style updates are
also accepted::

    f.publish_scan_release(
        angle_calibrations={0: 31.0, 2: 30.0},
        qc_receipts={1: 8.0, 3: -9999.0},
    )

Reading a window
----------------

A window is left-closed and right-open, ordered by corrected angle and then by
physical scan number::

    with h5netcdf.File("radar.nc", "r") as f:
        window = f.open_scan_window(29, 32, page_size=1)
        page = window.page()
        for record in page.records:
            print(record.scan, record.angle, record.reflectivity, record.qc)
        token = page.next_token

Pages carry a continuation token.  The token names the release snapshot, the
window bounds and the rank of its next entry, so resuming does not walk the
preceding pages::

    with h5netcdf.File("radar.nc", "r") as f:
        page = f.open_scan_window(token=token).page()

A token issued before a later release keeps reading the old snapshot: angles
and QC values it returns are the ones that were current when the token was
issued.  ``release=...`` selects an explicit snapshot; the default is the
latest release.

Each record exposes ``scan``, ``angle``, ``reflectivity``, ``qc``, ``missing``
and ``has_receipt``, and can also be read as a tuple ``(scan, angle,
reflectivity, qc, missing, has_receipt, release)``.

Reflectivity can be read directly by physical scan number::

    with h5netcdf.File("radar.nc", "r") as f:
        value = f.read_reflectivity(2)
        record = f.read_scan(2)

Storage and update costs
------------------------

Every release is an immutable snapshot stored as a persistent treap.  A
release copies only the search path for the scans it changes and appends those
nodes; all untouched index nodes are shared with the previous release.
Reflectivity datasets are never written when publishing.  Reading a page
locates the page by rank and reads only the reflectivity chunks for the scans
it returns, so neither the number of scans in the window nor later archive
growth makes a small correction scan or copy the whole index.
