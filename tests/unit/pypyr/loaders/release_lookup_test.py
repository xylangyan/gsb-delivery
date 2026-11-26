"""Unit tests for release-scoped pipeline look-up."""
from pathlib import Path

import pytest

from pypyr.errors import PipelineNotFoundError
import pypyr.loaders.file as fileloader

release_root = Path.cwd().joinpath('tests', 'pipelines', 'releases')
v1 = release_root.joinpath('v1')
v2 = release_root.joinpath('v2')


@pytest.fixture(autouse=True)
def clear_file_cache():
    """Keep the loader file cache isolated per test."""
    fileloader.file_cache.clear()
    yield
    fileloader.file_cache.clear()


def test_release_root_resolves_ingest():
    """Release root resolves its own ingest pipeline."""
    path = fileloader.get_pipeline_path('ingest', None, pipeline_dir=v1)

    assert path == v1.joinpath('ingest.yaml').resolve()


def test_release_parent_look_up_within_same_release():
    """A parent in the release resolves the sibling normalize first."""
    collect_parent = v1.joinpath('vendors', 'east')
    path = fileloader.get_pipeline_path('normalize',
                                        collect_parent,
                                        pipeline_dir=v1)

    assert path == collect_parent.joinpath('normalize.yaml').resolve()


def test_release_falls_back_to_release_root():
    """Missing same-dir normalize falls back to the release root."""
    v2_collect_parent = v2.joinpath('vendors', 'east')

    path = fileloader.get_pipeline_path('normalize',
                                        v2_collect_parent,
                                        pipeline_dir=v2)

    assert path == v2.joinpath('normalize.yaml').resolve()


def test_release_ignores_parent_from_another_release():
    """A parent outside this release doesn't escape the release dir."""
    path = fileloader.get_pipeline_path('normalize',
                                        v1.joinpath('vendors', 'east'),
                                        pipeline_dir=v2)

    assert path == v2.joinpath('normalize.yaml').resolve()


def test_release_resolve_from_parent_false_uses_release_root():
    """With parent None, look-up starts at the release root."""
    path = fileloader.get_pipeline_path('normalize', None, pipeline_dir=v2)

    assert path == v2.joinpath('normalize.yaml').resolve()


def test_release_not_found_does_not_fall_back_to_cwd():
    """Release look-ups don't leak into cwd paths."""
    with pytest.raises(PipelineNotFoundError) as err:
        fileloader.get_pipeline_path('smoke', None, pipeline_dir=v1)

    message = str(err.value)
    assert str(v1.resolve()) in message
    assert str(v1.resolve().joinpath('pipelines')) in message
    assert str(Path.cwd().joinpath('pipelines')) not in message


def test_legacy_look_up_still_resolves_cwd():
    """Without a release dir the cwd look-up still works."""
    path = fileloader.get_pipeline_path('tests/pipelines/smoke', None)

    assert path == Path.cwd().joinpath('tests/pipelines/smoke.yaml')
