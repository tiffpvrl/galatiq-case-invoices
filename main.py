#!/usr/bin/env python3
"""Acme Corp invoice processing.

  python main.py --invoice_path=data/invoices/invoice_1001.txt
  python main.py --all --fresh          # every sample invoice, from a clean ledger
  python main.py --review               # resolve invoices the system escalated
"""

import argparse
import json
import logging
import sys
import uuid
from dataclasses import replace
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm, Prompt

from invoice_ap import review
from invoice_ap.config import DB_PATH, INVOICE_DIR, RUN_DIR, SETTINGS
from invoice_ap.db import init_db
from invoice_ap.llm import PROVIDERS, make_provider
from invoice_ap.pipeline import process_file
from invoice_ap.report import money, print_result, print_summary, summarize

SUPPORTED = {".txt", ".pdf", ".json", ".csv", ".xml"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Multi-agent invoice processing: ingest, validate, approve, pay.")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--invoice_path", metavar="PATH",
                     help="an invoice file, or a directory of invoices")
    src.add_argument("--all", action="store_true", help=f"process every file in {INVOICE_DIR}")
    src.add_argument("--review", action="store_true",
                     help="interactively resolve invoices awaiting human review")
    p.add_argument("--fresh", action="store_true",
                   help="reset the database (inventory seed, empty ledger) before running")
    p.add_argument("--provider", choices=PROVIDERS,
                   help="LLM provider (default: grok if XAI_API_KEY is set, else mock)")
    p.add_argument("--offline", action="store_true", help="force the offline mock provider")
    p.add_argument("--reserve-stock", action="store_true",
                   help="exclude stock already claimed by paid invoices in the ledger")
    p.add_argument("--as-of", metavar="YYYY-MM-DD",
                   help="date used for past-due checks (default 2026-02-01, or AP_AS_OF_DATE)")
    p.add_argument("--json", action="store_true", help="print machine-readable JSON, no styling")
    p.add_argument("--brief", action="store_true", help="one line per invoice")
    p.add_argument("--db", default=str(DB_PATH), help="SQLite database path")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print the structured trace events to stderr")
    return p


def collect_files(args) -> list[Path]:
    if args.all:
        root = INVOICE_DIR
    else:
        root = Path(args.invoice_path)
    if root.is_dir():
        files = sorted(f for f in root.iterdir() if f.suffix.lower() in SUPPORTED)
    elif root.is_file():
        files = [root]
    else:
        raise SystemExit(f"error: {root} not found")
    if not files:
        raise SystemExit(f"error: no invoices found in {root}")
    return files


def run_review(conn, console: Console) -> int:
    queue = review.pending(conn)
    if not queue:
        console.print("[green]Nothing awaiting review.[/]")
        return 0
    console.print(f"[bold]{len(queue)} invoice(s) awaiting review[/]\n")
    reviewer = Prompt.ask("Your name", default="reviewer")
    for item in queue:
        console.rule(f"#{item.ledger_id}  {escape(str(item.invoice_number or item.source_file))}")
        console.print(f"Vendor: {escape(str(item.vendor or 'unknown'))}   "
                      f"Total: {money(item.total)}   File: {escape(str(item.source_file))}")
        for r in item.reasons:
            console.print(f"  {r['severity']:<8} {escape(r['code'])}: {escape(r['message'])}")
        choice = Prompt.ask("Decision", choices=["approve", "reject", "hold", "quit"],
                            default="hold")
        if choice == "quit":
            break
        if choice == "approve" and not Confirm.ask("Pay this invoice now?", default=False):
            continue
        note = Prompt.ask("Note (optional)", default="")
        try:
            payment = review.resolve(conn, item.ledger_id, choice, reviewer, note)
        except review.ReviewError as exc:
            console.print(f"[red]{escape(str(exc))}[/]")
            continue
        suffix = f" Paid {payment.reference}." if payment else ""
        console.print(f"[bold]Recorded: {choice}.[/]{suffix}")
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not (args.invoice_path or args.all or args.review):
        build_parser().print_usage()
        print("error: give --invoice_path PATH, --all, or --review", file=sys.stderr)
        return 2

    console = Console(quiet=args.json, highlight=False)
    if args.verbose:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")

    try:
        provider = make_provider(offline=args.offline, name=args.provider)
    except Exception as exc:  # e.g. a configured key but a bad setup
        print(f"error: could not start the LLM provider: {exc}", file=sys.stderr)
        return 2
    conn = init_db(args.db, reset=args.fresh)

    if args.review:
        try:
            return run_review(conn, console)
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Review stopped. Undecided invoices stay in the queue.[/]")
            return 130

    settings = SETTINGS
    if args.reserve_stock:
        settings = replace(settings, reserve_stock=True)
    if args.as_of:
        import datetime as dt

        settings = replace(settings, as_of_date=dt.date.fromisoformat(args.as_of))

    files = collect_files(args)
    run_id = uuid.uuid4().hex[:8]
    out_dir = RUN_DIR / run_id
    mode = "offline mock (no API key: rule-based extraction, canned reasoning)" \
        if provider.is_mock else f"{provider.name} (live)"
    console.print(f"[dim]run {run_id} · LLM: {mode} · as-of {settings.as_of_date} · "
                  f"{len(files)} invoice(s)[/]\n")

    results = []
    for path in files:
        result = process_file(path, conn, provider, settings, run_id=run_id, out_dir=out_dir)
        results.append(result)
        print_result(console, result, detail=not args.brief)

    summary = summarize(results, provider.name)
    if args.json:
        print(json.dumps({"run_id": run_id, "summary": summary,
                          "invoices": [r.to_dict() for r in results]}, indent=2, default=str))
        return 0
    if len(results) > 1:
        console.print()
        print_summary(console, summary)
    problems = sorted({w for r in results for w in r.warnings})
    if problems:
        console.print(f"\n[yellow]⚠ {len(problems)} operational problem(s) during this run "
                      "(affected invoices were handled safely):[/]")
        for w in problems:
            console.print(f"  [yellow]•[/] {escape(w)}")
    console.print(f"\n[dim]trace, notifications: {out_dir}[/]")
    held = summary["needs_review"]
    if held:
        console.print(f"[yellow]{held} invoice(s) await review: python main.py --review[/]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
