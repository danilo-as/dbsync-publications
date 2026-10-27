# Feature: flyway-check

Objective: at startup, compare Flyway history with the bmi-data-warehouse-sql migrations and offer to run missing ones on local targets.
Branch: `main` (user commits on request) · Delivery: single commit · Runner: `uv run pytest` (integration: `DBSYNC_TEST_ADMIN_URL`)

## Specs

- S1 "al iniciar el script, valides con el esquema flyway y el repositorio bmi-data-warehouse-sql las migraciones que faltan correr". History table: `flyway.flyway_schema_history`. Repo migrations dir comes from `.env` (`MIGRATIONS_DIR`); when unset the check is skipped with a notice.
- S2 Pending migrations are reported ("avisa") and the user is asked whether to run them ("pregunta si quiere ejecutarlas").
- S3 "este script solo puede ejecutar las migraciones si alguno de los destinos el host es localhost". Source and remote targets are only reported, never migrated.
- S4 "todos localmente deben usar un usuario administrador": local migrations run as the connected admin user, without `SET ROLE schema_owner`.
- S5 "hacer un copi de los registros igualitos a local una vez ejecutados de forma manual": the source history is the reference. A migration is runnable on a local target when it is applied successfully in the source and missing on the target; after running its SQL, the source history row is copied with the same version, description, type, script and checksum. Assumption: installed_rank, installed_by, installed_on and execution_time reflect the local run.
- S6 Reading the source history may fail ("ya estoy pidiendo permisos de lectura sobre flyway"): the CLI reports it and continues; without the source history nothing is runnable, repo-vs-target is shown for information.
- S7 Assumption: a local target without a history table is offered a bootstrap that copies the whole source history as-is, with a warning that it is only correct when the database already matches the source.
- S8 Assumption: migrations containing `CONCURRENTLY` run outside a transaction, one statement at a time (as Flyway does); others run in one transaction together with their history row.

## Tasks

- T1 S1-S8 migrations module, CLI step, config, README, tests · inline · done (RED: tests/test_migrations.py import error before module; 43 passed with real PG)

## Log

- L1 2026-10-16 "una ultima cosa, quisiera que al iniciar el script, valides con el esquema flyway y el repositorio bmi-data-warehouse-sql las migraciones que faltan correr, es posible?"
- L2 "avisa y pregunta si quiere ejecutarlas" · "es que este script solo puede ejecutar las migraciones si alguno de los destinos el host es localhost" · "pruebalo localmente, todos localmente deben usar un usuario administrador"
- L3 "necesitamos permisos de lectura sobre flyway? y seria hacer un copi de los registros igualitos a local una vez ejecutados de forma manual, que te parece?" · "ya estoy pidiendo permisos de lectura sobre flyway"
- L4 Findings: local_warehouse (postgres superuser) has no flyway history and no schema_owner role; source user lacks read on schema flyway; Flyway CLI not installed, so SQL runs through psycopg.
- L5 Verified: unit + integration tests (throwaway postgres:17 on :55432) and a CLI smoke run of check_migrations (bootstrap, local run, remote report only).
- L6 Independent verifier: S1-S8 met; blocker fixed (empty source history crashed max()); bootstrap errors now reported and skipped; regression test added; 44 passed with real PG. Open advisories: no CLI tests beyond empty history, hostaddr/PGHOST can fool is_local, uneven baselines unreported, ELSE'...' misread as E-string.
