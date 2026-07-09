"""token-saver CLI: init, select, index, status, search, retrieve, summarize, advise, mcp."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import index_path, init_workspace, load_config
from .indexer import connect, index_stats, index_workspace
from .install import install_claude, install_codex, install_protocol
from .retrieval import get_source_slice, retrieve_context, search
from .summarize import advise, summarize_file, summarize_folder
from .workspace import resolve_workspace


def _ws(args) -> Path:
    return resolve_workspace(getattr(args, "path", None))


def cmd_init(args) -> int:
    root = init_workspace(Path(args.path or ".").expanduser())
    print(f"Initialized Token Saver workspace at {root}/.tokensaver/")
    print("Next: token-saver index")
    return 0


def cmd_select(args) -> int:
    root = init_workspace(Path(args.path).expanduser())
    stats = index_workspace(root)
    print(f"Selected + indexed {root}")
    _print_stats(stats)
    return 0


def cmd_index(args) -> int:
    root = _ws(args)
    init_workspace(root)
    stats = index_workspace(root, force=args.force)
    print(f"Indexed {root}")
    _print_stats(stats)
    return 0


def _print_stats(stats: dict) -> None:
    print(f"  files: {stats['files']}  chunks: {stats['chunks']}  "
          f"indexed tokens: {stats['indexed_tokens']:,}")
    if "seconds" in stats:
        print(f"  scanned: {stats['files_scanned']}  (re)indexed: {stats['files_indexed']}  "
              f"removed: {stats['files_removed']}  in {stats['seconds']}s")


def cmd_status(args) -> int:
    root = _ws(args)
    print(f"Workspace: {root}")
    if not index_path(root).exists():
        print("Index: none (run token-saver init && token-saver index)")
        return 1
    con = connect(root)
    _print_stats(index_stats(con))
    con.close()
    return 0


def cmd_search(args) -> int:
    root = _ws(args)
    for h in search(root, args.query, top_k=args.top):
        loc = f"{h.path}:{h.start_line}-{h.end_line}" if not h.page else f"{h.path} p.{h.page}"
        sec = f" [{h.section}]" if h.section else ""
        print(f"{h.score:7.2f}  {loc}{sec}")
        if args.verbose:
            print("    " + h.text[:200].replace("\n", " ") + "…")
    return 0


def cmd_retrieve(args) -> int:
    root = _ws(args)
    print(retrieve_context(root, args.task, max_tokens=args.max_tokens))
    return 0


def cmd_summarize(args) -> int:
    root = _ws(args)
    target = Path(args.target)
    rel = str(target) if not target.is_absolute() else str(target.relative_to(root))
    if (root / rel).is_dir() or args.target in (".", ""):
        print(summarize_folder(root, None if args.target in (".", "") else rel,
                               focus=args.focus))
    else:
        print(summarize_file(root, rel, focus=args.focus))
    return 0


def cmd_slice(args) -> int:
    root = _ws(args)
    print(get_source_slice(root, args.file, args.start, args.end))
    return 0


def cmd_advise(args) -> int:
    print(advise(_ws(args)))
    return 0


def cmd_mcp(args) -> int:
    root = _ws(args)
    init_workspace(root)
    if not (args.claude or args.codex):
        print("Specify --claude and/or --codex", file=sys.stderr)
        return 2
    if args.claude:
        print(install_claude(root))
    if args.codex:
        print(install_codex(root, global_scope=not args.project))
    if args.protocol:
        target = "both" if (args.claude and args.codex) else ("claude" if args.claude else "codex")
        print(install_protocol(root, target))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="token-saver",
                                description="Local folder-scoped retrieval for AI agents.")
    p.add_argument("--version", action="version", version=f"token-saver {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="create .tokensaver/ in a folder")
    sp.add_argument("path", nargs="?", default=".")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("select", help="init + index a folder in one step")
    sp.add_argument("path")
    sp.set_defaults(fn=cmd_select)

    sp = sub.add_parser("index", help="build/update the index")
    sp.add_argument("path", nargs="?")
    sp.add_argument("--force", action="store_true", help="full re-index")
    sp.set_defaults(fn=cmd_index)

    sp = sub.add_parser("status", help="show workspace + index stats")
    sp.add_argument("path", nargs="?")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("search", help="hybrid BM25 search")
    sp.add_argument("query")
    sp.add_argument("path", nargs="?")
    sp.add_argument("--top", type=int, default=10)
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(fn=cmd_search)

    sp = sub.add_parser("retrieve", help="budgeted context pack for a task")
    sp.add_argument("task")
    sp.add_argument("path", nargs="?")
    sp.add_argument("--max-tokens", type=int, default=None)
    sp.set_defaults(fn=cmd_retrieve)

    sp = sub.add_parser("summarize", help="summarize a file or folder")
    sp.add_argument("target", nargs="?", default=".")
    sp.add_argument("path", nargs="?")
    sp.add_argument("--focus", default=None)
    sp.set_defaults(fn=cmd_summarize)

    sp = sub.add_parser("slice", help="print exact line range of a file")
    sp.add_argument("file")
    sp.add_argument("start", type=int, nargs="?", default=1)
    sp.add_argument("end", type=int, nargs="?", default=None)
    sp.add_argument("--path", dest="path")
    sp.set_defaults(fn=cmd_slice)

    sp = sub.add_parser("advise", help="retrieve vs cached-injection recommendation")
    sp.add_argument("path", nargs="?")
    sp.set_defaults(fn=cmd_advise)

    sp = sub.add_parser("mcp", help="install MCP server into agent configs")
    sp.add_argument("action", choices=["install"])
    sp.add_argument("path", nargs="?")
    sp.add_argument("--claude", action="store_true")
    sp.add_argument("--codex", action="store_true")
    sp.add_argument("--project", action="store_true",
                    help="Codex: write project .codex/config.toml instead of global")
    sp.add_argument("--protocol", action="store_true",
                    help="also append protocol block to CLAUDE.md/AGENTS.md")
    sp.set_defaults(fn=cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
