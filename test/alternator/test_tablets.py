# Copyright 2024-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0

# Tests for the Scylla-only "tablets" feature.
#
# Ideally, tablets are just an implementation detail (replacing the
# old vnodes), that the DynamoDB API user would not even be aware
# of. So there should be very few, if any, tests in this file.
# However, temporarily - while the tablets feature is only partially
# working, it is useful
# to have here a few tests that clarify the situation and how to
# override it. Most of these tests, or perhaps even this entire file,
# will probably go away eventually.

import time
import pytest
from botocore.exceptions import ClientError

from .util import new_test_table, create_test_table, scylla_config_read, scylla_config_temporary, scylla_inject_error

# All tests in this file are scylla-only
@pytest.fixture(scope="function", autouse=True)
def all_tests_are_scylla_only(scylla_only):
    pass

# Utility function for checking if a given table is using tablets
# or not. We rely on some knowledge of Alternator internals:
# 1. For table with name X, Scylla creates a keyspace called alternator_X
# 2. We can read a CQL system table using the ".scylla.alternator." prefix.
def uses_tablets(dynamodb, table):
    info = dynamodb.Table('.scylla.alternator.system_schema.scylla_keyspaces')
    try:
        response = info.query(
            KeyConditions={'keyspace_name': {
                    'AttributeValueList': ['alternator_'+table.name],
                    'ComparisonOperator':  'EQ'}})
    except dynamodb.meta.client.exceptions.ResourceNotFoundException:
        # The internal Scylla table doesn't even exist, either this isn't
        # Scylla or it's older Scylla and doesn't use tablets.
        return False
    if not 'Items' in response or not response['Items']:
        return False
    if 'initial_tablets' in response['Items'][0] and response['Items'][0]['initial_tablets']:
        return True
    return False

# Utility function for checking whether using tablets by a given table
# is in-line with the global Scylla configuration flag regarding tablets.
def assert_tablets_usage_follows_config(dynamodb, table):
    tablets_default = scylla_config_read(dynamodb, 'tablets_mode_for_new_keyspaces')
    if tablets_default in ("\"enabled\"", "\"enforced\"", None):
        assert uses_tablets(dynamodb, table)
    else:
        assert not uses_tablets(dynamodb, table)

# New Alternator tables are created with tablets or vnodes, according to the
# "tablets_mode_for_new_keyspaces" configuration flag.
def test_default_tablets(dynamodb):
    schema = {
        'KeySchema': [ { 'AttributeName': 'p', 'KeyType': 'HASH' } ],
        'AttributeDefinitions': [ { 'AttributeName': 'p', 'AttributeType': 'S' }]}
    with new_test_table(dynamodb, **schema) as table:
        assert_tablets_usage_follows_config(dynamodb, table)

# Tests for the initial_tablets tag named "system:initial_tablets".
# This tag was earlier called "experimental:initial_tablets".
# Ref. #26211
initial_tablets_tag = 'system:initial_tablets'

# Check that a table created with a number as initial_tablets will use 
# tablets. Different numbers have different meanings (0 asked to use
# default number, any other number overrides the default) but they
# all enable tablets.
def test_initial_tablets_int(dynamodb):
    for value in ['0', '4']:
        schema = {
            'Tags': [{'Key': initial_tablets_tag, 'Value': value}],
            'KeySchema': [ { 'AttributeName': 'p', 'KeyType': 'HASH' } ],
            'AttributeDefinitions': [ { 'AttributeName': 'p', 'AttributeType': 'S' }]}
        with new_test_table(dynamodb, **schema) as table:
            assert uses_tablets(dynamodb, table)

# Check that a table created with a non-number (e.g., the string "none")
# as initial_tablets, will not use tablets.
def test_initial_tablets_not_int(dynamodb):
    schema = {
        'Tags': [{'Key': initial_tablets_tag, 'Value': 'none'}],
        'KeySchema': [ { 'AttributeName': 'p', 'KeyType': 'HASH' } ],
        'AttributeDefinitions': [ { 'AttributeName': 'p', 'AttributeType': 'S' }]}
    with new_test_table(dynamodb, **schema) as table:
        assert not uses_tablets(dynamodb, table)

