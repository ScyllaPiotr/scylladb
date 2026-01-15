# Copyright 2025-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0

# Tests for Alternator integration with Scylla's audit logging.

import time

import pytest
from cassandra import InvalidRequest

from test.alternator.util import new_test_table, unique_table_name


# Shared table schemas reused across audit tests.
HASH_AND_RANGE_SCHEMA = {
    "KeySchema": [
        {"AttributeName": "p", "KeyType": "HASH"},
        {"AttributeName": "c", "KeyType": "RANGE"},
    ],
    "AttributeDefinitions": [
        {"AttributeName": "p", "AttributeType": "S"},
        {"AttributeName": "c", "AttributeType": "S"},
    ],
}

HASH_ONLY_SCHEMA = {
    "KeySchema": [{"AttributeName": "p", "KeyType": "HASH"}],
    "AttributeDefinitions": [{"AttributeName": "p", "AttributeType": "S"}],
}


# Returns the number of entries in the audit log table.
def _get_audit_log_count(cql):
    try:
        row = cql.execute("SELECT count(*) FROM audit.audit_log").one()
    except InvalidRequest:
        return 0
    return row[0]


def _get_audit_log_rows(cql):
    try:
        return list(cql.execute("SELECT * FROM audit.audit_log"))
    except InvalidRequest:
        # Auditing table may not exist yet
        return []


# Waits until the audit log has grown by at least `min_delta` entries, or fails after `timeout` seconds.
def _wait_for_audit_log_growth(cql, initial_count, min_delta=1, timeout=10):
    deadline = time.time() + timeout
    last = initial_count
    while time.time() < deadline:
        current = _get_audit_log_count(cql)
        if current - initial_count >= min_delta:
            return
        last = current
        time.sleep(0.1)
    pytest.fail(f"Audit log did not grow by at least {min_delta} entries (before={initial_count}, after={last})")


def _get_new_audit_log_rows(cql, rows_before, expected_new_row_count, timeout=10):
    before_count = len(rows_before)
    before_set = set(rows_before)
    _wait_for_audit_log_growth(cql, before_count, min_delta=expected_new_row_count, timeout=timeout)
    rows_after = _get_audit_log_rows(cql)
    new_rows = [row for row in rows_after if row not in before_set]
    return new_rows


def _simplify_rows(rows):
    # Map raw audit rows to a subset of fields we care about.
    simplified = []
    for row in rows:
        simplified.append(
            (
                row.category,
                row.consistency,
                bool(row.error),
                row.keyspace_name,
                row.table_name,
                row.operation,
            )
        )
    return simplified


# Verify audit entries against expected values:
# 1) Optionally filter rows by ks_name/table_name when provided (not None).
# 2) Check that the count of relevant entries matches the expected count.
# 3) Compare category, consistency, error (bool), keyspace_name and table_name field-by-field.
# 4) When table_name is provided and non-empty, assert it appears in the operation text.
# 5) Assert that all fragment strings from the expected tuple appear in the operation text.
def _assert_audit_entries(rows, expected, ks_name=None, table_name=None):
    # When ks_name or table_name is provided, filter to matching entries only.
    if ks_name is not None or table_name is not None:
        def is_relevant(r):
            if ks_name is not None and r.keyspace_name != ks_name:
                return False
            if table_name is not None and r.table_name != table_name:
                return False
            return True
        irrelevant = [(idx, row) for idx, row in enumerate(rows) if not is_relevant(row)]
        if len(irrelevant) > 0:
            print(f"Found {len(irrelevant)} irrelevant audit entries at indices {[idx for idx, _ in irrelevant]}: {irrelevant}")
        relevant = [row for row in rows if is_relevant(row)]
    else:
        relevant = list(rows)
    assert len(relevant) == len(expected), f"Expected {len(expected)} audit entries, got {len(relevant)}: {relevant}"

    # Include the operation text in the simplified actual rows, and keep the
    # expected structure as (category, consistency, error, keyspace_name,
    # table_name, [fragments...]). Sort both lists by the first five fields
    # and then compare element-by-element, treating the last field specially.
    actual_simple = sorted(_simplify_rows(relevant), key=lambda r: r[:5])
    expected_simple = sorted(expected, key=lambda e: e[:5])

    assert len(actual_simple) == len(expected_simple), f"Unexpected audit entries: expected={expected_simple}, actual={actual_simple}"

    for actual_entry, expected_entry in zip(actual_simple, expected_simple):
        # Compare the basic audit fields one-to-one.
        assert actual_entry[:5] == expected_entry[:5], f"Unexpected audit entry fields: expected={expected_entry[:5]}, actual={actual_entry[:5]}"
        actual_operation = actual_entry[5]
        expected_fragments = expected_entry[5]
        # Basic sanity for the recorded operation text.
        assert actual_operation, "Audit entry has empty operation string"
        if table_name:
            assert table_name in actual_operation, f"Table name {table_name} not found in operation {actual_operation}"
        # The last element of the expected tuple is a list of fragments
        # that should all appear in the operation text.
        for fragment in expected_fragments:
            assert fragment in actual_operation, f"Expected substring '{fragment}' not found in operation {actual_operation}"


