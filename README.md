# Claude Code Usage + Plan Optimizer

A tiny, zero-dependency tool that reads your local [Claude Code](https://claude.com/claude-code) transcripts and answers two questions:

1. **Where did your hours go?** Per-project breakdown with daily / weekly / monthly views, an estimate of time spent, an estimate of labor saved, and an API-equivalent cost.
2. **Are you on the right Claude plan?** Your last 30 days of API-equivalent consumption against the Pro / Max 5x / Max 20x / raw-API stickers, with a keep / upgrade / downgrade verdict — including how many days you actually hit a usage limit.

It produces a self-contained HTML dashboard plus a quick terminal summary, and a downloadable **share card** (a privacy-safe PNG with your totals — no project names).

```
  Claude Code -- week of 2026-06-15 -> 2026-06-21
  ------------------------------------------------------------------------------
  Project                  Active h  Saved A  Saved B  Sessions     Est $
  ------------------------------------------------------------------------------
  my-app                        3.4     13.7    277.2         5    102.87
  notes-vault                   2.2      9.0    432.3         2    152.95
  side-project                  2.2      8.6    151.1         2     80.19
  ------------------------------------------------------------------------------
  TOTAL                         8.5     33.9    912.1        26    358.30
```

The dashboard adds a project breakdown, KPI cards, and a daily/weekly/monthly time chart.

## Privacy

**Everything runs locally.** The tool only *reads* files under `~/.claude/projects` and writes one HTML file next to itself. Nothing is sent anywhere — no network calls, no telemetry. The generated `usage_dashboard.html` contains your real project names and usage, so it's git-ignored by default; don't commit it.

## Requirements

- Python 3.9+
- Claude Code installed, with transcripts under `~/.claude/projects` (the default)

No pip install, no dependencies — it's standard library only.

## Usage

```bash
python claude_usage.py            # build the dashboard + print this week's summary
python claude_usage.py --open     # also open the dashboard in your browser
python claude_usage.py --plan max_5x   # tell the plan check what you're on
python claude_usage.py --out report.html
python claude_usage.py --base /custom/path/to/.claude/projects
python claude_usage.py --config my-config.json
```

Re-run it any time to refresh — it reads your live transcripts each time, no database to maintain.

## How the numbers are estimated

These are estimates. The tool is transparent about how each is derived so you can judge — and tune — them.

- **Active time** — within each session, the time between consecutive events is summed, but any gap longer than the idle cap (default **5 min**) is treated as "stepped away" and dropped. This measures *engaged* time, not wall-clock.

- **Saved A — time multiplier:** `active hours × 4`. The intuition: one focused hour driving Claude ≈ four hours done by hand. Tune the multiplier in config.

- **Saved B — output volume:** `output tokens ÷ 1,500/hr`, i.e. tokens of finished work a person produces per hour. **Heads-up:** Claude emits far more output than the final deliverable (explanations, tool-call JSON, retries), so Method B reads high — often 10×+ Method A. Treat it as a *relative* signal across projects, or raise `human_throughput_tokens_per_hour` (try 8,000–15,000) to bring it near Method A. Method A is the more defensible headline.

- **Est $** — the API list-price equivalent of the tokens consumed (cache reads billed at 0.1×, cache writes at 1.25×). **This is not your subscription cost** — if you're on a Pro/Max plan you didn't pay this. It's a proxy for the raw compute value you consumed. Prices are current Anthropic list rates as of mid-2026; edit the `pricing` block in config if they drift.

## The plan check

Tell the tool what you're on (`--plan pro|max_5x|max_20x|api`, or `"plan"` in config) and it compares your last 30 days of API-equivalent consumption to the subscription stickers:

- **The value multiple** — API-equivalent dollars ÷ plan price. "$412 of compute on a $100 plan — 4.1× the sticker."
- **Limit-hit days** — Claude Code records a local notice when you hit a usage limit; the tool counts the days it happened. This is the ground truth for upgrades: dollars say what the plan is *worth*, limit hits say whether it's *enough*.
- **The verdict** — keep, upgrade, or downgrade, with the reasoning printed next to it. The dollar bands behind "best fit" are heuristics (quotas aren't published as dollar figures) — 3+ limit-hit days bumps the fit one tier up. Plan prices are editable in `plan_prices` in config if they drift.

The dashboard also renders a **share card**: a 1200×630 PNG with your 30-day totals (compute value, active hours, labor saved, sessions) and no project names. Download it, post it.

## Projects: auto-detection + optional config

By default the tool derives a project name from each session's working directory — it takes the folder right after a recognized container directory (`dev`, `projects`, `repos`, `src`, `code`, `work`, …). So `~/dev/my-app/src` becomes **my-app** automatically, on macOS, Linux, or Windows.

Want custom grouping (merge subfolders, rename, label clients)? Copy [`claude_usage.config.example.json`](claude_usage.config.example.json) to `claude_usage.config.json` (next to the script, or at `~/.claude/claude_usage.config.json`) and add rules:

```json
{
  "project_rules": [
    {"match": "client-acme", "label": "ACME (client)"},
    {"match": "/dev/my-app", "label": "My App"}
  ],
  "time_multiplier": 5.0,
  "human_throughput_tokens_per_hour": 10000
}
```

The first rule whose `match` is a substring of the full path wins, before auto-detection. Any key you omit falls back to the default.

## License

MIT — see [LICENSE](LICENSE).

---

Not affiliated with Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic.