# Usage of tablets is determined by the configuration flag
# "tablets_mode_for_new_keyspaces", as well as by the per-table
# "system:initial_tablets" tag. The tag overrides the configuration,
# except when the configuration flag's value is "enforced" -
# then if the tag asks for vnodes, an error is generated.
def test_tablets_tag_vs_config(dynamodb):
    schema = {
        'KeySchema': [ { 'AttributeName': 'p', 'KeyType': 'HASH' } ],
        'AttributeDefinitions': [ { 'AttributeName': 'p', 'AttributeType': 'S' }]
    }
    schema_tablets = {**schema, 'Tags': [{'Key': initial_tablets_tag, 'Value': '0'}]}
    schema_vnodes = {**schema, 'Tags': [{'Key': initial_tablets_tag, 'Value': 'none'}]}
    # With tablets_mode_for_new_keyspaces=enabled, tablets are used unless
    # the user explicitly asks for vnodes (schema_vnodes).
    with scylla_config_temporary(dynamodb, 'tablets_mode_for_new_keyspaces', 'enabled'):
        with new_test_table(dynamodb, **schema) as table:
            assert uses_tablets(dynamodb, table)
        with new_test_table(dynamodb, **schema_tablets) as table:
            assert uses_tablets(dynamodb, table)
        with new_test_table(dynamodb, **schema_vnodes) as table:
            assert not uses_tablets(dynamodb, table)
    # With tablets_mode_for_new_keyspaces=disabled, vnodes are used unless
    # the user explicitly asks tablets (schema_tablets)
    with scylla_config_temporary(dynamodb, 'tablets_mode_for_new_keyspaces', 'disabled'):
        with new_test_table(dynamodb, **schema) as table:
            assert not uses_tablets(dynamodb, table)
        with new_test_table(dynamodb, **schema_tablets) as table:
            assert uses_tablets(dynamodb, table)
        with new_test_table(dynamodb, **schema_vnodes) as table:
            assert not uses_tablets(dynamodb, table)
    # With tablets_mode_for_new_keyspaces=enforced, tablets are used except
    # when the user requests vnodes, which is a ValidationException.
    with scylla_config_temporary(dynamodb, 'tablets_mode_for_new_keyspaces', 'enforced'):
        with new_test_table(dynamodb, **schema) as table:
            assert uses_tablets(dynamodb, table)
        with new_test_table(dynamodb, **schema_tablets) as table:
            assert uses_tablets(dynamodb, table)
        with pytest.raises(ClientError, match='ValidationException.*tablets'):
            with new_test_table(dynamodb, **schema_vnodes) as table:
                pass