# system.config stores values as JSON-encoded strings with surrounding quotes.
# Strip them so that writing back via a parameterized UPDATE doesn't double-quote.
def _strip_config_quotes(val):
    if val and val.startswith('"') and val.endswith('"'):
        return val[1:-1]
    return val


# A fixture to enable auditing for all audit categories for the duration of the test.
# The main config flag "audit" is not live updatable, so it is required to be already enabled.
# After the test, the previous audit settings are restored.
@pytest.fixture(scope="function")
def alternator_audit_enabled(cql):
    # Store current values of "audit_categories", "audit_keyspaces" in the system.config table
    names = ("audit_categories", "audit_keyspaces")
    names_serialized = ", ".join(f"'{n}'" for n in names)

    def read_config_vals(names_serialized):
        rows = cql.execute(f"SELECT name, value FROM system.config WHERE name IN ({names_serialized})")
        return {row.name: row.value for row in rows}

    original_config_vals = read_config_vals(names_serialized)

    def get_original_config_vals(name, default):
        val = original_config_vals[name] if name in original_config_vals and original_config_vals[name] is not None else default
        return _strip_config_quotes(val)

    try:
        # Enable auditing for all categories of operations
        # Note: "audit" itself is not changed here, assuming that auditing is already enabled
        cql.execute(
            "UPDATE system.config SET value=%s WHERE name='audit_categories'",
            ("ADMIN,AUTH,QUERY,DML,DDL,DCL",),
        )
        yield
    finally:
        # Restore previous values of "audit_categories", "audit_keyspaces" in the system.config table and verify the restoration
        for name in names:
            if name in original_config_vals:
                original_val = get_original_config_vals(name, "")
                cql.execute("UPDATE system.config SET value=%s WHERE name=%s", (original_val, name))
                restored = cql.execute("SELECT value FROM system.config WHERE name=%s", (name,)).one()
                restored_value = _strip_config_quotes(restored.value) if restored else None
                assert restored_value == original_val, (
                    f"Config '{name}' not properly restored: expected '{original_val}', got '{restored_value}'"
                )
            else:
                # If the key wasn't present before the test, remove it so we don't leave test artifacts behind
                cql.execute("DELETE FROM system.config WHERE name=%s", (name,))

def _wait_for_active_stream(client, table_name, timeout=10):
    # Wait until the table has an active stream and return the stream ARN.
    deadline = time.time() + timeout
    while time.time() < deadline:
        desc = client.describe_table(TableName=table_name)
        stream_spec = desc['Table'].get('StreamSpecification', {})
        if stream_spec.get('StreamEnabled'):
            latest_arn = desc['Table'].get('LatestStreamArn')
            if latest_arn:
                return latest_arn
        time.sleep(0.1)
    pytest.fail(f"Stream did not become active for table {table_name} within {timeout}s")


