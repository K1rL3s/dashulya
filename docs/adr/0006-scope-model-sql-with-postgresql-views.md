# Scope model SQL through temporary PostgreSQL views

Let `chat_history` accept a list of 1-5 independent PostgreSQL SELECT queries over a transaction-local
`messages` view, whose group and original request cutoff are supplied by the
trusted session. The application creates a security-barrier view, grants SELECT
to `dashulya_history_reader`, switches to that role and always rolls back the
transaction. A batch shares one repeatable-read snapshot and uses savepoints
to preserve independent answers after a query fails. The reader has no base-table privileges or privileged memberships;
model SQL cannot widen the view's rows. This preserves [ADR 0003](0003-bind-chat-memory-access.md)
without relying on a model-generated WHERE or a mutable session setting for authority.

Parse the complete PostgreSQL syntax tree before execution: allow one SELECT,
query-local CTEs, the view, and approved built-in functions, types and operators.
Reject writes, settings, catalogs and administrative capabilities. Revoke PUBLIC
execution of `set_config` in this dedicated application database and grant it
only to the application login; otherwise a SELECT could reset its execution role.
Apply query deadlines and bounded result reads. Resolve saved media separately
through the existing scoped original reader; file references never enter SQL results.

Versioned SQL migrations provision tables, the nonadministrative `dashulya_app`
login and the NOLOGIN reader role using an administrative connection. Runtime
connections verify the migration journal and do not perform DDL. The administrator
retains ownership; the runtime login receives only required data privileges and
membership allowing it to enter the reader role.
