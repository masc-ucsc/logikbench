#!/usr/bin/env python3
"""Serial, resumable control-cone A/B. Requires a prepared, frozen run directory.

The only LHD treatment difference is color.synth.ctrl_cones. Each completed result
is published into its own ASAP7 column; old values are never imported.
"""
import argparse
import datetime
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

from logikbench.apps.lb import ALL_GROUPS, make_worklist, save_target
from logikbench.common import clean_build

REPO = Path(__file__).resolve().parents[1]
PYTHON = str(REPO / '.venv/bin/python')
BASE_OPTIONS = ('--stats --set color.synth.mode=cones --set synth.reduce=false '
                '--set abc.ware=true --set abc.boundary=true --set abc.boundary_rounds=3 '
                '--set abc.ctrl_flow=inherit')


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    tmp.replace(path)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def options(ctrl, delay=200):
    return BASE_OPTIONS + f' --set abc.delay={delay} --set color.synth.ctrl_cones={ctrl}'


def read_control_stats(log):
    control_stats = []
    if log.exists():
        for line in log.open(errors='replace'):
            if '[color.ctrl]' in line and '"ctrl_cones":' in line:
                payload = line[line.index('"ctrl_cones":'):].strip()
                try:
                    control_stats.append(json.loads('{' + payload + '}'))
                except ValueError:
                    pass
    return control_stats


def summarize(result, log):
    data = json.loads(result.read_text()) if result.exists() else {}
    qor = (data.get('qor') or {}).get('abc') or {}
    regions = qor.get('regions', [])
    controls = [r for r in regions if r.get('ctrl')]
    return {'status': data.get('status'), 'error': data.get('error'),
            'phases': data.get('phases'), 'incremental': data.get('incremental'),
            'abc_total': qor.get('total'), 'control_regions': len(controls),
            'max_region_ms': max((r.get('ms', 0) for r in regions), default=0),
            'slowest_control_regions': sorted(controls, key=lambda r: r.get('ms', 0), reverse=True)[:10],
            'control_stats': read_control_stats(log), 'sta': (data.get('qor') or {}).get('sta')}