# Test auditing of DML item operations: PutItem, UpdateItem, DeleteItem.
# Each operation is called 16 times (4x4 hash/range combinations), producing 48 audit entries total.
def test_audit_dml_operations(scylla_only, dynamodb, cql, alternator_audit_enabled):
    # Use a schema with both hash and range keys, to allow more varied Query-s.
    with new_test_table(dynamodb, **HASH_AND_RANGE_SCHEMA) as table:
        ks_name = f"alternator_{table.name}"
        # Enable audit for the current table's keyspace. The `alternator_audit_enabled` fixture
        # ensures that `audit_keyspaces` in system.config has been already stored too and will be
        # restored after the test.
        cql.execute("UPDATE system.config SET value=%s WHERE name='audit_keyspaces'", (ks_name,))
        before_rows = _get_audit_log_rows(cql)
        tab = dynamodb.Table(table.name)
        # The format inside expected is: (category, consistency, error(bool), keyspace_name, table_name, [fragments that should appear in the operation text])
        expected = []
        # PutItem
        for hash_i in range(4):
            for range_i in range(4):
                tab.put_item(Item={"p": f"pk_{hash_i}", "c": f"ck_{range_i}", "v": f"item_{hash_i}_{range_i}"})
                expected.append(("DML", "LOCAL_QUORUM", False, ks_name, table.name, ["PutItem", f"pk_{hash_i}", f"ck_{range_i}", f"item_{hash_i}_{range_i}"]))
        # UpdateItem
        for hash_i in range(4):
            for range_i in range(4):
                tab.update_item(Key={"p": f"pk_{hash_i}", "c": f"ck_{range_i}"}, AttributeUpdates={"v": {"Value": f"updated_item_{hash_i}_{range_i}", "Action": "PUT"}})
                expected.append(("DML", "LOCAL_QUORUM", False, ks_name, table.name, ["UpdateItem", f"pk_{hash_i}", f"ck_{range_i}", f"updated_item_{hash_i}_{range_i}"]))
        # DeleteItem
        for hash_i in range(4):
            for range_i in range(4):
                tab.delete_item(Key={"p": f"pk_{hash_i}", "c": f"ck_{range_i}"})
                expected.append(("DML", "LOCAL_QUORUM", False, ks_name, table.name, ["DeleteItem", f"pk_{hash_i}", f"ck_{range_i}"]))
        # Each individual Alternator call above must be audited.
        new_rows = _get_new_audit_log_rows(cql, before_rows, expected_new_row_count=len(expected))
        _assert_audit_entries(new_rows, expected, ks_name, table.name)


# Test auditing of the DML batch operation: BatchWriteItem.
# A single BatchWriteItem call produces one audit entry regardless of the number of items in the batch.
# Note: table.meta.client has boto3's TransformationInjector which converts native Python values
# to DynamoDB JSON, so we pass native values (not {"S": "..."} format) to avoid double-wrapping.
# Batch operations leave keyspace_name empty because they can span multiple tables.
def test_audit_dml_batch_operations(scylla_only, dynamodb, cql, alternator_audit_enabled):
    with new_test_table(dynamodb, **HASH_ONLY_SCHEMA) as table:
        ks_name = f"alternator_{table.name}"
        # Enable audit for the current table's keyspace. The `alternator_audit_enabled` fixture
        # ensures that `audit_keyspaces` in system.config has been already stored too and will be
        # restored after the test.
        # Include empty keyspace (trailing comma) because batch operations leave keyspace_name empty.
        cql.execute("UPDATE system.config SET value=%s WHERE name='audit_keyspaces'", (f"{ks_name},",))
        before_rows = _get_audit_log_rows(cql)
        client = table.meta.client
        # BatchWriteItem with PutRequest items targeting a single table.
        client.batch_write_item(RequestItems={
            table.name: [{"PutRequest": {"Item": {"p": f"pk_{i}", "v": f"val_{i}"}}} for i in range(4)]
        })
        # The format inside expected is: (category, consistency, error(bool), keyspace_name, table_name, [fragments that should appear in the operation text])
        expected = [
            ("DML", "LOCAL_QUORUM", False, "", table.name, ["BatchWriteItem", "pk_0", "val_0", "pk_1", "val_1", "pk_2", "val_2", "pk_3", "val_3"]),
        ]
        # Each individual Alternator call above must be audited.
        new_rows = _get_new_audit_log_rows(cql, before_rows, expected_new_row_count=len(expected))
        _assert_audit_entries(new_rows, expected, table_name=table.name)


