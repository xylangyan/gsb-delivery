"""Build and verify the CLDF Wordlist artifact with pycldf.

The word form table and the example table use *different local names* for their
citation columns; both are declared with the CLDF ``source`` property so that
references can be read back through the ontology rather than via hard-coded
column names.
"""

import pathlib

from pycldf import Wordlist
from pycldf.terms import term_uri
from pycldf.sources import Source, Sources

FORM_TABLE = 'FormTable'
EXAMPLE_TABLE = 'ExampleTable'
FORM_REF_COLUMN = 'Witness_Ref'
EXAMPLE_REF_COLUMN = 'Example_Ref'
SOURCE_TERM = term_uri('source')
METADATA_NAME = 'Wordlist-metadata.json'


def source_columns(dataset) -> list[tuple[str, str]]:
    """Return ``(table url, local column name)`` for every CLDF source column."""
    found = []
    for table in dataset.tables:
        for column in table.tableSchema.columns:
            if column.propertyUrl and column.propertyUrl.uri == SOURCE_TERM:
                found.append((str(table.url), column.name))
    return found


def build_wordlist(
    output_dir: pathlib.Path,
    forms: list[dict],
    examples: list[dict],
    sources: Sources,
) -> pathlib.Path:
    output_dir = pathlib.Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset = Wordlist.in_dir(output_dir)
    # The standard FormTable ships a "Source" column; give it the local name
    # used for word-form citations.
    dataset.rename_column(FORM_TABLE, 'Source', FORM_REF_COLUMN)
    dataset.add_columns(FORM_TABLE, {'name': 'Batch_ID', 'datatype': 'string'})
    dataset.add_component(EXAMPLE_TABLE)
    dataset.add_columns(EXAMPLE_TABLE, {
        'name': EXAMPLE_REF_COLUMN,
        'propertyUrl': SOURCE_TERM,
        'separator': ';',
    })
    dataset.add_columns(EXAMPLE_TABLE, {'name': 'Batch_ID', 'datatype': 'string'})
    for source in sources:
        dataset.add_sources(Source(source.genre, source.id, **dict(source.items())))
    dataset.write(FormTable=forms, ExampleTable=examples)
    written = Wordlist.from_metadata(output_dir / METADATA_NAME)
    written.validate()
    return output_dir / METADATA_NAME


def read_back_citations(artifact_dir: pathlib.Path) -> dict[str, list[list[str]]]:
    """Read every citation cell back from the actual CLDF source columns.

    Returns a mapping ``table url -> list of citation cells`` where each cell is
    the ordered list of ``Key[description]`` strings as stored in the CLDF data.
    """
    dataset = Wordlist.from_metadata(pathlib.Path(artifact_dir) / METADATA_NAME)
    result: dict[str, list[list[str]]] = {}
    for table_url, column_name in source_columns(dataset):
        cells = []
        for row in dataset.iter_rows(table_url):
            value = row[column_name]
            if value is None:
                cells.append([])
            elif isinstance(value, list):
                cells.append(list(value))
            else:
                cells.append([piece.strip() for piece in value.split(';') if piece.strip()])
        result[table_url] = cells
    return result


def read_back_sources(artifact_dir: pathlib.Path) -> Sources:
    dataset = Wordlist.from_metadata(pathlib.Path(artifact_dir) / METADATA_NAME)
    return dataset.sources
