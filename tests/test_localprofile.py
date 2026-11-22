# -*- coding: utf-8 -*-
from __future__ import division
from __future__ import print_function
from __future__ import absolute_import
from __future__ import unicode_literals

import json
import threading
import pytest
from tableschema import exceptions
from tableschema.profile import LocalProfile, Profile


# Helpers

PATTERN_CHECKSUM_V1 = '^[0-9]{4}-[0-9]$'
PATTERN_CHECKSUM_V2 = '^[0-9]{4}-[0-9]{2}$'
PATTERN_PLAIN = '^[0-9]+$'


def _root_json(marker):
    return json.dumps({
        '$schema': 'http://json-schema.org/draft-04/schema#',
        'title': 'customer-sites',
        'description': marker,
        'type': 'object',
        'properties': {
            'primaryKey': {
                'type': 'array',
                'items': {'type': 'string'},
                'minItems': 2,
            },
            'home': {'$ref': 'next/site.json'},
            'work': {'$ref': 'next/site.json'},
        },
        'additionalProperties': False,
    }, indent=2, sort_keys=True)


def _site_json(marker):
    return json.dumps({
        'description': marker,
        'type': 'object',
        'properties': {
            'address': {'$ref': 'address.json'},
        },
        'required': ['address'],
    }, indent=2, sort_keys=True)


def _address_json(marker, pattern):
    return json.dumps({
        'description': marker,
        'type': 'object',
        'properties': {
            'siteCode': {'type': 'string', 'pattern': pattern},
        },
        'required': ['siteCode'],
    }, indent=2, sort_keys=True)


def _write_package(directory, marker, pattern):
    package = directory / marker
    (package / 'next').mkdir(parents=True)
    (package / 'schema.json').write_text(_root_json(marker), encoding='utf-8')
    (package / 'next' / 'site.json').write_text(_site_json(marker), encoding='utf-8')
    (package / 'next' / 'address.json').write_text(
        _address_json(marker, pattern), encoding='utf-8')
    return package


def _load(directory, marker):
    return LocalProfile(str(directory / marker / 'schema.json'))


def _descriptor(site_code, primary_key=None):
    descriptor = {
        'home': {'address': {'siteCode': site_code}},
        'work': {'address': {'siteCode': site_code}},
    }
    if primary_key is not None:
        descriptor['primaryKey'] = primary_key
    return descriptor


# Tests

def test_preparations_count_and_content_addressed_reuse(tmp_path):
    marker = 'test_preparations_count_and_content_addressed_reuse'
    # A and B share byte-identical root documents but differ in the
    # indirectly referenced file; C holds rules identical to B
    # in another directory
    _write_package(tmp_path, marker, PATTERN_CHECKSUM_V1)
    (tmp_path / marker).rename(tmp_path / (marker + '_a'))
    _write_package(tmp_path, marker, PATTERN_PLAIN)
    (tmp_path / marker).rename(tmp_path / (marker + '_b'))
    _write_package(tmp_path, marker, PATTERN_PLAIN)
    (tmp_path / marker).rename(tmp_path / (marker + '_c'))

    before = LocalProfile.preparations
    profile_a = _load(tmp_path, marker + '_a')
    profile_b = _load(tmp_path, marker + '_b')
    profile_c = _load(tmp_path, marker + '_c')

    # identical root bytes don't imply reuse: A and B are prepared apart,
    # while C reuses the rules already prepared for B
    assert LocalProfile.preparations == before + 2
    assert profile_a.version != profile_b.version
    assert profile_b.version == profile_c.version


def test_error_details_include_version_location_and_source(tmp_path):
    marker = 'test_error_details_include_version_location_and_source'
    _write_package(tmp_path, marker, PATTERN_CHECKSUM_V1)
    profile = _load(tmp_path, marker)

    errors = profile.validate(_descriptor('not-a-code'))
    assert len(errors) == 2
    by_location = {}
    for error in errors:
        message = str(error)
        assert profile.version in message
        assert 'next/address.json#/properties/siteCode' in message
        assert error.rule_version == profile.version
        assert error.reference_source == 'next/address.json#/properties/siteCode'
        by_location[error.descriptor_path] = error
    assert sorted(by_location) == ['home/address/siteCode', 'work/address/siteCode']
    for location in by_location:
        assert location in str(by_location[location])


def test_publish_swaps_version_and_inflight_keeps_old_rules(tmp_path):
    marker = 'test_publish_swaps_version_and_inflight_keeps_old_rules'
    package = _write_package(tmp_path, marker, PATTERN_CHECKSUM_V1)
    profile = _load(tmp_path, marker)
    descriptor_v1 = _descriptor('1234-5')
    descriptor_v2 = _descriptor('1234-56')
    assert profile.validate(descriptor_v1) == []
    assert len(profile.validate(descriptor_v2)) == 2

    # a validation started before the publish
    inflight = profile.current

    # replace only the indirectly referenced file of the package
    (package / 'next' / 'address.json').write_text(
        _address_json(marker, PATTERN_CHECKSUM_V2), encoding='utf-8')
    before = LocalProfile.preparations
    version = profile.publish()
    assert LocalProfile.preparations == before + 1
    assert version != inflight.version
    assert profile.version == version

    # the started validation keeps the old rules at both reference sites
    assert inflight.validate(descriptor_v1) == []
    assert len(inflight.validate(descriptor_v2)) == 2

    # validations started after the publish use the new rules at both sites
    assert profile.validate(descriptor_v2) == []
    assert len(profile.validate(descriptor_v1)) == 2


