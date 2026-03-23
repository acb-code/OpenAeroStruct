"""Standalone CLI for OAS provenance data.

Works entirely from the local SQLite file — no running server or Claude session needed.

Usage
-----
    # List all sessions in a DB
    python -m oas_mcp.provenance.cli list --db ./oas_data/provenance.db

    # Export one session to JSON
    python -m oas_mcp.provenance.cli export <session_id> --db ./oas_data/provenance.db
    python -m oas_mcp.provenance.cli export <session_id> --db ./oas_data/provenance.db -o graph.json

    # Serve the viewer locally (open http://localhost:7654/viewer)
    python -m oas_mcp.provenance.cli serve --db ./oas_data/provenance.db --port 7654
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Default DB path — matches what the Docker container writes to
# ---------------------------------------------------------------------------

_DEFAULT_DB = Path(
    os.environ.get("OAS_PROV_DB", Path.home() / ".oas_provenance" / "sessions.db")
)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(db: Path) -> None:
    """Print a table of all sessions."""
    from .db import init_db, list_sessions

    init_db(db)
    sessions = list_sessions()
    if not sessions:
        print("No sessions found.")
        return

    # Header
    fmt = "{:<30}  {:>7}  {:>9}  {}"
    print(fmt.format("session_id", "calls", "decisions", "notes / started_at"))
    print("-" * 80)
    for s in sessions:
        notes = s.get("notes") or ""
        started = s.get("started_at", "")[:19]
        label = f"{notes}  [{started}]" if notes else started
        print(fmt.format(
            s["session_id"][:30],
            s.get("tool_call_count", 0),
            s.get("decision_count", 0),
            label,
        ))


def cmd_export(session_id: str, db: Path, output: Path | None) -> None:
    """Export a session graph to JSON."""
    from .db import init_db, get_session_graph, _dumps

    init_db(db)
    graph = get_session_graph(session_id)

    if not graph["nodes"]:
        print(f"Warning: session '{session_id}' has no nodes.", file=sys.stderr)

    text = _dumps(graph)

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"Wrote {len(graph['nodes'])} nodes, {len(graph['edges'])} edges → {output}")
    else:
        print(text)


def cmd_serve(db: Path, port: int) -> None:
    """Start a local viewer server and keep running until Ctrl-C."""
    from .db import init_db
    from .viewer_server import _ProvHandler, VIEWER_HTML
    from http.server import HTTPServer

    init_db(db)
    print(f"Provenance DB : {db.resolve()}")
    print(f"Viewer        : http://localhost:{port}/viewer")
    print(f"Sessions      : http://localhost:{port}/sessions")
    print("Press Ctrl-C to stop.\n")

    server = HTTPServer(("127.0.0.1", port), _ProvHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m oas_mcp.provenance.cli",
        description="Query and visualise OAS provenance data offline.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB,
        metavar="PATH",
        help=f"Path to provenance SQLite file (default: {_DEFAULT_DB})",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", help="List all sessions with call/decision counts")
    p_list.add_argument("--db", type=Path, default=None, metavar="PATH",
                        help="Override global --db for this subcommand")

    # export
    p_export = sub.add_parser("export", help="Export a session graph as JSON")
    p_export.add_argument("session_id", help="Session ID to export")
    p_export.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        metavar="FILE",
        help="Write JSON to FILE instead of stdout",
    )
    p_export.add_argument("--db", type=Path, default=None, metavar="PATH",
                          help="Override global --db for this subcommand")

    # serve
    p_serve = sub.add_parser(
        "serve",
        help="Start a local HTTP viewer (open http://localhost:PORT/viewer)",
    )
    p_serve.add_argument(
        "--port", type=int,
        default=int(os.environ.get("OAS_PROV_PORT", "7654")),
        help="Port to listen on (default: 7654)",
    )
    p_serve.add_argument("--db", type=Path, default=None, metavar="PATH",
                         help="Override global --db for this subcommand")

    args = parser.parse_args(argv)
    # Subcommand --db overrides the global one if provided
    effective_db = getattr(args, "db", None) or parser.get_default("db")
    # Fall back to global default if still None
    if effective_db is None:
        effective_db = _DEFAULT_DB
    db = Path(effective_db)
    db = db.resolve() if not db.is_absolute() else db

    if not db.exists() and args.command != "serve":
        print(f"Error: DB not found at {db}", file=sys.stderr)
        print("Run the server at least once, or pass --db <path>.", file=sys.stderr)
        sys.exit(1)

    if args.command == "list":
        cmd_list(db)
    elif args.command == "export":
        cmd_export(args.session_id, db, args.output)
    elif args.command == "serve":
        cmd_serve(db, args.port)


if __name__ == "__main__":
    main()