# Test that enabling Alternator Streams on a tablet table uses deferred
# enablement: the table goes through an ENABLING state (where the intent
# is stored but CDC is not yet active) before the topology coordinator
# finalizes it to ENABLED. While streams are active (ENABLING or ENABLED),
# tablet merges must be blocked but splits must still be allowed. After
# streams are disabled, merges must be allowed again.
#
# The test exercises an elaborate sequence of tablet count changes:
# in each of the ENABLING and ENABLED states, first a shrinkage is
# attempted (must be blocked), then an increase (must succeed). After
# disabling streams, a shrinkage is attempted (must succeed).
def test_deferred_stream_enablement_on_tablets(dynamodb, rest_api, cql):
    def get_table_id(ks, table_name):
        rows = cql.execute(f"SELECT id FROM system_schema.tables "
                           f"WHERE keyspace_name='{ks}' AND table_name='{table_name}'")
        return rows.one().id

    def get_tablet_count(table_id):
        rows = cql.execute(f"SELECT tablet_count FROM system.tablets "
                           f"WHERE table_id={table_id} LIMIT 1")
        return rows.one().tablet_count

    def set_tablet_target(ks, table_name, count):
        """Set both min and max tablet count to force a specific target."""
        cql.execute(f'ALTER TABLE "{ks}"."{table_name}" '
                    f"WITH tablets = {{'min_tablet_count': {count}, 'max_tablet_count': {count}}}")

    def wait_for_tablet_count(table_id, expected_count, timeout=60):
        """Wait for tablet count to reach exact expected_count."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            count = get_tablet_count(table_id)
            if count == expected_count:
                return count
            time.sleep(0.1)
        pytest.fail(f"Tablet count did not reach {expected_count} "
                    f"within {timeout}s (current: {get_tablet_count(table_id)})")

    def assert_tablet_count_stable(table_id, expected_count, duration=5):
        """Assert tablet count stays at expected_count for duration seconds."""
        deadline = time.time() + duration
        while time.time() < deadline:
            assert get_tablet_count(table_id) == expected_count, \
                f"Tablet count changed unexpectedly from {expected_count}"
            time.sleep(0.1)

    # === Phase 1: ENABLING state ===
    # Hold finalization with an error injection so the table stays in
    # ENABLING (enable_requested=true, enabled=false) state.
    with scylla_inject_error(rest_api, "delay_cdc_stream_finalization"):
        table = create_test_table(dynamodb,
            Tags=[{'Key': initial_tablets_tag, 'Value': '4'}],
            StreamSpecification={'StreamEnabled': True, 'StreamViewType': 'KEYS_ONLY'},
            KeySchema=[{'AttributeName': 'p', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'p', 'AttributeType': 'S'}])
        try:
            # Verify ENABLING state: StreamSpecification is present but
            # LatestStreamArn is not (CDC log table does not exist yet).
            desc = table.meta.client.describe_table(TableName=table.name)['Table']
            assert 'StreamSpecification' in desc
            assert desc['StreamSpecification']['StreamEnabled'] == True
            assert desc['StreamSpecification']['StreamViewType'] == 'KEYS_ONLY'
            assert 'LatestStreamArn' not in desc

            # Double-enable must be rejected while ENABLING.
            with pytest.raises(ClientError, match='ValidationException.*already has an enabled stream'):
                table.update(StreamSpecification={
                    'StreamEnabled': True, 'StreamViewType': 'KEYS_ONLY'})

            ks = f'alternator_{table.name}'
            table_id = get_table_id(ks, table.name)
            count = get_tablet_count(table_id)
            assert count == 4

            # Shrinkage attempt (ENABLING): must be BLOCKED.
            # Setting target to 2 would normally trigger a merge (4 -> 2),
            # but tablet_merge_blocked prevents it.
            set_tablet_target(ks, table.name, 2)
            assert_tablet_count_stable(table_id, 4, duration=5)

            # Increase (ENABLING): must succeed (4 -> 8).
            set_tablet_target(ks, table.name, 8)
            wait_for_tablet_count(table_id, 8)
        except:
            table.delete()
            raise
    # <-- delay_cdc_stream_finalization disabled; finalization proceeds.

    # === Phase 2: Transition to ENABLED ===
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            desc = table.meta.client.describe_table(TableName=table.name)['Table']
            if 'LatestStreamArn' in desc:
                break
            time.sleep(0.1)
        else:
            pytest.fail("Stream did not reach ENABLED state within timeout")

        count = get_tablet_count(table_id)
        assert count == 8

        # === Phase 3: ENABLED state ===
        # Shrinkage attempt (ENABLED): must be BLOCKED.
        set_tablet_target(ks, table.name, 4)
        assert_tablet_count_stable(table_id, 8, duration=5)

        # Increase (ENABLED): must succeed (8 -> 16).
        set_tablet_target(ks, table.name, 16)
        wait_for_tablet_count(table_id, 16)

        # === Phase 4: Disable streams, merges must be unblocked ===
        table.update(StreamSpecification={'StreamEnabled': False})

        # Shrinkage (streams disabled): must SUCCEED (16 -> 8).
        set_tablet_target(ks, table.name, 8)
        wait_for_tablet_count(table_id, 8)
    finally:
        table.delete()
        table.meta.client.get_waiter('table_not_exists').wait(TableName=table.name)

# For a while (see #18068) it was possible to create an Alternator table with
# tablets enabled and choose LWT for write isolation (always_use_lwt)
# but the writes themselves failed. This test verifies that this is no longer
# the case, and the LWT writes succeed even when tablets are used.
def test_alternator_tablets_and_lwt(dynamodb):
    schema = {
        'Tags': [
            {'Key': initial_tablets_tag, 'Value': '0'},
            {'Key': 'system:write_isolation', 'Value': 'always_use_lwt'}],
        'KeySchema': [ { 'AttributeName': 'p', 'KeyType': 'HASH' } ],
        'AttributeDefinitions': [ { 'AttributeName': 'p', 'AttributeType': 'S' }]}
    with new_test_table(dynamodb, **schema) as table:
        assert_tablets_usage_follows_config(dynamodb, table)
        # This put_item() failed before #18068 was fixed:
        table.put_item(Item={'p': 'hello'})
        assert table.get_item(Key={'p': 'hello'}, ConsistentRead=True)['Item'] == {'p': 'hello'}

# An Alternator table created with tablets and with a write isolation
# mode that doesn't use LWT ("forbid_rmw") works normally, even
# before #18068 is fixed.
def test_alternator_tablets_without_lwt(dynamodb):
    schema = {
        'Tags': [
            {'Key': initial_tablets_tag, 'Value': '0'},
            {'Key': 'system:write_isolation', 'Value': 'forbid_rmw'}],
        'KeySchema': [ { 'AttributeName': 'p', 'KeyType': 'HASH' } ],
        'AttributeDefinitions': [ { 'AttributeName': 'p', 'AttributeType': 'S' }]}
    with new_test_table(dynamodb, **schema) as table:
        assert_tablets_usage_follows_config(dynamodb, table)
        table.put_item(Item={'p': 'hello'})
        assert table.get_item(Key={'p': 'hello'})['Item'] == {'p': 'hello'}
