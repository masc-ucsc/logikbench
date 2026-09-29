#!/usr/bin/env python3
"""Summarize measured control-cone pairs, without treating absent rows as wins."""
import argparse
import datetime
import json
from pathlib import Path
import statistics
import time

REPO = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text()) if path.exists() else {}


def number(value):
    return '—' if value is None else f'{value:,.3f}'.rstrip('0').rstrip('.')


def phase_ms(record, name):
    phases = [p['ms'] for p in record.get('phases') or [] if p.get('name') == name]
    return sum(phases) if phases else None


def sta_delay(record):
    designs = (record.get('sta') or {}).get('designs', [])
    if isinstance(designs, dict): designs = list(designs.values())
    return max((d['max_delay'] for d in designs if d.get('max_delay') is not None), default=None)


def ratio(a, b):
    return f'{b / a:.3f}' if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a > 0 else '—'


def control_cost(record):
    stats = [entry.get('ctrl_stats', {}) for entry in record.get('control_stats', [])]
    return (sum(entry.get('duplicated_pred_aig', 0) for entry in stats),
            max((entry.get('largest_pred_aig', 0) for entry in stats), default=0))


def render(root):
    state = read(root / 'progress.json')
    variants = {ctrl: read(REPO / f'results/syn/asic/lhd_asap7_ctrl{ctrl}.json') for ctrl in ('false', 'true')}
    for data in variants.values():
        if data.get('meta', {}).get('refresh', {}).get('run') != root.name:
            return None  # never mix publications from different experiments
    completed = state.get('completed', {})
    minion = state.get('minion', {})
    activity = (f'Stopped {state["stopped_utc"]}: {state.get("stop_reason", "cancelled")}'
                if state.get('stopped_utc') else f'Active: `{state.get("active", "none")}`.')
    rows = [f'# Control-cone results: {root.name}', '',
            f'Updated {datetime.datetime.now(datetime.timezone.utc).isoformat()}.', '',
            f'LogikBench: {len(completed)}/500 LHD ASAP7 attempts completed; '
            f'{sum(r["exit_code"] != 0 for r in completed.values())} unsuccessful attempts. '
            + activity, '',
            'Both variants use the same frozen optimized binary and Liberty, cones, a 200 ps '
            'delay target and boundary timing. Only ctrl_cones differs. Ratios below are '
            'true/false; smaller is better except Fmax. Failed or missing pairs are excluded. '
            'The existing Yosys ASAP7 column remains an external comparison with its own provenance.', '',
            '| Metric | Complete measured pairs | Geometric mean ratio | Median ratio | Better / equal / worse |',
            '|---|---:|---:|---:|---:|']
    pairs = []
    for key, a in completed.items():
        ctrl, group, name = key.split('/')
        if ctrl != 'false': continue
        b = completed.get(f'true/{group}/{name}')
        if b and a['exit_code'] == 0 and b['exit_code'] == 0: pairs.append((group, name))
    for metric in ('cells', 'cellarea', 'logicdepth', 'fmax', 'tasktime', 'memory'):
        ratios = []
        for group, name in pairs:
            values = [variants[c].get('metrics', {}).get(metric, {}).get(group, {}).get(name) for c in ('false', 'true')]
            if all(isinstance(v, (int, float)) and v > 0 for v in values): ratios.append(values[1] / values[0])
        if ratios:
            lower = sum(x < .999 for x in ratios); higher = sum(x > 1.001 for x in ratios)
            better, worse = (higher, lower) if metric == 'fmax' else (lower, higher)
            rows.append(f'| {metric} | {len(ratios)} | {statistics.geometric_mean(ratios):.4f} | '
                        f'{statistics.median(ratios):.4f} | {better} / {len(ratios)-better-worse} / {worse} |')
        else: rows.append(f'| {metric} | 0 | — | — | — |')
    rows += ['', 'These aggregates describe available pairs, not the entire suite. Cells/area '
             'can describe a partial mapping with native state; inspect the dashboard status '
             'and each job’s timing/structural reports. Fmax is included only when both timed '
             'runs produced it. ABC per-region delay is not whole-design STA.', '',
             '## Designs with mapped control regions', '',
             'Only completed successful pairs with at least one mapped control region appear here. '
             'Duplication and largest-cone values are predicted AIG counts, not mapped cells. '
             'Ratios are true/false; ABC time excludes reader, timing analysis and other passes.', '',
             '| Design | Control regions | Duplicated AIG | Largest control AIG | Cells ratio | Area ratio | Depth ratio | Fmax ratio | ABC time ratio | Slowest control ms |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    active_pairs = 0
    for group, name in pairs:
        a, b = (completed[f'{ctrl}/{group}/{name}'] for ctrl in ('false', 'true'))
        if not b.get('control_regions'): continue
        active_pairs += 1
        duplicate, largest = control_cost(b)
        values = []
        for metric in ('cells', 'cellarea', 'logicdepth', 'fmax'):
            values.append(ratio(*(variants[c].get('metrics', {}).get(metric, {}).get(group, {}).get(name)
                                  for c in ('false', 'true'))))
        values.append(ratio(phase_ms(a, 'pass.abc'), phase_ms(b, 'pass.abc')))
        slowest = max((r.get('ms', 0) for r in b.get('slowest_control_regions', [])), default=None)
        rows.append(f'| {group}/{name} | {b["control_regions"]} | {duplicate:,} | {largest:,} | '
                    + ' | '.join(values) + f' | {number(slowest)} |')
    if not active_pairs: rows.append('| No completed control-bearing pairs yet | — | — | — | — | — | — | — | — | — |')
    rows += ['', '## Whole Minion', '',
             'Original RTL and Pyrope are reported separately. Edit rows intentionally change '
             'the adder for cache-locality measurement and are not correctness comparisons '
             'against the original. Warm1/warm2 use unchanged source and the same workdir.', '',
             '| Reader / PDK / ctrl / phase | Exit | ABC area | ABC ms | Color ms | STA delay | Control regions | ABC cache hits / misses |',
             '|---|---:|---:|---:|---:|---:|---:|---:|']
    for key, record in minion.items():
        total = record.get('abc_total') or {}
        cache = (record.get('incremental') or {}).get('abc', {})
        rows.append(f'| {key} | {record["exit_code"]} | {number(total.get("area"))} | '
                    f'{number(phase_ms(record, "pass.abc"))} | {number(phase_ms(record, "pass.color"))} | '
                    f'{number(sta_delay(record))} | {record.get("control_regions", 0)} | '
                    f'{cache.get("hits", "—")} / {cache.get("misses", "—")} |')
    if not minion: rows.append('| Pending | — | — | — | — | — | — | — |')
    for key, record in minion.items():
        if record['exit_code']:
            message = (record.get('error') or {}).get('message', 'See command log and memory guard report.')
            rows += ['', f'- `{key}` failed: {message}']
    rows += ['', '## Slowest measured control regions', '',
             '| Job | Region | Mapping ms | Mapped area |',
             '|---|---|---:|---:|']
    slowest = [(r.get('ms', 0), key, r) for records in (completed, minion)
               for key, record in records.items() for r in record.get('slowest_control_regions', [])]
    for ms, key, region in sorted(slowest, key=lambda row: row[0], reverse=True)[:10]:
        rows.append(f'| {key} | {region.get("module", region.get("name", region.get("color", "—")))} | '
                    f'{number(ms)} | {number(region.get("area"))} |')
    if not slowest: rows.append('| Pending | — | — | — |')
    rows += ['', 'STA units are reported in each retained `*-timing.json`; compare only within '
             'the same PDK. Per-job `summary.json` records duplicated nodes/predicted AIG, '
             'largest control cone, maximum region runtime and the ten slowest control regions. '
             'No keep/delete or speedup conclusion follows from unfinished, failed or zero-control cases.', '',
             f'Provenance and exact commands: `{root.relative_to(REPO)}`. '
             '[Experiment and validation notes](control-cones.md).', '']
    return '\n'.join(rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=REPO / 'docs/control-cones-results.md')
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    while True:
        report = render(root)
        if report is not None:
            tmp = args.output.with_suffix('.tmp'); tmp.write_text(report); tmp.replace(args.output)
        state = read(root / 'progress.json')
        if not args.watch or state.get('finished_utc') or state.get('stopped_utc'): break
        time.sleep(30)
