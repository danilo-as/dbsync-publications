# Feature: sync-cli

Objective: interactive Python CLI that syncs selected PostgreSQL tables from a source DB to up to two target DBs.
Branch: `feat/sync-cli` · Delivery: ask-on-risk · Runner: `uv run pytest`

## Specs

- S1 Connection strings live in `.env`: `SOURCE_DATABASE_URL`, `TARGET_1_DATABASE_URL`, `TARGET_2_DATABASE_URL` (optional). "pueden existir hasta dos bases de datos destino". Every defined target is always synced ("siempre sincroniza todo los que esten definidos"), each in its own transaction.
- S2 First menu option: "esquemas de publicacion o otros esquemas". Publication schemas are those whose name ends with `publications` ("los esquemas de publicacion terminan con publications"). Then list schemas of that group; one schema per run ("por ahora un solo esquema").
- S3 List the tables of the chosen schema and let the user select which to sync (multi-select). Views are excluded.
- S4 Publication schemas: the user selects one or more versions from `<schema>.publication_versions` ("puedo seleccionar varias versiones"); data is filtered by `version_id`. `publication_month` exists only in some schemas' `publication_versions` and is shown when present.
- S5 Tables with `version_id`: in one transaction per target, upsert the selected `publication_versions` rows first (FK), then `DELETE WHERE version_id = ANY(...)` and `COPY` from a temporary CSV ("vamos con el DELETE antes del copy").
- S6 Tables without `version_id` in publication schemas: `TRUNCATE` + `COPY`, and the CLI must say "que esa tabla no tiene version_id y se va hacer truncate". Never use `CASCADE`: a table referenced by an incoming FK is blocked with a message; outgoing FKs get a warning naming the required parent table.
- S7 Other schemas: `COPY` into a temporary staging table, then `INSERT ... ON CONFLICT DO NOTHING` ("solo agregar lo nuevo"). Warn when the table has no PK/unique constraint.
- S8 Schema validation before any transaction: compare column name, data type and nullability per table. A mismatching table is skipped with a message and the rest continue ("se omite solo esta tabla y se deja el mensaje, pero se hacen los demas"). If `publication_versions` mismatches, every `version_id` table of that schema is skipped for that target.
- S9 Data is exported to temporary CSV files (deleted at the end, even on failure) and loaded with `COPY`.
- S10 A summary with confirmation is shown before executing, flagging truncate tables.

## Tasks

- T1 S1-S10 · inline · project scaffold, core sync engine, CLI, tests, README · commit: see Log L4

## Log

- L1 (2026-10-16) "necesito crear un script de python para syncronizar datos entre dos bases de datos, los string de conexion deben ir en un .env de la base de datos origen vs base de datos destino, pueden existir hasta dos bases de datos destino, el script debe ser un cli que me liste los esquemas que quiero sincronizar, pero quiero que la opcion primera sea esquemas de publicacion o otros esquemas, despues que me liste las tablas de ese esquema y que yo seleccione que tablas quiero sincronizar, como todo de publicaciones se basa en una publicacion, quiero decirle que datos de que publicacion quiero sincronizar para no tener que descargar toda la data, el script debe validar esquemas que sean identicos y debe descargar los datos es un csv temporal para despues hacer un copy a la bds destino"
- L2 Decisions from Q&A: engine PostgreSQL; DELETE+COPY same transaction; truncate for no-version tables with CLI notice; ON CONFLICT DO NOTHING for other schemas; sync all targets; skip mismatching table; multiple versions; one schema per run.
- L3 Evidence (local_warehouse): 17 `*publications` schemas; `phosphate` and `silicon_anode` lack `publication_versions`; 10 base tables without `version_id`, only `bess.system_pricing` has an FK (outgoing to `locations.regions`). `latest` column is named `latest_version` in some schemas.
- L4 T1 done: 19 tests pass (10 planner, 6 config, 3 real-PG integration); read-only smoke on local_warehouse OK. `.env.example` blocked by permission settings, documented in README instead.
