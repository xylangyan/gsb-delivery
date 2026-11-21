"""Build the two input CLDF Wordlist batches (``jia`` and ``yi``).

Both datasets only contain the tables the task needs:

* FormTable
* ParameterTable
* CognateTable (cognacy judgements)
* CognatesetTable (the cognate sets being referenced)

The two batches deliberately use *local* column names for some CLDF
reference properties and duplicate plain-text IDs (``F1``, ``C1``,
``P1``) to prove that identity is per record / per batch, not per
spelling.
"""
import pathlib

from pycldf import Wordlist

ROOT = pathlib.Path(__file__).resolve().parent.parent

# CLDF term URIs reused below.
ID = 'http://cldf.clld.org/v1.0/terms.rdf#id'
NAME = 'http://cldf.clld.org/v1.0/terms.rdf#name'
DESCRIPTION = 'http://cldf.clld.org/v1.0/terms.rdf#description'
FORM = 'http://cldf.clld.org/v1.0/terms.rdf#form'
PARAMETER_REF = 'http://cldf.clld.org/v1.0/terms.rdf#parameterReference'
FORM_REF = 'http://cldf.clld.org/v1.0/terms.rdf#formReference'
COGSET_REF = 'http://cldf.clld.org/v1.0/terms.rdf#cognatesetReference'


def _new_dataset(directory: pathlib.Path) -> Wordlist:
    directory.mkdir(parents=True, exist_ok=True)
    ds = Wordlist.in_dir(directory)
    # The module default only ships a FormTable; add the components the
    # task asks for.  pycldf derives the foreign-key constraints from the
    # reference propertyUrls automatically.
    ds.add_component('ParameterTable')
    ds.add_component('CognatesetTable')
    ds.add_component('CognateTable')
    return ds


def build_jia(directory: pathlib.Path) -> None:
    """Batch 甲.

    * form ``F1`` (local form id) for concept ``P1``, cognate judgement
      ``C1`` linking it to cognate set ``S1``;
    * a second form ``F2`` for a batch-specific concept ``P2`` with its
      own cognate judgement ``C2`` / set ``S2``.
    """
    ds = _new_dataset(directory)
    ds.write(
        ParameterTable=[
            {'ID': 'P1', 'Name': 'hand', 'Description': 'hand (body part)'},
            {'ID': 'P2', 'Name': 'arm', 'Description': 'arm (body part)'},
        ],
        FormTable=[
            {'ID': 'F1', 'Language_ID': 'L-jia', 'Parameter_ID': 'P1', 'Form': 'hant'},
            {'ID': 'F2', 'Language_ID': 'L-jia', 'Parameter_ID': 'P2', 'Form': 'arum'},
        ],
        CognatesetTable=[
            {'ID': 'S1', 'Description': 'hand cognate set of batch jia'},
            {'ID': 'S2', 'Description': 'arm cognate set of batch jia'},
        ],
        CognateTable=[
            {'ID': 'C1', 'Form_ID': 'F1', 'Cognateset_ID': 'S1'},
            {'ID': 'C2', 'Form_ID': 'F2', 'Cognateset_ID': 'S2'},
        ],
    )


def build_yi(directory: pathlib.Path) -> None:
    """Batch 乙.

    Columns carrying CLDF references use *local* names (``Begriff``,
    ``Wortform``, ``Beleg``, ``Verwandtschaftsgruppe``) instead of the
    ontology term names; the propertyUrls still mark them as the standard
    reference properties, so the merge can resolve columns by semantics.

    * ``F1`` / ``P1`` / ``C1`` / ``S1`` again - separate records that only
      happen to share spellings with batch 甲;
    * concept ``P3`` is *also named* "hand" but is explicitly NOT mapped
      onto 甲's P1: it carries its own form ``F3``, judgement ``C3`` and
      set ``S3``.
    """
    ds = _new_dataset(directory)

    # Rename the reference columns to batch-local names.  rename_column
    # cascades the change into the auto-generated foreign-key constraints.
    ds.rename_column(ds['ParameterTable'], NAME, 'BegriffName')
    ds.rename_column(ds['FormTable'], PARAMETER_REF, 'Begriff')
    ds.rename_column(ds['FormTable'], FORM, 'Wortform')
    ds.rename_column(ds['CognateTable'], FORM_REF, 'Beleg')
    ds.rename_column(ds['CognateTable'], COGSET_REF, 'Verwandtschaftsgruppe')
    ds.rename_column(ds['CognatesetTable'], DESCRIPTION, 'Anmerkung')

    ds.write(
        ParameterTable=[
            {'ID': 'P1', 'BegriffName': 'hand',
             'Description': 'hand - mapped onto jia P1 by the explicit mapping'},
            {'ID': 'P3', 'BegriffName': 'hand',
             'Description': 'hand-like concept, NOT confirmed as shared with jia P1'},
        ],
        FormTable=[
            # Same spelled form as jia's F1 ("hant"): still a separate record.
            {'ID': 'F1', 'Language_ID': 'L-yi', 'Begriff': 'P1', 'Wortform': 'hant'},
            {'ID': 'F3', 'Language_ID': 'L-yi', 'Begriff': 'P3', 'Wortform': 'hant'},
        ],
        CognatesetTable=[
            {'ID': 'S1', 'Anmerkung': 'hand cognate set of batch yi'},
            {'ID': 'S3', 'Anmerkung': 'unconfirmed hand-like cognate set of batch yi'},
        ],
        CognateTable=[
            {'ID': 'C1', 'Beleg': 'F1', 'Verwandtschaftsgruppe': 'S1'},
            {'ID': 'C3', 'Beleg': 'F3', 'Verwandtschaftsgruppe': 'S3'},
        ],
    )


def main() -> None:
    build_jia(ROOT / 'inputs' / 'jia' / 'cldf')
    build_yi(ROOT / 'inputs' / 'yi' / 'cldf')
    print('wrote input batches jia and yi')


if __name__ == '__main__':
    main()
