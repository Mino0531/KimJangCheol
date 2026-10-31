import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from backend.knowledge import SOURCE, parse_source, import_knowledge


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path, nargs='?', default=SOURCE)
    parser.add_argument('--database', type=Path)
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--source-kind', choices=['user_supplied','service_authored'], default='user_supplied')
    args = parser.parse_args()
    _, rows = parse_source(args.source.read_bytes())
    if args.validate_only:
        print(json.dumps({'valid': True, 'document_count': len(rows)}))
        return
    from backend.app import DEFAULT_DB, SQLModel, create_app
    target = args.database or DEFAULT_DB
    backup = None
    if target.exists():
        folder = target.parent / 'backups'
        folder.mkdir(exist_ok=True)
        backup = folder / f"before-rag-import-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
        with sqlite3.connect(target) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
    engine = create_app(target).state.engine
    SQLModel.metadata.create_all(engine)
    result = import_knowledge(engine, args.source, source_kind=args.source_kind)
    engine.dispose()
    print(json.dumps({**result, 'database': str(target.resolve()), 'backup': str(backup.resolve()) if backup else None}, ensure_ascii=False))


if __name__ == '__main__':
    main()
