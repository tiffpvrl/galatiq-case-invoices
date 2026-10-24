"""Score a model on the approval cases that need judgment.

Usage:
  python scripts/eval_judgment.py --provider grok --runs 3
  python scripts/eval_judgment.py --provider grok --runs 5 --out docs/eval-grok.md
  python scripts/eval_judgment.py --provider mock            # free, deterministic

Runs the approver and critic on the labeled cases in data/test_cases/expected.json
(plus INV-1010) and reports ideal rate, unsafe approvals, over-caution, run to run
consistency and what the critic did. Costs real tokens on a live provider.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_ap.evals import evaluate, load_cases, render_markdown, summarize, to_json
from invoice_ap.llm import PROVIDERS, make_provider


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", choices=PROVIDERS, default=None,
                   help="default: whichever key is configured, else mock")
    p.add_argument("--runs", type=int, default=3, help="repetitions per case (default 3)")
    p.add_argument("--workers", type=int, default=4, help="parallel runs (default 4)")
    p.add_argument("--cases", help="comma-separated substrings to select cases, e.g. tc10,tc30")
    p.add_argument("--out", help="write the markdown report here")
    p.add_argument("--json", dest="json_out", help="write raw per-run data here")
    args = p.parse_args()

    only = set(args.cases.split(",")) if args.cases else None
    probe = make_provider(name=args.provider)
    if not load_cases(only):
        print("no matching cases", file=sys.stderr)
        return 2
    n_runs = 1 if probe.is_mock else args.runs
    print(f"provider: {probe.name} · {len(load_cases(only))} cases x {n_runs} run(s)",
          file=sys.stderr)

    reports = evaluate(lambda: make_provider(name=args.provider), runs=n_runs, only=only,
                       workers=args.workers)
    summary = summarize(reports)
    markdown = render_markdown(reports, summary, probe.name)
    print(markdown)
    if args.out:
        Path(args.out).write_text(markdown)
        print(f"wrote {args.out}", file=sys.stderr)
    if args.json_out:
        Path(args.json_out).write_text(to_json(reports, summary, probe.name))
    return 1 if summary["unsafe_approvals"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
