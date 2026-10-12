"""Extend immutable image provenance while preserving existing decisions."""
from .formal_recovery_migration import authoritative_migration_fragment


def decision_origin_extension():
    old = authoritative_migration_fragment()
    table = next(sql for sql in old if "CREATE TABLE version_image_decisions_v2(" in sql)
    table = table.replace("version_image_decisions_v2(", "version_image_decisions_v2_extended(", 1)
    table = table.replace("('brief','asset_package')", "('brief','asset_package','generated')")
    return (table,
        "INSERT INTO version_image_decisions_v2_extended SELECT * FROM version_image_decisions_v2",
        "DROP TABLE version_image_decisions_v2",
        "ALTER TABLE version_image_decisions_v2_extended RENAME TO version_image_decisions_v2",
        *(sql for sql in old if ("CREATE INDEX version_image_decisions_v2_" in sql
            or ("CREATE TRIGGER" in sql and " ON version_image_decisions_v2" in sql))))
