#!/usr/bin/env python3
"""Inventory PostgreSQL access and declared database objects.

The ownership manifest is the architecture authority. This scanner turns the
current source into reviewable records and refuses relations that the manifest
does not classify. It intentionally reports dynamic SQL instead of guessing
which relation an expression will produce.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "db" / "ownership-manifest.json"

_DB_METHODS = frozenset(
    {
        "copy_from_query",
        "copy_records_to_table",
        "execute",
        "executemany",
        "fetch",
        "fetchrow",
        "fetchval",
        "prepare",
    }
)
_DB_WRAPPER_SQL_ARGUMENTS = {
    "_apply_migration_sql": 1,
    "_execute": 1,
    "_fetch": 1,
    "_fetchrow": 1,
    "_fetchval": 1,
    "_run_purge": 1,
    "delete_or_404": 1,
}
_OPERATION_RE = re.compile(
    r"\b(ALTER|COMMENT|CREATE|DELETE|DROP|GRANT|INSERT|MERGE|REINDEX|REVOKE|SELECT|TRUNCATE|UPDATE)\b",
    re.IGNORECASE,
)
_RELATION_RE = re.compile(
    r"\b(?:DELETE\s+FROM|INSERT\s+INTO|MERGE\s+INTO|TRUNCATE(?:\s+TABLE)?|UPDATE|"
    r"FROM|JOIN|USING|ALTER\s+TABLE|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|"
    r"DROP\s+TABLE(?:\s+IF\s+EXISTS)?)\s+(?:ONLY\s+)?"
    r"(?P<name>(?:[a-zA-Z_][\w$]*\.)?[a-zA-Z_][\w$]*)",
    re.IGNORECASE,
)
_WRITE_RELATION_RE = re.compile(
    r"\b(?:DELETE\s+FROM|INSERT\s+INTO|MERGE\s+INTO|TRUNCATE(?:\s+TABLE)?|UPDATE)\s+"
    r"(?:ONLY\s+)?(?P<name>(?:[a-zA-Z_][\w$]*\.)?[a-zA-Z_][\w$]*)",
    re.IGNORECASE,
)
_CTE_RE = re.compile(
    r"(?:\bWITH(?:\s+RECURSIVE)?|,)\s*([a-zA-Z_][\w$]*)"
    r"(?:\s*\([^)]*\))?\s+AS\s*\(",
    re.IGNORECASE,
)
_SCHEMA_OBJECT_PATTERNS = {
    "tables": re.compile(
        r"^CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+((?:public\.)?[a-zA-Z_][\w$]*)",
        re.IGNORECASE | re.MULTILINE,
    ),
    "sequences": re.compile(
        r"^CREATE\s+SEQUENCE(?:\s+IF\s+NOT\s+EXISTS)?\s+((?:public\.)?[a-zA-Z_][\w$]*)",
        re.IGNORECASE | re.MULTILINE,
    ),
    "functions": re.compile(
        r"^CREATE(?:\s+OR\s+REPLACE)?\s+FUNCTION\s+((?:public\.)?[a-zA-Z_][\w$]*)",
        re.IGNORECASE | re.MULTILINE,
    ),
    "types": re.compile(
        r"^CREATE\s+TYPE\s+((?:public\.)?[a-zA-Z_][\w$]*)",
        re.IGNORECASE | re.MULTILINE,
    ),
    "triggers": re.compile(
        r"^CREATE\s+TRIGGER\s+([a-zA-Z_][\w$]*)",
        re.IGNORECASE | re.MULTILINE,
    ),
    "rules": re.compile(
        r"^CREATE(?:\s+OR\s+REPLACE)?\s+RULE\s+([a-zA-Z_][\w$]*)",
        re.IGNORECASE | re.MULTILINE,
    ),
}
_SYSTEM_RELATION_PREFIXES = ("information_schema.", "pg_catalog.")
_SYSTEM_RELATIONS = frozenset(
    {
        "pg_attribute",
        "pg_class",
        "pg_constraint",
        "pg_database",
        "pg_indexes",
        "pg_namespace",
        "pg_proc",
        "pg_roles",
        "pg_settings",
        "pg_stat_activity",
        "pg_stat_database",
        "pg_stat_user_tables",
        "pg_tables",
        "pg_trigger",
        "pg_type",
    }
)
_DATABASE_HELPER_SCRIPTS = frozenset({"scripts/_db.py"})
_SHELL_DATABASE_CALL_RE = re.compile(
    r"\b(?:createdb|dropdb|pg_dump|pg_restore|psql)\b|"
    r"(?:^|\s)(?:export\s+)?DATABASE_URL=",
)


@dataclass(frozen=True)
class QueryRecord:
    """One database call discovered in production source.

    Attributes
    ----------
    path : str
        Repository-relative source path.
    line : int
        One-based line containing the database call.
    method : str
        Called database method, such as ``fetchrow`` or ``execute``.
    dynamic : bool
        Whether any part of the SQL expression is computed at runtime.
    operations : tuple[str, ...]
        SQL operation keywords present in the recoverable query text.
    relations : tuple[str, ...]
        Manifest relations found in the recoverable query text.
    write_relations : tuple[str, ...]
        Relations directly targeted by data-changing statements.
    sql_sha256 : str
        Stable digest of the recoverable query text without exposing it in reports.

    """

    path: str
    line: int
    method: str
    dynamic: bool
    operations: tuple[str, ...]
    relations: tuple[str, ...]
    write_relations: tuple[str, ...]
    sql_sha256: str


@dataclass(frozen=True)
class CrossDomainWrite:
    """A direct write whose relation is outside the caller's owning domain.

    Attributes
    ----------
    writer : str
        Service or shared package that issues the write.
    relation : str
        Unqualified database relation name.
    destination : str
        Domain that owns the relation.
    path : str
        Repository-relative source path.
    line : int
        One-based line containing the database call.

    """

    writer: str
    relation: str
    destination: str
    path: str
    line: int


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    """Load the versioned database ownership manifest.

    Parameters
    ----------
    path : Path
        Manifest path to read.

    Returns
    -------
    dict[str, Any]
        Parsed manifest data.

    """
    return json.loads(path.read_text(encoding="utf-8"))


def flatten_owned(manifest: dict[str, Any], object_kind: str) -> dict[str, str]:
    """Map object names to owning domains.

    Parameters
    ----------
    manifest : dict[str, Any]
        Parsed ownership manifest.
    object_kind : str
        Top-level manifest collection to flatten.

    Returns
    -------
    dict[str, str]
        Object name to owning domain.

    Raises
    ------
    ValueError
        If an object is assigned to more than one domain.

    """
    owners: dict[str, str] = {}
    for domain, names in manifest[object_kind].items():
        for name in names:
            if name in owners:
                raise ValueError(
                    f"{object_kind} object {name!r} is owned by both "
                    f"{owners[name]!r} and {domain!r}"
                )
            owners[name] = domain
    return owners


def _static_sql(node: ast.AST, constants: dict[str, tuple[str, bool]]) -> tuple[str, bool]:
    result = ("{dynamic}", True)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        result = (node.value, False)
    elif isinstance(node, ast.Name) and node.id in constants:
        result = constants[node.id]
    elif isinstance(node, ast.JoinedStr):
        pieces: list[str] = []
        dynamic = False
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                pieces.append(value.value)
            else:
                pieces.append("{dynamic}")
                dynamic = True
        result = ("".join(pieces), dynamic)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, left_dynamic = _static_sql(node.left, constants)
        right, right_dynamic = _static_sql(node.right, constants)
        result = (left + right, left_dynamic or right_dynamic)
    elif isinstance(node, ast.Call) and node.args:
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"format", "join"}:
            base, _ = _static_sql(node.func.value, constants)
            result = (base + "{dynamic}", True)
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {"dedent", "strip"}:
            result = _static_sql(node.args[0], constants)
    return result


def _normalize_relation(name: str) -> str | None:
    normalized = name.lower()
    if normalized in {"lateral", "of", "set"}:
        return None
    if normalized.startswith(_SYSTEM_RELATION_PREFIXES) or normalized in _SYSTEM_RELATIONS:
        return None
    if normalized.startswith("public."):
        normalized = normalized.removeprefix("public.")
    elif "." in normalized:
        # The current schema is public-only. Other dotted matches are aliases
        # such as ``p.published_date`` or ``excluded.column``.
        return None
    return normalized


def _query_shape(
    sql: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    operations = tuple(sorted({match.group(1).upper() for match in _OPERATION_RE.finditer(sql)}))
    ctes = {match.group(1).lower() for match in _CTE_RE.finditer(sql)}
    relations: set[str] = set()
    for match in _RELATION_RE.finditer(sql):
        if sql[match.end() :].lstrip().startswith("("):
            # A set-returning function in FROM is not a relation.
            continue
        relation = _normalize_relation(match.group("name"))
        if relation is not None and relation not in ctes:
            relations.add(relation)
    write_relations = {
        relation
        for match in _WRITE_RELATION_RE.finditer(sql)
        if (relation := _normalize_relation(match.group("name"))) is not None
        and relation not in ctes
    }
    return operations, tuple(sorted(relations)), tuple(sorted(write_relations))


class _PythonQueryVisitor(ast.NodeVisitor):
    def __init__(
        self,
        path: Path,
        root: Path,
        constants: dict[str, tuple[str, bool]],
    ) -> None:
        self.path = path
        self.root = root
        self.constants = constants
        self.records: list[QueryRecord] = []

    def _record_assigned_sql(self, value: ast.AST) -> None:
        pending = [value]
        while pending:
            node = pending.pop()
            if isinstance(node, ast.JoinedStr):
                # The complete f-string is inventoried at its database call.
                # Scanning its isolated literal fragments loses CTE context.
                continue
            pending.extend(ast.iter_child_nodes(node))
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            operations, relations, write_relations = _query_shape(node.value)
            if not operations or not relations:
                continue
            self.records.append(
                QueryRecord(
                    path=str(self.path.relative_to(self.root)),
                    line=node.lineno,
                    method="literal",
                    dynamic=False,
                    operations=operations,
                    relations=relations,
                    write_relations=write_relations,
                    sql_sha256=hashlib.sha256(node.value.encode()).hexdigest(),
                )
            )

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802 - ast visitor API
        self._record_assigned_sql(node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802 - ast visitor API
        if node.value is not None:
            self._record_assigned_sql(node.value)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802 - ast visitor API
        if node.name not in _DB_WRAPPER_SQL_ARGUMENTS:
            self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802 - ast visitor API
        if node.name not in _DB_WRAPPER_SQL_ARGUMENTS:
            self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast visitor API
        method = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        sql_argument = 0
        if isinstance(node.func, ast.Name) and node.func.id in _DB_WRAPPER_SQL_ARGUMENTS:
            method = node.func.id
            sql_argument = _DB_WRAPPER_SQL_ARGUMENTS[method]
        if (method in _DB_METHODS or method in _DB_WRAPPER_SQL_ARGUMENTS) and len(
            node.args
        ) > sql_argument:
            sql, dynamic = _static_sql(node.args[sql_argument], self.constants)
            operations, relations, write_relations = _query_shape(sql)
            if operations or dynamic:
                self.records.append(
                    QueryRecord(
                        path=str(self.path.relative_to(self.root)),
                        line=node.lineno,
                        method=method,
                        dynamic=dynamic,
                        operations=operations,
                        relations=relations,
                        write_relations=write_relations,
                        sql_sha256=hashlib.sha256(sql.encode()).hexdigest(),
                    )
                )
        self.generic_visit(node)


def _module_constants(tree: ast.Module) -> dict[str, tuple[str, bool]]:
    constants: dict[str, tuple[str, bool]] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name):
                constants[target.id] = _static_sql(statement.value, constants)
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            if statement.value is not None:
                constants[statement.target.id] = _static_sql(statement.value, constants)
    return constants


def production_python_files(root: Path = REPO_ROOT) -> list[Path]:
    """Find Python modules that can execute in shipped services or operator scripts.

    Parameters
    ----------
    root : Path
        Repository root to inspect.

    Returns
    -------
    list[Path]
        Sorted production Python paths, excluding test support modules.

    """
    roots = [
        root / "services" / "platform_api" / "platform_api",
        root / "services" / "paper_ingestion" / "paper_ingestion",
        root / "services" / "learning_engine" / "learning_engine",
        root / "services" / "telegram_bot" / "telegram_bot",
        root / "libs" / "jarvis_common" / "jarvis_common",
        root / "scripts",
    ]
    files: list[Path] = []
    for source_root in roots:
        for path in source_root.rglob("*.py"):
            if (
                "tests" in path.parts
                or "testing_sidecars" in path.parts
                or path.name.startswith("testing")
            ):
                continue
            files.append(path)
    return sorted(files)


def inventory_queries(root: Path = REPO_ROOT) -> list[QueryRecord]:
    """Inventory direct database calls in production Python.

    Parameters
    ----------
    root : Path
        Repository root to inspect.

    Returns
    -------
    list[QueryRecord]
        Database calls in deterministic path and source order.

    """
    records: list[QueryRecord] = []
    for path in production_python_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _PythonQueryVisitor(path, root, _module_constants(tree))
        visitor.visit(tree)
        records.extend(visitor.records)
    direct_digests = {
        (record.path, record.sql_sha256) for record in records if record.method != "literal"
    }
    unique_records = {
        record
        for record in records
        if record.method != "literal" or (record.path, record.sql_sha256) not in direct_digests
    }
    return sorted(
        unique_records,
        key=lambda record: (record.path, record.line, record.method, record.sql_sha256),
    )


def inventory_schema_objects(root: Path = REPO_ROOT) -> dict[str, list[str]]:
    """Inventory objects created by the baseline and retained migrations.

    Parameters
    ----------
    root : Path
        Repository root containing ``db/init.sql`` and ``db/migrations``.

    Returns
    -------
    dict[str, list[str]]
        Object kind to sorted unqualified object names.

    """
    sql_paths = [root / "db" / "init.sql", *sorted((root / "db" / "migrations").glob("*.sql"))]
    sql = "\n".join(path.read_text(encoding="utf-8") for path in sql_paths)
    objects: dict[str, list[str]] = {}
    for kind, pattern in _SCHEMA_OBJECT_PATTERNS.items():
        names = {match.removeprefix("public.").lower() for match in pattern.findall(sql)}
        objects[kind] = sorted(names)
    return objects


def database_caller_scripts(root: Path = REPO_ROOT) -> list[str]:
    """Inventory operator scripts that connect to or configure PostgreSQL.

    Python callers are identified by an ``asyncpg`` import. Shell callers must
    contain an executable PostgreSQL client command or assign ``DATABASE_URL``
    outside a comment. The shared Python DSN helper is included explicitly.

    Parameters
    ----------
    root : Path
        Repository root containing the ``scripts`` directory.

    Returns
    -------
    list[str]
        Sorted repository-relative caller paths.

    """
    callers = {path for path in _DATABASE_HELPER_SCRIPTS if (root / path).is_file()}
    scripts_root = root / "scripts"
    for path in scripts_root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports_asyncpg = any(
            (isinstance(node, ast.Import) and any(alias.name == "asyncpg" for alias in node.names))
            or (isinstance(node, ast.ImportFrom) and node.module == "asyncpg")
            for node in ast.walk(tree)
        )
        if imports_asyncpg:
            callers.add(str(path.relative_to(root)))

    for path in scripts_root.rglob("*.sh"):
        if "tests" in path.parts:
            continue
        executable_lines = (
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        if any(_SHELL_DATABASE_CALL_RE.search(line) for line in executable_lines):
            callers.add(str(path.relative_to(root)))
    return sorted(callers)


def _writer_for_path(path: str) -> tuple[str, str | None]:
    owners = (
        ("services/platform_api/", "platform_api", "platform"),
        ("services/paper_ingestion/", "paper_ingestion", "research"),
        ("services/learning_engine/", "learning_engine", "learning"),
        ("services/telegram_bot/", "telegram_bot", None),
        ("libs/jarvis_common/", "jarvis_common", None),
        ("scripts/", "operator_scripts", None),
    )
    for prefix, writer, domain in owners:
        if path.startswith(prefix):
            return writer, domain
    return "unknown", None


def cross_domain_writes(
    manifest: dict[str, Any], records: list[QueryRecord]
) -> list[CrossDomainWrite]:
    """Find direct writes that cross the caller's declared domain boundary.

    Parameters
    ----------
    manifest : dict[str, Any]
        Parsed ownership manifest.
    records : list[QueryRecord]
        Inventoried production database calls.

    Returns
    -------
    list[CrossDomainWrite]
        Cross-domain writes in source order. Operator scripts are excluded
        because their exceptional callers are declared separately.

    """
    owners = flatten_owned(manifest, "tables")
    writes: list[CrossDomainWrite] = []
    for record in records:
        if not record.write_relations:
            continue
        writer, native_domain = _writer_for_path(record.path)
        if writer == "operator_scripts":
            continue
        for relation in record.write_relations:
            destination = owners.get(relation)
            if destination is None or destination == native_domain:
                continue
            writes.append(
                CrossDomainWrite(
                    writer=writer,
                    relation=relation,
                    destination=destination,
                    path=record.path,
                    line=record.line,
                )
            )
    return writes


def validate_inventory(
    manifest: dict[str, Any],
    queries: list[QueryRecord],
    schema_objects: dict[str, list[str]],
    root: Path = REPO_ROOT,
) -> list[str]:
    """Validate ownership declarations against schema and source inventories.

    Parameters
    ----------
    manifest : dict[str, Any]
        Parsed ownership manifest.
    queries : list[QueryRecord]
        Inventoried production database calls.
    schema_objects : dict[str, list[str]]
        Objects created by the baseline and retained migrations.
    root : Path
        Repository root used to discover supported database callers.

    Returns
    -------
    list[str]
        Deterministic validation errors. An empty list means the inventory is complete.

    """
    errors: list[str] = []
    table_owners = flatten_owned(manifest, "tables")

    for kind, actual_names in schema_objects.items():
        declared_names = set(flatten_owned(manifest, kind))
        actual = set(actual_names)
        missing = sorted(actual - declared_names)
        stale = sorted(declared_names - actual)
        if missing:
            errors.append(f"unowned {kind}: {', '.join(missing)}")
        if stale:
            errors.append(f"declared {kind} absent from schema: {', '.join(stale)}")

    reviewed_query_aliases = set(manifest.get("reviewed_query_aliases", []))
    unknown_relations = sorted(
        {
            relation
            for record in queries
            for relation in record.relations
            if relation not in table_owners and relation not in reviewed_query_aliases
        }
    )
    if unknown_relations:
        errors.append(f"relations absent from ownership manifest: {', '.join(unknown_relations)}")

    actual_dynamic_counts = Counter(record.path for record in queries if record.dynamic)
    reviewed_dynamic_counts = manifest.get("reviewed_dynamic_sql", {})
    if dict(sorted(actual_dynamic_counts.items())) != reviewed_dynamic_counts:
        errors.append("dynamic SQL inventory differs from reviewed per-file counts")

    declared_seams = {
        (seam["current_writer"], relation, seam["destination"])
        for seam in manifest["transition_seams"]
        for relation in seam["relations"]
    }
    unreviewed_writes = sorted(
        {
            (write.writer, write.relation, write.destination)
            for write in cross_domain_writes(manifest, queries)
            if (write.writer, write.relation, write.destination) not in declared_seams
        }
    )
    if unreviewed_writes:
        rendered_writes = ", ".join(
            f"{writer}:{relation}->{domain}" for writer, relation, domain in unreviewed_writes
        )
        errors.append("unreviewed cross-domain writes: " + rendered_writes)

    declared_callers = set(manifest["supported_database_callers"])
    actual_callers = set(database_caller_scripts(root))
    if undeclared_callers := sorted(actual_callers - declared_callers):
        errors.append("unreviewed database callers: " + ", ".join(undeclared_callers))
    if stale_callers := sorted(declared_callers - actual_callers):
        errors.append("declared database callers no longer detected: " + ", ".join(stale_callers))
    return errors


def _summary(
    manifest: dict[str, Any], records: list[QueryRecord], schema_objects: dict[str, list[str]]
) -> dict[str, Any]:
    writes = cross_domain_writes(manifest, records)
    return {
        "manifest": str(DEFAULT_MANIFEST.relative_to(REPO_ROOT)),
        "owned_table_count": len(flatten_owned(manifest, "tables")),
        "schema_objects": schema_objects,
        "query_count": len(records),
        "dynamic_query_count": sum(record.dynamic for record in records),
        "supported_database_callers": database_caller_scripts(),
        "cross_domain_writes": [asdict(write) for write in writes],
        "queries": [asdict(record) for record in records],
    }


def main(argv: list[str] | None = None) -> int:
    """Run the database inventory command.

    Parameters
    ----------
    argv : list[str] | None
        Optional command-line arguments. ``None`` uses ``sys.argv``.

    Returns
    -------
    int
        Process exit status.

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--check", action="store_true", help="fail on incomplete ownership data")
    parser.add_argument("--output", type=Path, help="write JSON to this path instead of stdout")
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    queries = inventory_queries()
    schema_objects = inventory_schema_objects()
    payload = _summary(manifest, queries, schema_objects)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)

    if args.check:
        errors = validate_inventory(manifest, queries, schema_objects)
        if errors:
            for error in errors:
                print(f"database inventory: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
