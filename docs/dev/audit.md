# Introduction

Similar to the approach described in CASSANDRA-12151, we add the
concept of an audit specification.  An audit has a target (syslog or a
table) and a set of events/actions that it wants recorded.

The current implementation uses `scylla.yaml` configuration parameters
(`audit`, `audit_categories`, `audit_keyspaces`, `audit_tables`) for
audit configuration.  Auditing covers both CQL and Alternator
(DynamoDB-compatible API) requests.

Prior art:
- Microsoft SQL Server [audit
  description](https://docs.microsoft.com/en-us/sql/relational-databases/security/auditing/sql-server-audit-database-engine?view=sql-server-ver15)
- pgAudit [docs](https://github.com/pgaudit/pgaudit/blob/master/README.md)
- MySQL audit_log docs in
  [MySQL](https://dev.mysql.com/doc/refman/8.0/en/audit-log.html) and
  [Azure](https://docs.microsoft.com/en-us/azure/mysql/concepts-audit-logs)
- DynamoDB can [use CloudTrail](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/logging-using-cloudtrail.html) to log all events

# CQL extensions (design proposal, not yet implemented)

> **Note:** The CQL syntax described in this section (`CREATE AUDIT`,
> `DESCRIBE AUDIT`, `DROP AUDIT`, `ALTER AUDIT`) and the associated
> permission model are **design proposals** that have not been
> implemented.  The actual audit configuration is done via `scylla.yaml`
> parameters as described in the
> [operator-facing auditing guide](https://docs.scylladb.com/operating-scylla/security/auditing/).

## Create an audit

```cql
CREATE AUDIT [IF NOT EXISTS] audit-name WITH TARGET { SYSLOG | table-name }
[ AND TRIGGER KEYSPACE IN (ks1, ks2, ks3) ]
[ AND TRIGGER TABLE IN (tbl1, tbl2, tbl3) ]
[ AND TRIGGER ROLE IN (usr1, usr2, usr3) ]
[ AND TRIGGER CATEGORY IN (cat1, cat2, cat3) ]
;
```

From this point on, every database event that matches all present
triggers will be recorded in the target.  When the target is a table,
it behaves like the [current
design](https://docs.scylladb.com/operating-scylla/security/auditing/#table-storage).

The audit name must be different from all other audits, unless IF NOT
EXISTS precedes it, in which case the existing audit must be identical
to the new definition.  Case sensitivity and length limit are the same
as for table names.

A trigger kind (ie, `KEYSPACE`, `TABLE`, `ROLE`, or `CATEGORY`) can be
specified at most once.

## Show an audit

```cql
DESCRIBE AUDIT [audit-name ...];
```

Prints definitions of all audits named herein.  If no names are
provided, prints all audits.

## Delete an audit

```cql
DROP AUDIT audit-name;
```

Stops logging events specified by this audit.  Doesn't impact the
already logged events.  If the target is a table, it remains as it is.

## Alter an audit

```cql
ALTER AUDIT audit-name WITH {same syntax as CREATE}
```

Any trigger provided will be updated (or newly created, if previously
absent).  To drop a trigger, use `IN *`.

## Permissions

Only superusers can modify audits or turn them on and off.

Only superusers can read tables that are audit targets; no user can
modify them.  Only superusers can drop tables that are audit targets,
after the audit itself is dropped.  If a superuser doesn't drop a
target table, it remains in existence indefinitely.

# Implementation

## Current implementation

Audit configuration is driven by the following `scylla.yaml` parameters
(see `db/config.cc`):

- `audit` -- audit mode: `"none"`, `"table"`, `"syslog"`, or
  `"syslog,table"` (default: `"table"`)
- `audit_categories` -- comma-separated list of categories to audit:
  `AUTH`, `DML`, `DDL`, `DCL`, `QUERY`, `ADMIN` (default:
  `"DCL,AUTH,ADMIN"`)
- `audit_keyspaces` -- comma-separated list of keyspaces to audit
  (default: `""`)
- `audit_tables` -- comma-separated `<keyspace_name>.<table_name>` pairs to audit
  (default: `""`).  For Alternator tables, the format `alternator.<table_name>` is
  used and expanded by `parse_audit_tables()` to the real keyspace
  name `alternator_<table_name>` (see below).

The `audit_categories`, `audit_keyspaces`, and `audit_tables` parameters
support live updates via `system.config` without requiring a node
restart.  The `audit` parameter (backend selection) requires a restart.

### Core classes

- **`audit::audit`** (`audit/audit.cc`) -- the main audit service.
  Owns the storage helpers and the filtering state (audited categories,
  keyspaces, tables).  Key methods:
  - `log()` -- records an audit event to the configured backend(s)
  - `should_log()` / `should_log_table()` -- checks whether a given
    category + keyspace + table combination should be audited
  - `will_log(cat, keyspace, table)` -- lightweight pre-check used by
    Alternator to short-circuit before expensive JSON serialization

- **`audit::audit_info`** (`audit/audit.hh`) -- base class carrying
  per-request audit metadata (category, keyspace, table, query string,
  batch flag).  Created by `audit::create_audit_info()` for CQL
  statements.

- **`audit::inspect()`** (`audit/audit.cc`) -- called after a CQL
  statement or Alternator operation completes (or fails) to log the
  audit event.  Two overloads exist: one for CQL (extracts CL from
  `query_options`), one for Alternator (extracts CL from
  `audit_info_alternator`).

### Storage helpers

- **`audit_cf_storage_helper`** -- writes audit events to the
  `audit.audit_log` table (CL=ONE)
- **`audit_syslog_storage_helper`** -- writes audit events to syslog
  via a Unix socket
- **`audit_composite_storage_helper`** -- delegates to both of the above

## Alternator auditing

Alternator auditing was added in PR
[#27953](https://github.com/scylladb/scylladb/pull/27953).  It reuses
the existing audit infrastructure, requiring no new configuration
options.

### Architecture

**`audit::audit_info_alternator`** (`audit/audit.hh`) -- a subclass of
`audit_info` for Alternator requests.  Unlike CQL (where CL comes from
`cql3::query_options`), Alternator has no `query_options`, so the CL is
stored directly in this object.  The `batch` flag is always `false`
(CQL-style batch unpacking does not apply).

**`executor::maybe_audit()`** (`alternator/executor.cc`) -- the central
integration point.  Every Alternator operation handler calls this
method.  It:

1. Calls `audit::will_log()` to check whether the operation's
   category/keyspace would be audited.  This is a lightweight check
   that avoids expensive work when auditing is disabled or filtered out.
2. Only if auditing applies, allocates an `audit_info_alternator` and
   serializes the JSON request body via `rjson::print(request)` into
   the query string.

The query string is set via `audit_info::set_query_string(operation,
query)`, which formats it as `<OperationName>|<JSON request body>`.

After the operation completes (successfully or with an error),
`audit::inspect(ai, client_state, error)` is called to log the event.

### Operation-to-category mapping

| Category | Operations |
|----------|------------|
| DDL      | CreateTable, DeleteTable, UpdateTable, TagResource, UntagResource, UpdateTimeToLive |
| DML      | PutItem, UpdateItem, DeleteItem, BatchWriteItem |
| QUERY    | GetItem, BatchGetItem, Query, Scan, DescribeTable, ListTables, DescribeEndpoints, ListTagsOfResource, DescribeTimeToLive, DescribeContinuousBackups, ListStreams, DescribeStream, GetShardIterator, GetRecords |

AUTH, DCL, and ADMIN categories have no Alternator equivalents.

### Keyspace and table in audit entries

- The real keyspace name of an Alternator table `T` is
  `alternator_T`.  The `keyspace_name` and `table_name` fields in audit
  entries use these real keyspace names.
- The `audit_tables` config flag uses the shorthand `alternator.T` to
  refer to Alternator tables.  `parse_audit_tables()` expands this to
  the real keyspace name `alternator_T` with table `T`.
- **Global operations** (ListTables, DescribeEndpoints) have no
  associated keyspace/table, so both fields are empty.
- **Batch operations** (BatchWriteItem, BatchGetItem) may span
  multiple tables.  The `keyspace_name` field is empty; `table_name`
  contains the involved table names separated by `|`.
- **Streams operations** (DescribeStream, GetShardIterator, GetRecords)
  record the `table_name` as `base_table|cdc_log_table`.

### Known limitations and future work

- **Batch keyspace filtering bypass**: `will_log()` is called with an
  empty keyspace for batch operations, so `audit_keyspaces` /
  `audit_tables` filtering is bypassed.  The batch is audited as a
  whole whenever its [category](#operation-to-category-mapping) is enabled.
- **Large operation payloads**: The full JSON request body is
  serialized into the `operation` field.  For BatchWriteItem this can
  be up to 16 MB (`rjson::print(request)` is called only after
  `will_log()` confirms auditing is needed).
- **No Alternator-native API to read audit entries**: The current implementation
  extends ScyllaDB's existing CQL-oriented audit system.
  DynamoDB-compatible data event logging (CloudTrail-style) is tracked
  in [#9226](https://github.com/scylladb/scylladb/issues/9226).

## Proposed CQL-driven implementation (not fully implemented)

> The following describes the originally proposed trie-based trigger
> evaluation and system-table persistence.  These have not been
> implemented; the current implementation uses config-file-based
> filtering with `std::set`/`std::map` lookups (see `audit::should_log()`
> and `audit::will_log()` in `audit/audit.cc`).

### Efficient trigger evaluation

```c++
namespace audit {

/// Stores triggers from an AUDIT statement.
class triggers {
    // Use trie structures for speedy string lookup.
    optional<trie> _ks_trigger, _tbl_trigger, _usr_trigger;

    // A logical-AND filter.
    optional<unsigned> _cat_trigger;

public:
    /// True iff every non-null trigger matches the corresponding ainf element.
    bool should_audit(const audit_info& ainf);
};

} // namespace audit
```

To prevent modification of target tables, `audit::inspect()` will
check the statement and throw if it is disallowed, similar to what
`check_access()` currently does.

> **Note:** In the current implementation, `audit::inspect()` serves as
> a logging hook only -- it does not enforce access control or throw on
> disallowed operations.

### Persisting audit definitions

Obviously, an audit definition must survive a server restart and stay
consistent among all nodes in a cluster.  We'll accomplish both by
storing audits in a system table.