class Experiment:
    def __init__(self, root):
        self.root = root.resolve()
        self.prov = json.loads((self.root / 'provenance.json').read_text())
        self.binary = self.root / 'lhd-tool/lhd'
        assert digest(self.binary) == self.prov['binary_sha256']
        self.env = os.environ.copy()
        self.env['LHD'] = str(self.binary)
        self.env['PATH'] = (str(REPO / 'build/tools/yosys-head/bin') + ':' +
                            str(REPO / '.venv/bin') + ':/usr/local/bin:/opt/homebrew/bin:' + self.env['PATH'])
        self.path = self.root / 'progress.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'expected_logikbench': 500, 'completed': {}, 'minion': {}}
        if self.state.get('stopped_utc'):
            raise RuntimeError('This experiment was cancelled; preserve it and prepare a new run instead')
        self.worklist = make_worklist(SimpleNamespace(group=ALL_GROUPS, name=None))
        assert len(self.worklist) == 250

    def guarded(self, command, directory, timeout=14400):
        if shutil.disk_usage(REPO).free < 12 * 2**30:
            raise RuntimeError('Below 12 GiB disk reserve; stopping before launching another synthesis')
        # Recover a child that completed while its controller was stopped.
        # Its SiliconCompiler tasktime remains authoritative; controller elapsed
        # time is unknown after recovery and is deliberately left null.
        prior = directory / 'memory.json'
        if prior.exists():
            memory = json.loads(prior.read_text())
            if memory.get('command') == [str(v) for v in command] and 'exit_code' not in memory:
                # A controller restart may leave its already guarded child alive.
                # Join that exact guard by its report path, never launch a duplicate.
                import psutil
                print('WAIT for existing guarded child', self.state['active'], flush=True)
                while 'exit_code' not in memory and 'reason' not in memory:
                    running = any('logikbench.memory_guard' in (p.info['cmdline'] or [])
                                  and str(prior) in (p.info['cmdline'] or [])
                                  for p in psutil.process_iter(['cmdline']))
                    if not running:
                        raise RuntimeError(f'Incomplete job without a live guard; inspect {prior}')
                    time.sleep(1)
                    memory = json.loads(prior.read_text())
            if 'exit_code' in memory and memory.get('command') == [str(v) for v in command]:
                print('RECOVER', self.state['active'], memory['exit_code'], flush=True)
                return {'exit_code': memory['exit_code'], 'seconds': None,
                        'command': memory['command'], 'memory_report': str(prior),
                        'recovered': True}
        # Protect against accidentally restarting alongside another sweep.
        import psutil
        for proc in psutil.process_iter(['cmdline']):
            args = proc.info['cmdline'] or []
            if len(args) > 1 and Path(args[0]).name == 'lhd' and args[1] == 'synth':
                raise RuntimeError(f'Another synthesis is running: PID {proc.pid}')
        directory.mkdir(parents=True, exist_ok=True)
        log = directory / 'run.log'
        command = [str(v) for v in command]
        write(directory / 'command.json', command)
        guard = [PYTHON, '-u', '-m', 'logikbench.memory_guard', '--limit-gib', '48',
                 '--reserve-gib', '16', '--soft-gib', '16', '--timeout', str(timeout),
                 '--report', str(directory / 'memory.json'), '--', *command]
        start = time.monotonic()
        print('START', self.state['active'], flush=True)
        with log.open('w') as stream:
            rc = subprocess.run(guard, env=self.env, cwd=REPO, stdout=stream, stderr=subprocess.STDOUT).returncode
        record = {'exit_code': rc, 'seconds': time.monotonic() - start,
                  'command': command, 'memory_report': str(directory / 'memory.json')}
        print('DONE', self.state['active'], rc, round(record['seconds'], 1), flush=True)
        return record

    def publish(self):
        for ctrl in ('false', 'true'):
            label = 'ctrl' + ctrl
            stem = 'lhd_asap7_' + label
            completed = [row for row in self.worklist if f'{ctrl}/{row[0]}/{row[2]}' in self.state['completed']]
            args = SimpleNamespace(builddir=str(self.root / 'runs'), group=ALL_GROUPS, name=None, label=label)
            if completed:
                save_target('lhd_asap7', args, completed)
                data = json.loads((self.root / 'runs/results' / (stem + '.json')).read_text())
            else:
                data = {'meta': {'target': 'lhd_asap7'}, 'metrics': {}, 'status': {}}
            data['meta'].update(display_label=f'lhd_asap7 ctrl_cones={ctrl}', options=options(ctrl),
                                experiment=self.prov)
            data['meta']['refresh'] = {'run': self.root.name, 'binary_sha256': self.prov['binary_sha256'],
                                       'attempted': len(completed), 'total': 250,
                                       'started_utc': self.state['started_utc']}
            data['meta']['row_provenance'] = {}
            for group, _, name in completed:
                data['meta']['row_provenance'].setdefault(group, {})[name] = {
                    'run': self.root.name, 'binary_sha256': self.prov['binary_sha256']}
            write(REPO / 'results/syn/asic' / (stem + '.json'), data)
        with (self.root / 'dashboard.log').open('a') as log:
            for command in ([PYTHON, 'dashboard/build_db.py', '--flat', '--results', 'results/syn/asic',
                             '--out', 'build/dashboard-db', '--config', 'default'],
                            [PYTHON, 'dashboard/generate.py', '--db', 'build/dashboard-db/asic',
                             '--out', 'build/dashboard', '--title', 'ASIC Synthesis']):
                subprocess.run(command, cwd=REPO, stdout=log, stderr=subprocess.STDOUT, check=True)
        write(self.root / 'publication.json', {'attempted': len(self.state['completed']), 'expected': 500,
              'complete': len(self.state['completed']) == 500,
              'updated_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'scope': 'canonical local JSON/dashboard; no git push'})

    def benchmark(self, group, name, ctrl):
        key = f'{ctrl}/{group}/{name}'
        if key in self.state['completed']:
            return
        self.state['active'] = key
        write(self.path, self.state)
        task = self.root / 'jobs' / key
        command = [PYTHON, '-u', '-m', 'logikbench.apps.lb', 'syn', '--tool', 'lhd', '--target', 'asap7',
                   '--label', 'ctrl' + ctrl, '-g', group, '-n', name, '--timeout', '14400', '-j', '1',
                   '--keep', '--no-publish', '--options=' + options(ctrl), '-b', self.root / 'runs']
        record = self.guarded(command, task, 18000)
        bench = self.root / 'runs' / ('lhd_asap7_ctrl' + ctrl) / group / name
        syn = bench / 'job0/synthesis/0'
        report = syn / 'reports'
        report.mkdir(parents=True, exist_ok=True)
        result = syn / 'result.json'
        if result.exists():
            shutil.copy2(result, report / 'lhd_result.json')
            envelope = json.loads(result.read_text())
            color = [s for s in envelope.get('recipe', []) if s.startswith('pass.color ') and 'alg:synth' in s]
            if color or envelope.get('status') == 'pass':
                assert color and all('mode:cones' in s and 'ctrl_cones:' + ctrl in s for s in color), color
        mapping_log = syn / 'synthesis.log'
        if mapping_log.exists(): shutil.copy2(mapping_log, task / 'compiler.log')
        for logs in syn.glob('lhd-work*/logs'):
            shutil.copytree(logs, task / logs.parent.name, dirs_exist_ok=True)
        record.update(summarize(result, task / 'compiler.log'))
        color_stats = [entry for log in task.glob('lhd-work*/*pass_color.log') for entry in read_control_stats(log)]
        if color_stats: record['control_stats'] = color_stats
        write(task / 'summary.json', record)
        # Archive inputs needed to replay measurement; deduplicate large Liberty files.
        libs = {}
        for lib in syn.glob('*.lib'):
            sha = digest(lib)
            stored = self.root / 'libraries' / (sha + '.lib')
            stored.parent.mkdir(exist_ok=True)
            if not stored.exists(): shutil.copy2(lib, stored)
            libs[lib.name] = {'sha256': sha, 'path': str(stored)}
        write(report / 'libraries.json', libs)
        for source in (syn / 'outputs').glob('*'):
            if source.is_file():
                with source.open('rb') as src, gzip.open(report / (source.name + '.gz'), 'wb') as dst:
                    shutil.copyfileobj(src, dst)
        self.state['completed'][key] = record
        self.state.pop('active', None)
        write(self.path, self.state)
        self.publish()
        # The result and raw mapping logs are retained even for failures.
        # Remove only derived analysis copies after the mapped netlist is archived.
        if list(report.glob('*.vg.gz')):
            for n in ('structural.v', 'structural.modules', 'logicdepth.netlist.json', 'normalized.il'):
                path = report / n
                if path.is_dir(): shutil.rmtree(path)
                elif path.exists(): path.unlink()
        clean_build(name, builddir=str(bench.parent))

    def minion(self):
        import shlex
        for reader in ('pyrope', 'yosys'):
            # Establish whole-core ASAP7 QoR before spending time on cache/edit
            # and second-PDK sweeps. Prefer the suite's original Pyrope; use a
            # matched whole-RTL pair only if the Pyrope pair cannot complete.
            pyrope_pair = [self.state['minion'].get(f'pyrope/asap7/{c}/cold') for c in ('false', 'true')]
            if reader == 'yosys' and all(v and v['exit_code'] == 0 for v in pyrope_pair):
                break
            for pdk in ('asap7',):
                for phase in ('cold',):
                    for ctrl in ('false', 'true'):
                        base = self.root / 'minion' / reader / pdk / ctrl
                        key = f'{reader}/{pdk}/{ctrl}/{phase}'
                        if key in self.state['minion']:
                            continue
                        prior = [v for k, v in self.state['minion'].items() if k.startswith(f'{reader}/{pdk}/{ctrl}/')]
                        if any(v['exit_code'] for v in prior): continue
                        self.state['active'] = 'minion/' + key
                        write(self.path, self.state)
                        tree = base / 'sources'
                        if not tree.exists():
                            shutil.copytree(self.root / 'sources/minion' / ('pyrope' if reader == 'pyrope' else 'verilog'), tree)
                        if phase == 'edit':
                            if reader == 'pyrope':
                                source = tree / 'txfma_adder.prp'
                                shutil.copy2(self.root / 'sources/minion/bug1/txfma_adder.prp', source)
                            else:
                                source = tree / 'txfma_adder.sv'
                                text = source.read_text()
                                assert 'sum_tmp = a_i + b_i +' in text
                                source.write_text(text.replace('sum_tmp = a_i + b_i +', 'sum_tmp = a_i - b_i +'))
                        result = base / (phase + '.json')
                        top = 'minion_top.minion_top' if reader == 'pyrope' else 'minion_top'
                        cmd = [self.binary, 'synth', '--top', top, '--workdir', base / 'work',
                               '--result-json', result, '--emit', 'verilog:' + str(base / 'mapped.v'),
                               '--set', 'synth.liberty=' + str(self.root / (pdk + '.lib')),
                               '--set', 'synth.opentimer=true',
                               *shlex.split(options(ctrl, 10000 if pdk == 'sky130' else 200))]
                        cmd += ['--set', 'color.synth.max_gate=5000', '--set', 'abc.memory_budget_mb=16384']
                        if reader == 'pyrope': cmd += [tree / 'minion_top.prp']
                        else:
                            cmd += ['--reader', 'yosys', '--', '-F', tree / 'filelist.f', '-DSYNTHESIS',
                                    '--relax-enum-conversions', '--allow-use-before-declare', '--ignore-assertions']
                        task = base / phase
                        record = self.guarded(cmd, task)
                        record.update(summarize(result, task / 'run.log'))
                        logs = base / 'work/logs'
                        if logs.exists():
                            shutil.copytree(logs, task / 'logs', dirs_exist_ok=True)
                            color_stats = [entry for log in logs.glob('*pass_color.log')
                                           for entry in read_control_stats(log)]
                            if color_stats: record['control_stats'] = color_stats
                        net = base / 'mapped.v'
                        if net.exists(): record['netlist_sha256'] = digest(net)
                        for name in ('qor', 'timing'):
                            file = base / 'work/synth' / (name + '.json')
                            if file.exists(): shutil.copy2(file, base / (phase + '-' + name + '.json'))
                        write(task / 'summary.json', record)
                        self.state['minion'][key] = record
                        self.state.pop('active', None)
                        write(self.path, self.state)
                for ctrl in ('false', 'true'):
                    base = self.root / 'minion' / reader / pdk / ctrl
                    # Keep the final mapped RTL compressed and measured sidecars;
                    # delete only this run's reproducible work/cache after all phases.
                    net = base / 'mapped.v'
                    if net.exists():
                        with net.open('rb') as src, gzip.open(base / 'mapped.v.gz', 'wb') as dst:
                            shutil.copyfileobj(src, dst)
                        net.unlink()
                    if (base / 'work').exists(): shutil.rmtree(base / 'work')

    def run(self):
        os.chdir(REPO)
        lock = (self.root / 'serial.lock').open('w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.publish()
        # Measure mux/enable-bearing cases first, including the previous model's
        # largest duplication regressions, before committing to whole-core runs.
        priority = [('arithmetic', n) for n in ('argmax', 'argmin', 'counter', 'lrelu', 'absdiff', 'clamp')]
        for group, name in priority:
            for ctrl in ('false', 'true'): self.benchmark(group, name, ctrl)
        self.minion()
        for group, _, name in sorted(self.worklist, key=lambda r: (r[0] == 'large', r[0], r[2])):
            for ctrl in ('false', 'true'): self.benchmark(group, name, ctrl)
        self.state['finished_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        write(self.path, self.state)
        self.publish()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    Experiment(args.root).run()