# Test auditing of QUERY item operations: GetItem, Query, Scan.
# GetItem is called 16 times (4x4), Query 4 times (2 range + 2 exact), Scan 4 times = 24 entries total.
def test_audit_query_item_operations(scylla_only, dynamodb, cql, alternator_audit_enabled):
    # Use a schema with both hash and range keys, to allow more varied Query-s.
    with new_test_table(dynamodb, **HASH_AND_RANGE_SCHEMA) as table:
        ks_name = f"alternator_{table.name}"
        tab = dynamodb.Table(table.name)
        # Pre-populate the table so that reads have data to return.
        for hash_i in range(4):
            for range_i in range(4):
                tab.put_item(Item={"p": f"pk_{hash_i}", "c": f"ck_{range_i}", "v": f"item_{hash_i}_{range_i}"})
        # Enable audit for the current table's keyspace. The `alternator_audit_enabled` fixture
        # ensures that `audit_keyspaces` in system.config has been already stored too and will be
        # restored after the test.
        cql.execute("UPDATE system.config SET value=%s WHERE name='audit_keyspaces'", (ks_name,))
        before_rows = _get_audit_log_rows(cql)
        # The format inside expected is: (category, consistency, error(bool), keyspace_name, table_name, [fragments that should appear in the operation text])
        expected = []
        # GetItem, with deterministically chosen consistency.
        for hash_i in range(4):
            for range_i in range(4):
                strongly_consistent = bool((hash_i + range_i) % 2)
                tab.get_item(Key={"p": f"pk_{hash_i}", "c": f"ck_{range_i}"}, ConsistentRead=True if strongly_consistent else None)
                expected.append(("QUERY", "LOCAL_QUORUM" if strongly_consistent else "LOCAL_ONE", False, ks_name, table.name, ["GetItem", f"pk_{hash_i}", f"ck_{range_i}"]))
        # Query, with both exact and range predicates.
        for hash_i in range(2):
            strongly_consistent = bool(hash_i % 2)
            tab.query(
                KeyConditionExpression="#p = :pval AND #c BETWEEN :cmin AND :cmax",
                ExpressionAttributeNames={"#p": "p", "#c": "c"},
                ExpressionAttributeValues={
                    ":pval": f"pk_{hash_i}",
                    ":cmin": f"ck_{hash_i}",
                    ":cmax": f"ck_{hash_i+2}",
                },
                ConsistentRead=True if strongly_consistent else None,
                Limit=2,
            )
            expected.append(("QUERY", "LOCAL_QUORUM" if strongly_consistent else "LOCAL_ONE", False, ks_name, table.name, ["Query", "BETWEEN", f"pk_{hash_i}", f"ck_{hash_i}", f"ck_{hash_i+2}"]))
            tab.query(
                KeyConditionExpression="#p = :pval AND #c = :cval",
                ExpressionAttributeNames={"#p": "p", "#c": "c"},
                ExpressionAttributeValues={
                    ":pval": f"pk_{hash_i}",
                    ":cval": f"ck_{hash_i+1}",
                },
                ConsistentRead=True if strongly_consistent else None,
                Limit=1,
            )
            expected.append(("QUERY", "LOCAL_QUORUM" if strongly_consistent else "LOCAL_ONE", False, ks_name, table.name, ["Query", f"pk_{hash_i}", f"ck_{hash_i+1}"]))
        # Scan
        for i in range(4):
            strongly_consistent = bool(i % 2)
            use_filter = (i // 2) % 2  # use FilterExpression for the latter two loop runs
            if use_filter:
                tab.scan(
                    FilterExpression="#v = :vval",
                    ExpressionAttributeNames={"#v": "v"},
                    ExpressionAttributeValues={":vval": f"item_{i}_{i}"},
                    ConsistentRead=True if strongly_consistent else None,
                )
                expected.append(("QUERY", "LOCAL_QUORUM" if strongly_consistent else "LOCAL_ONE", False, ks_name, table.name, ["Scan", f"item_{i}_{i}"]))
            else:
                tab.scan(ConsistentRead=True if strongly_consistent else None)
                expected.append(("QUERY", "LOCAL_QUORUM" if strongly_consistent else "LOCAL_ONE", False, ks_name, table.name, ["Scan"]))
        # Each individual Alternator call above must be audited.
        new_rows = _get_new_audit_log_rows(cql, before_rows, expected_new_row_count=len(expected))
        _assert_audit_entries(new_rows, expected, ks_name, table.name)


# Test auditing of the QUERY batch operation: BatchGetItem.
# A single BatchGetItem call produces one audit entry.
# The audit entry records CL=ANY as a placeholder; per-item consistency is set individually.
# Note: table.meta.client has boto3's TransformationInjector (see test_audit_dml_batch_operations).
# Batch operations leave keyspace_name empty because they can span multiple tables.
def test_audit_query_batch_operations(scylla_only, dynamodb, cql, alternator_audit_enabled):
    with new_test_table(dynamodb, **HASH_ONLY_SCHEMA) as table:
        ks_name = f"alternator_{table.name}"
        # Pre-populate the table.
        for i in range(4):
            table.put_item(Item={"p": f"pk_{i}"})
        # Enable audit for the current table's keyspace. The `alternator_audit_enabled` fixture
        # ensures that `audit_keyspaces` in system.config has been already stored too and will be
        # restored after the test.
        # Include empty keyspace (trailing comma) because batch operations leave keyspace_name empty.
        cql.execute("UPDATE system.config SET value=%s WHERE name='audit_keyspaces'", (f"{ks_name},",))
        before_rows = _get_audit_log_rows(cql)
        client = table.meta.client
        client.batch_get_item(RequestItems={table.name: {"Keys": [{"p": f"pk_{i}"} for i in range(4)]}})
        expected = [("QUERY", "ANY", False, "", table.name, ["BatchGetItem", "pk_0", "pk_1", "pk_2", "pk_3"]),]
        # Each individual Alternator call above must be audited.
        new_rows = _get_new_audit_log_rows(cql, before_rows, expected_new_row_count=len(expected))
        _assert_audit_entries(new_rows, expected, table_name=table.name)


# Test auditing of DDL operations: CreateTable, UpdateTable (with GSI),
# TagResource, UntagResource, UpdateTimeToLive, DeleteTable.
# DDL operations use CL=SERIAL because they go through group0 (Raft-based schema changes).
# The DescribeTable call (used to fetch the TableArn) also produces a QUERY entry.
# Produces 7 audit entries.
def test_audit_ddl_operations(scylla_only, dynamodb, cql, alternator_audit_enabled):
    client = dynamodb.meta.client
    table_name = unique_table_name()
    ks_name = f"alternator_{table_name}"
    # Enable audit for the current table's keyspace. The `alternator_audit_enabled` fixture
    # ensures that `audit_keyspaces` in system.config has been already stored too and will be
    # restored after the test.
    cql.execute("UPDATE system.config SET value=%s WHERE name='audit_keyspaces'", (ks_name,))
    before_rows = _get_audit_log_rows(cql)
    # The format inside expected is: (category, consistency, error(bool), keyspace_name, table_name, [fragments that should appear in the operation text])
    expected = []
    try:
        # CreateTable
        client.create_table(
            TableName=table_name,
            KeySchema=HASH_ONLY_SCHEMA["KeySchema"],
            AttributeDefinitions=HASH_ONLY_SCHEMA["AttributeDefinitions"],
            BillingMode='PAY_PER_REQUEST',
        )
        expected.append(("DDL", "SERIAL", False, ks_name, table_name, ["CreateTable", table_name]))
        # Get TableArn via describe_table (CreateTable response may omit it in Alternator).
        desc = client.describe_table(TableName=table_name)
        table_arn = desc['Table']['TableArn']
        expected.append(("QUERY", "LOCAL_ONE", False, ks_name, table_name, ["DescribeTable", table_name]))
        # UpdateTable - add a GSI which requires a new attribute definition.
        # Only declare the new GSI key attribute; re-declaring existing table
        # key attributes (e.g. "p") is rejected by Scylla as spurious.
        client.update_table(
            TableName=table_name,
            AttributeDefinitions=[
                {"AttributeName": "x", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexUpdates=[{
                "Create": {
                    "IndexName": "x_index",
                    "KeySchema": [{"AttributeName": "x", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            }],
        )
        expected.append(("DDL", "SERIAL", False, ks_name, table_name, ["UpdateTable", table_name, "x_index"]))
        # TagResource
        client.tag_resource(ResourceArn=table_arn, Tags=[{"Key": "env", "Value": "test"}])
        expected.append(("DDL", "SERIAL", False, ks_name, table_name, ["TagResource", "env", "test"]))
        # UntagResource
        client.untag_resource(ResourceArn=table_arn, TagKeys=["env"])
        expected.append(("DDL", "SERIAL", False, ks_name, table_name, ["UntagResource", "env"]))
        # UpdateTimeToLive
        client.update_time_to_live(
            TableName=table_name,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
        )
        expected.append(("DDL", "SERIAL", False, ks_name, table_name, ["UpdateTimeToLive", table_name, "ttl"]))
        # DeleteTable
        client.delete_table(TableName=table_name)
        expected.append(("DDL", "SERIAL", False, ks_name, table_name, ["DeleteTable", table_name]))
        # Each individual Alternator call above must be audited.
        new_rows = _get_new_audit_log_rows(cql, before_rows, expected_new_row_count=len(expected))
        _assert_audit_entries(new_rows, expected, ks_name, table_name)
    finally:
        try:
            client.delete_table(TableName=table_name)
        except Exception:
            pass


# Test auditing of QUERY table-level operations: DescribeTable, ListTagsOfResource,
# DescribeTimeToLive, DescribeContinuousBackups, ListTables, DescribeEndpoints.
# ListTables and DescribeEndpoints have empty keyspace/table, so the audit_keyspaces
# filter includes a trailing comma to match the empty keyspace as well.
# Produces 6 audit entries.
def test_audit_query_table_operations(scylla_only, dynamodb, cql, alternator_audit_enabled):
    with new_test_table(dynamodb, **HASH_ONLY_SCHEMA) as table:
        ks_name = f"alternator_{table.name}"
        # Enable audit for the current table's keyspace. The `alternator_audit_enabled` fixture
        # ensures that `audit_keyspaces` in system.config has been already stored too and will be
        # restored after the test.
        # Include empty keyspace (trailing comma) for ListTables/DescribeEndpoints.
        cql.execute("UPDATE system.config SET value=%s WHERE name='audit_keyspaces'", (f"{ks_name},",))
        before_rows = _get_audit_log_rows(cql)
        expected = []
        client = table.meta.client
        # DescribeTable
        desc = client.describe_table(TableName=table.name)
        table_arn = desc['Table']['TableArn']
        expected.append(("QUERY", "LOCAL_ONE", False, ks_name, table.name, ["DescribeTable", table.name]))
        # ListTagsOfResource
        client.list_tags_of_resource(ResourceArn=table_arn)
        expected.append(("QUERY", "LOCAL_ONE", False, ks_name, table.name, ["ListTagsOfResource", table_arn]))
        # DescribeTimeToLive
        client.describe_time_to_live(TableName=table.name)
        expected.append(("QUERY", "LOCAL_ONE", False, ks_name, table.name, ["DescribeTimeToLive", table.name]))
        # DescribeContinuousBackups
        client.describe_continuous_backups(TableName=table.name)
        expected.append(("QUERY", "LOCAL_ONE", False, ks_name, table.name, ["DescribeContinuousBackups", table.name]))
        # ListTables (empty keyspace)
        client.list_tables()
        expected.append(("QUERY", "LOCAL_ONE", False, "", "", ["ListTables"]))
        # DescribeEndpoints (empty keyspace)
        client.describe_endpoints()
        expected.append(("QUERY", "LOCAL_ONE", False, "", "", ["DescribeEndpoints"]))
        # Each individual Alternator call above must be audited.
        new_rows = _get_new_audit_log_rows(cql, before_rows, expected_new_row_count=len(expected))
        _assert_audit_entries(new_rows, expected)


# Test auditing of DynamoDB Streams operations: ListStreams, DescribeStream, GetShardIterator, GetRecords.
# Each operation's audit entry uses different keyspace/table naming conventions:
#   - ListStreams: keyspace is empty (like batch ops, it can span multiple tables),
#     table is the CDC log table name. CL=LOCAL_ONE.
#   - DescribeStream: keyspace is the CDC log table's keyspace,
#     table is pipe-separated "base_table|cdc_table".
#     CL is QUORUM for multi-node clusters, ONE for single-node (tests run single-node).
#   - GetShardIterator: keyspace is the CDC log table's keyspace,
#     table is pipe-separated "base_table|cdc_table". CL=LOCAL_ONE.
#   - GetRecords: keyspace is the CDC log table's keyspace,
#     table is pipe-separated "base_table|cdc_table". CL=LOCAL_QUORUM.
# Produces 4 audit entries.
def test_audit_streams_operations(scylla_only, dynamodb, dynamodbstreams, cql, alternator_audit_enabled):
    # With #23838 open, we will explicitly ask for a table with vnodes.
    with new_test_table(dynamodb, StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"}, Tags=[{'Key': 'system:initial_tablets', 'Value': 'none'}], **HASH_ONLY_SCHEMA) as table:
        ks_name = f"alternator_{table.name}"
        client = table.meta.client
        # Write data so that stream records exist.
        table.put_item(Item={"p": "pk_0"})
        stream_arn = _wait_for_active_stream(client, table.name)
        # Naming for audit entries: CDC log table names and pipe-separated base|cdc names.
        # In Alternator the base and CDC tables share the same keyspace.
        cdc_table = f"{table.name}_scylla_cdc_log"
        piped_table = f"{table.name}|{cdc_table}"
        # Enable audit for the current table's keyspace.
        # Include empty keyspace (trailing comma) because ListStreams leaves keyspace_name empty.
        # The `alternator_audit_enabled` fixture ensures that `audit_keyspaces` in system.config
        # has been already stored too and will be restored after the test.
        cql.execute("UPDATE system.config SET value=%s WHERE name='audit_keyspaces'", (f"{ks_name},",))
        before_rows = _get_audit_log_rows(cql)
        expected = []
        # ListStreams - keyspace is empty (can span multiple tables), table is the CDC log table name.
        dynamodbstreams.list_streams(TableName=table.name)
        expected.append(("QUERY", "LOCAL_ONE", False, "", cdc_table, ["ListStreams", table.name]))
        # DescribeStream - keyspace is the CDC log table's keyspace, table is pipe-separated base|cdc.
        # CL is QUORUM for multi-node clusters, ONE for single-node (our test environment).
        desc_resp = dynamodbstreams.describe_stream(StreamArn=stream_arn)
        shards = desc_resp['StreamDescription']['Shards']
        expected.append(("QUERY", "ONE", False, ks_name, piped_table, ["DescribeStream", stream_arn]))
        # GetShardIterator - keyspace is the CDC log table's keyspace, table is pipe-separated base|cdc.
        iter_resp = dynamodbstreams.get_shard_iterator(
            StreamArn=stream_arn, ShardId=shards[0]['ShardId'], ShardIteratorType='LATEST')
        expected.append(("QUERY", "LOCAL_ONE", False, ks_name, piped_table, ["GetShardIterator", stream_arn]))
        # GetRecords - keyspace is the CDC log table's keyspace, table is pipe-separated base|cdc. CL=LOCAL_QUORUM.
        dynamodbstreams.get_records(ShardIterator=iter_resp['ShardIterator'])
        expected.append(("QUERY", "LOCAL_QUORUM", False, ks_name, piped_table, ["GetRecords"]))
        # Each individual Alternator call above must be audited.
        new_rows = _get_new_audit_log_rows(cql, before_rows, expected_new_row_count=len(expected))
        _assert_audit_entries(new_rows, expected)
