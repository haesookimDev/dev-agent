"""Versioned DDL shared by migrations and explicit development bootstrap.

Do not change V1 in place: deployed migration history must remain reproducible.
"""

from sqlalchemy import DDL, Table, event

AUDIT_GUARDS_V1 = {
    "sqlite": (
        "CREATE TRIGGER audit_records_no_update BEFORE UPDATE ON audit_records "
        "BEGIN SELECT RAISE(ABORT, 'audit records are append-only'); END",
        "CREATE TRIGGER audit_records_no_delete BEFORE DELETE ON audit_records "
        "BEGIN SELECT RAISE(ABORT, 'audit records are append-only'); END",
        # REPLACE can delete a conflicting row without firing DELETE triggers.
        "CREATE TRIGGER audit_records_no_replace BEFORE INSERT ON audit_records "
        "WHEN EXISTS (SELECT 1 FROM audit_records WHERE id = NEW.id) "
        "BEGIN SELECT RAISE(ABORT, 'audit records are append-only'); END",
    ),
    "postgresql": (
        "CREATE FUNCTION kelpie_audit_immutable_v1() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'audit records are append-only' USING ERRCODE = '23000'; END; $$",
        "CREATE TRIGGER audit_records_immutable BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON audit_records FOR EACH STATEMENT EXECUTE FUNCTION kelpie_audit_immutable_v1()",
    ),
}


def register_audit_guards(table: Table) -> None:
    for dialect, statements in AUDIT_GUARDS_V1.items():
        for statement in statements:
            event.listen(table, "after_create", DDL(statement).execute_if(dialect=dialect))
    event.listen(table, "after_drop", DDL(
        "DROP FUNCTION kelpie_audit_immutable_v1()",
    ).execute_if(dialect="postgresql"))
