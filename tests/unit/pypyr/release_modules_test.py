"""Unit tests for release-scoped python module loading."""
from pathlib import Path

from pypyr.cache.admin import clear_all
import pypyr.moduleloader as moduleloader

release_root = Path.cwd().joinpath('tests', 'pipelines', 'releases')
v1 = release_root.joinpath('v1').resolve()
v2 = release_root.joinpath('v2').resolve()


def teardown_function():
    """Release modules shouldn't leak between tests."""
    clear_all()


def test_same_package_name_resolves_per_release():
    """receipt_rules.tag resolves to the calling release's package."""
    module_v1 = moduleloader.get_module('receipt_rules.tag', v1)
    module_v2 = moduleloader.get_module('receipt_rules.tag', v2)

    assert module_v1 is not module_v2
    assert module_v1.RULE_TAG == 'old'
    assert module_v2.RULE_TAG == 'new'
    assert module_v1.__file__.startswith(str(v1))
    assert module_v2.__file__.startswith(str(v2))


def test_release_module_cache_is_per_release():
    """Repeated loads of one release return the same module instance."""
    first = moduleloader.get_module('receipt_rules.emit', v1)
    second = moduleloader.get_module('receipt_rules.emit', v1)

    assert first is second


def test_package_and_submodule_share_release_namespace():
    """A package's __init__ initializes once per release."""
    tag_v1 = moduleloader.get_module('receipt_rules.tag', v1)
    input_v1 = moduleloader.get_module('receipt_rules.input', v1)

    assert input_v1.RULE_TAG == tag_v1.RULE_TAG == 'old'


def test_module_in_dir_detects_packages_and_modules():
    """Membership check knows packages from modules."""
    assert moduleloader.module_in_dir('receipt_rules.tag', v1)
    assert moduleloader.module_in_dir('receipt_rules', v2)
    assert not moduleloader.module_in_dir('not_here', v1)
