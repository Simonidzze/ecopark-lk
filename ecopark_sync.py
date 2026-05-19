#!/usr/bin/env python3
import argparse
import json
import sys

from ecopark_sync.config import load_dotenv


def command_init_schema(_args):
    from ecopark_sync.schema import init_schema

    init_schema()
    print("Schema is ready")


def command_sync(args):
    from ecopark_sync.client import fetch_snapshot, load_snapshot_file
    from ecopark_sync.syncer import sync_snapshot

    snapshot = load_snapshot_file(args.from_file) if args.from_file else fetch_snapshot()
    result = sync_snapshot(snapshot)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def command_scheduler(_args):
    from ecopark_sync.scheduler import run_forever

    run_forever()


def command_web(_args):
    from ecopark_sync.web import run_server

    run_server()


def command_export_sheets(_args):
    from ecopark_sync.sheets import export_to_google_sheets

    result = export_to_google_sheets()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def command_import_calls(args):
    from ecopark_sync.calls import import_call_report

    with open(args.file, "rb") as handle:
        result = import_call_report(handle, source_file=args.file)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(description="Sync Ecopark 1C snapshot into MySQL")
    parser.add_argument("--env-file", default=".env", help="Path to .env file")

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-schema", help="Create MySQL database and tables")
    init_parser.set_defaults(func=command_init_schema)

    sync_parser = subparsers.add_parser("sync", help="Fetch 1C snapshot and upsert it into MySQL")
    sync_parser.add_argument("--from-file", help="Load snapshot from local JSON instead of 1C API")
    sync_parser.set_defaults(func=command_sync)

    scheduler_parser = subparsers.add_parser("scheduler", help="Run Python scheduler loop")
    scheduler_parser.set_defaults(func=command_scheduler)

    web_parser = subparsers.add_parser("web", help="Run Flask admin interface")
    web_parser.set_defaults(func=command_web)

    export_parser = subparsers.add_parser("export-sheets", help="Export current MySQL data to Google Sheets")
    export_parser.set_defaults(func=command_export_sheets)

    calls_parser = subparsers.add_parser("import-calls", help="Import call campaign CSV report")
    calls_parser.add_argument("file", help="CSV report file")
    calls_parser.set_defaults(func=command_import_calls)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    load_dotenv(args.env_file)
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