def test_parallel_validation_isolated_between_packages(tmp_path):
    marker = 'test_parallel_validation_isolated_between_packages'
    _write_package(tmp_path, marker, PATTERN_CHECKSUM_V1)
    (tmp_path / marker).rename(tmp_path / (marker + '_a'))
    _write_package(tmp_path, marker, PATTERN_PLAIN)
    (tmp_path / marker).rename(tmp_path / (marker + '_b'))
    profile_a = _load(tmp_path, marker + '_a')
    profile_b = _load(tmp_path, marker + '_b')
    version_b = profile_b.version

    failures = []

    def validate(profile, descriptor, expected_errors):
        try:
            for _ in range(20):
                errors = profile.validate(descriptor)
                assert len(errors) == expected_errors
        except AssertionError as exception:
            failures.append(str(exception))

    threads = []
    for _ in range(4):
        threads.append(threading.Thread(
            target=validate, args=(profile_a, _descriptor('1234-5'), 0)))
        threads.append(threading.Thread(
            target=validate, args=(profile_a, _descriptor('12345'), 2)))
        threads.append(threading.Thread(
            target=validate, args=(profile_b, _descriptor('12345'), 0)))
        threads.append(threading.Thread(
            target=validate, args=(profile_b, _descriptor('1234-5'), 2)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []

    # publishing a new version for A doesn't affect B
    package_a = tmp_path / (marker + '_a')
    (package_a / 'next' / 'address.json').write_text(
        _address_json(marker, PATTERN_CHECKSUM_V2), encoding='utf-8')
    profile_a.publish()
    assert profile_b.version == version_b
    assert profile_b.validate(_descriptor('12345')) == []
    assert len(profile_b.validate(_descriptor('1234-5'))) == 2


def test_failed_publish_keeps_previous_version(tmp_path):
    marker = 'test_failed_publish_keeps_previous_version'
    package = _write_package(tmp_path, marker, PATTERN_CHECKSUM_V1)
    profile = _load(tmp_path, marker)
    version = profile.version
    descriptor = _descriptor('1234-5')

    # invalid JSON in the referenced file
    (package / 'next' / 'address.json').write_text('{broken', encoding='utf-8')
    with pytest.raises(exceptions.LoadError):
        profile.publish()
    assert profile.version == version
    assert profile.validate(descriptor) == []

    # incomplete package: the referenced file is missing
    (package / 'next' / 'address.json').unlink()
    with pytest.raises(exceptions.LoadError):
        profile.publish()
    assert profile.version == version
    assert profile.validate(descriptor) == []

    # dangling file reference
    (package / 'next' / 'site.json').write_text(
        _site_json(marker).replace('address.json', 'missing.json'), encoding='utf-8')
    with pytest.raises(exceptions.LoadError):
        profile.publish()
    assert profile.version == version
    assert profile.validate(descriptor) == []

    # dangling fragment reference
    (package / 'next' / 'site.json').write_text(
        _site_json(marker).replace('address.json', 'address.json#/nope'),
        encoding='utf-8')
    (package / 'next' / 'address.json').write_text(
        _address_json(marker, PATTERN_CHECKSUM_V2), encoding='utf-8')
    with pytest.raises(exceptions.LoadError):
        profile.publish()
    assert profile.version == version
    assert profile.validate(descriptor) == []

    # invalid rules in the referenced file
    (package / 'next' / 'site.json').write_text(_site_json(marker), encoding='utf-8')
    (package / 'next' / 'address.json').write_text(
        json.dumps({'type': 'unknown-type'}), encoding='utf-8')
    with pytest.raises(exceptions.LoadError):
        profile.publish()
    assert profile.version == version
    assert profile.validate(descriptor) == []


def test_strict_and_error_collection_are_per_call(tmp_path):
    marker = 'test_strict_and_error_collection_are_per_call'
    _write_package(tmp_path, marker, PATTERN_CHECKSUM_V1)
    profile = _load(tmp_path, marker)
    descriptor = _descriptor('bad', primary_key=['only-one'])

    # without strict the full error set is collected, not raised
    errors = profile.validate(descriptor)
    assert len(errors) == 3
    assert sorted(error.descriptor_path for error in errors) == [
        'home/address/siteCode', 'primaryKey', 'work/address/siteCode']
    assert 'schema.json#/properties/primaryKey' in [
        error.reference_source for error in errors]

    # with strict the collected error set is raised per call
    with pytest.raises(exceptions.ValidationError) as excinfo:
        profile.validate(descriptor, strict=True)
    assert excinfo.value.multiple
    assert len(excinfo.value.errors) == 3

    # other calls with other descriptors and composite keys are unaffected
    assert profile.validate(_descriptor('1234-5', primary_key=['a', 'b'])) == []
    assert profile.validate(_descriptor('1234-5', primary_key=['x', 'y']),
                            strict=True) == []


def test_missing_package_root_raises_load_error(tmp_path):
    with pytest.raises(exceptions.LoadError):
        LocalProfile(str(tmp_path / 'missing' / 'schema.json'))


def test_remote_reference_raises_load_error(tmp_path):
    marker = 'test_remote_reference_raises_load_error'
    package = _write_package(tmp_path, marker, PATTERN_CHECKSUM_V1)
    (package / 'schema.json').write_text(
        _root_json(marker).replace(
            'next/site.json', 'https://example.com/site.json'),
        encoding='utf-8')
    with pytest.raises(exceptions.LoadError):
        LocalProfile(str(package / 'schema.json'))


def test_builtin_profiles_keep_default_entry(tmp_path):
    before = LocalProfile.preparations
    profile = Profile('table-schema')
    assert profile.name == 'table-schema'
    assert Profile('geojson').jsonschema['type'] == 'object'
    assert profile.validate({'fields': [{'name': 'id'}]}) is True
    with pytest.raises(exceptions.LoadError):
        Profile('not-existing-profile')
    assert LocalProfile.preparations == before
