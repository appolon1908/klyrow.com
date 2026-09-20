#!/usr/bin/env python3
"""Export source-owned SQLAlchemy table structure without connecting to a database."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/database/source-structure-inventory.json"
sys.path.insert(0, str(ROOT / "apps/gateway"))


def build() -> dict:
    for name in list(os.environ):
        if name.startswith(("KLYROW_", "CODESTRA_")):
            del os.environ[name]
    os.environ.update(
        KLYROW_ENV="development",
        KLYROW_DATABASE_URL="sqlite://",
        KLYROW_SAFE_MODE="true",
        LIVE_EMAIL_DELIVERY="false",
        EXTERNAL_EMAIL_DELIVERY="false",
        PRODUCTION_PROVIDER_ROUTING="false",
    )
    from app.main import Base

    tables = []
    for table in sorted(Base.metadata.tables.values(), key=lambda item: item.name):
        tables.append({
            "table": table.name,
            "tenant_scoped": "tenant_id" in {column.name for column in table.columns},
            "columns": [
                {
                    "name": column.name,
                    "type": str(column.type),
                    "nullable": column.nullable,
                    "primary_key": column.primary_key,
                }
                for column in table.columns
            ],
            "indexes": [
                {
                    "name": index.name,
                    "unique": index.unique,
                    "columns": [column.name for column in index.columns],
                }
                for index in sorted(table.indexes, key=lambda item: item.name or "")
            ],
            "foreign_keys": [
                {
                    "column": foreign_key.parent.name,
                    "target": f"{foreign_key.target_fullname}",
                }
                for foreign_key in sorted(table.foreign_keys, key=lambda item: (item.parent.name, item.target_fullname))
            ],
            "constraints": [
                {
                    "name": constraint.name,
                    "type": type(constraint).__name__,
                    "columns": [column.name for column in getattr(constraint, "columns", ())],
                }
                for constraint in sorted(table.constraints, key=lambda item: (type(item).__name__, item.name or ""))
            ],
        })
    return {
        "authority": "apps/gateway/app/main.py:Base.metadata",
        "tables": tables,
        "table_count": len(tables),
        "tenant_scoped_count": sum(item["tenant_scoped"] for item in tables),
        "non_tenant_scoped_count": sum(not item["tenant_scoped"] for item in tables),
    }


def main() -> int:
    content = json.dumps(build(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if "--check" in sys.argv:
        current = None
        if OUTPUT.exists():
            try:
                current = json.loads(OUTPUT.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                current = None
        if current != json.loads(content):
            print(f"stale database structure inventory: {OUTPUT}", file=sys.stderr)
            return 1
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
