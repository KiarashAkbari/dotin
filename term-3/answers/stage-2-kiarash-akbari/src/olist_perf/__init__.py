"""Stage-2 evidence helpers: query timing, plans, index cost and the Python/SQL bridge.

The notebook imports these functions and only narrates their results. Every
SQL statement is either a versioned .sql file or a constant here; values are
always bound parameters, identifiers are composed with psycopg.sql.Identifier.
"""

__version__ = "0.1.0"
