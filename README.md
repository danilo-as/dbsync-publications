# dbsync-publications

Interactive CLI that syncs selected PostgreSQL tables from a source database to up to two
target databases. Data is exported to temporary CSV files and loaded with `COPY`.

## Setup

```bash
uv sync
```

Create a `.env` file in the directory where you run the CLI:

```dotenv
SOURCE_DATABASE_URL=postgresql://user:password@source-host:5432/warehouse
TARGET_1_DATABASE_URL=postgresql://user:password@target-host:5432/warehouse
# Optional second target
TARGET_2_DATABASE_URL=postgresql://user:password@other-host:5432/warehouse
```

Every target defined in `.env` is synced on each run.

## Usage

```bash
uv run dbsync-publications            # reads ./.env
uv run dbsync-publications --env-file /path/to/.env
```

The CLI asks, in order:

1. **Schema group**: publication schemas (name ends with `publications`) or other schemas.
2. **Schema**: one schema per run.
3. **Publication versions** (publication schemas only): one or more rows of
   `<schema>.publication_versions`. `publication_month` is shown when the schema has it.
4. **Tables**: multi-select. Views are not listed. `publication_versions` is synced
   automatically and is not listed.

It then validates every target, shows a plan per target and asks for confirmation.

## Sync strategies

| Table | Strategy |
|---|---|
| Publication schema, has `version_id` | Upsert the selected `publication_versions` rows, `DELETE ... WHERE version_id = ANY(...)`, then `COPY` only those versions. |
| Publication schema, no `version_id` | `TRUNCATE` + `COPY` of the whole table. Flagged with ⚠ in the table list and in the plan. |
| Publication schema without `publication_versions`, table has `version_id` | Skipped with a message. It is never truncated, because that would delete every version on the target. |
| Other schemas | `COPY` into a temporary staging table, then `INSERT ... ON CONFLICT DO NOTHING`: only new rows are added, existing rows are never updated. |

Each target is loaded in **one transaction**: if anything fails, that target is rolled back
completely and the other target is unaffected. Within the transaction, deletes and truncates
run children-first and loads run parents-first according to the foreign keys between the
selected tables.

## Validation and safety rules

- Before any write, each selected table is compared with the target by column name, data type
  and nullability (column order is ignored). A table that differs, or does not exist in the
  target, is **skipped with a message** and the other tables still sync.
- If `publication_versions` differs in a target, every `version_id` table of that schema is
  skipped for that target.
- `TRUNCATE` never uses `CASCADE`. A table referenced by a foreign key from another table is
  skipped with a message.
- Missing parent rows are copied automatically. For every foreign key from a selected table to
  an unselected table (any schema, followed recursively to parents of parents), only the parent
  rows referenced by the synced rows are exported from the source and inserted, parents first,
  with `INSERT ... ON CONFLICT DO NOTHING`. Existing parent rows are never updated or deleted.
  The plan lists every parent table that will be filled. A parent table that references itself
  is copied whole (still insert only). If a parent table differs in the target, or does not
  exist there, the selected table that needs it is skipped with a message.
- In other schemas, tables without a primary key or unique constraint show a warning:
  `ON CONFLICT` cannot detect duplicates there, so every row would be inserted again.
- Temporary CSV files live in a temporary directory that is removed at the end of the run,
  including on errors.

## Tests

```bash
uv run pytest
```

The end-to-end tests run only when `DBSYNC_TEST_ADMIN_URL` points to a role that can create
databases. They create and drop throwaway databases named `dbsync_it_*`:

```bash
DBSYNC_TEST_ADMIN_URL=postgresql://postgres:secret@localhost:5432/postgres uv run pytest
```
