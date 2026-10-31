"""Validate/import a versioned chart file; DB writes are atomic and backed up."""
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from backend.chart_schema import ChartBundle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('file', type=Path)
    parser.add_argument('--database', type=Path)
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    # Validation occurs before opening or creating any DB.
    bundle = ChartBundle.model_validate_json(args.file.read_text())
    if args.validate_only:
        print(json.dumps({'valid': True, 'schema_version': bundle.schema_version, 'patient_count': len(bundle.patients)}))
        return
    from backend.app import DEFAULT_DB, SQLModel, create_app, import_chart_bundle
    target = args.database or DEFAULT_DB
    backup = None
    if target.exists():
        backups = target.parent / 'backups'
        backups.mkdir(exist_ok=True)
        backup = backups / f"before-chart-import-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
        with sqlite3.connect(target) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
    app = create_app(target)
    engine = app.state.engine
    SQLModel.metadata.create_all(engine)
    result = import_chart_bundle(engine, bundle, source_name=args.file.name)
    engine.dispose()
    print(json.dumps({**result, 'database': str(target.resolve()), 'backup': str(backup.resolve()) if backup else None}, ensure_ascii=False))


if __name__ == '__main__':
    main()
