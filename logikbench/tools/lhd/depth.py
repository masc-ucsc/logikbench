"""Mapped-cell depth across hierarchy, with registers and memories as cuts.

Yosys only elaborates and resolves wiring in a copy of the mapped netlist. It
never technology-maps or rewrites the netlist used by timing or equivalence.
"""

from collections import deque
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import psutil
from logikbench.tools.lhd.liberty import structural_library

from logikbench.tools.lhd.liberty import cell_metadata
from logikbench.tools.lhd.netlist import prepare_input_bits


_NATIVE_STATE = re.compile(r'^\$_?(?:[a-z]*dff|[a-z]*latch|ff|mem)', re.I)
_STATE_CONTROLS = {'CLK', 'EN', 'CE', 'ARST', 'SRST', 'ALOAD',
                   'SET', 'CLR', 'C', 'E', 'R', 'S'}


def analysis_sources(source, threshold=16 * 2**20):
    """Bound Yosys's temporary AST by reading generated modules separately.

    Preserve every byte and its order. Sources with preprocessor directives
    stay together because macros can carry state between module definitions.
    This only changes input batching, not the analysis or mapped netlist.
    """
    source = Path(source)
    if source.stat().st_size <= threshold:
        return [source]
    with source.open('rb') as stream:
        if any(b'`' in line for line in stream):
            return [source]
    directory = source.with_suffix('.modules')
    directory.mkdir(exist_ok=True)
    paths = []
    output = None
    in_comment = False
    tokens = re.compile(rb'//|/\*|"(?:\\.|[^"\\])*"')
    try:
        with source.open('rb') as stream:
            for line in stream:
                if output is None:
                    path = directory / f'module-{len(paths):05d}.sv'
                    paths.append(path)
                    output = path.open('wb')
                output.write(line)
                # LiveHD emits module terminators on their own line.
                if not in_comment and line.strip() == b'endmodule':
                    output.close()
                    output = None
                position = 0
                while position < len(line):
                    if in_comment:
                        end = line.find(b'*/', position)
                        if end < 0:
                            break
                        in_comment = False
                        position = end + 2
                    else:
                        token = tokens.search(line, position)
                        if token is None or token.group() == b'//':
                            break
                        in_comment = token.group() == b'/*'
                        position = token.end()
    finally:
        if output is not None:
            output.close()
    return paths


def _native_control_cells(module, library_cells):
    """Control-only logic synthesized by opt_dff belongs to native state.

    Only remove builtin cells whose entire fanout ends in native state control
    pins. An unknown datapath cell or a cone reaching an output remains an
    error; this does not assign a guessed gate depth to unmapped datapaths.
    """
    cells = module.get('cells', {})
    candidates = {n for n, c in cells.items() if c['type'].startswith('$')
                  and c['type'] != '$scopeinfo' and c['type'] not in library_cells
                  and not _NATIVE_STATE.match(c['type'])}
    if not candidates:
        return set()
    drivers = {}
    for n in candidates:
        for port, bits in cells[n]['connections'].items():
            if cells[n]['port_directions'][port] == 'output':
                for bit in bits:
                    if isinstance(bit, int):
                        drivers.setdefault(bit, set()).add(n)
    users = {n: set() for n in candidates}
    exposed = set()
    for port in module.get('ports', {}).values():
        if port['direction'] in ('output', 'inout'):
            for bit in port['bits']:
                exposed.update(drivers.get(bit, ()))
    for n, c in cells.items():
        for port, bits in c['connections'].items():
            direction = c['port_directions'][port]
            for bit in bits:
                for driver in drivers.get(bit, ()):
                    if direction != 'output':
                        users[driver].add((n, port))
                    elif driver != n:
                        exposed.add(driver)  # ambiguous drivers are not a state cone
    ignored = set()
    while True:
        ready = set()
        for n in candidates - ignored - exposed:
            if users[n] and all(
                    consumer in ignored or
                    (cells[consumer]['type'] not in library_cells and
                     _NATIVE_STATE.match(cells[consumer]['type']) and port in _STATE_CONTROLS)
                    for consumer, port in users[n]):
                ready.add(n)
        if not ready:
            return ignored
        ignored.update(ready)


def cell_depth(design, top, sequential, library_cells):
    """Longest combinational cell path in flattened Yosys JSON."""
    module = design['modules'][top]
    cells = module.get('cells', {})
    native_controls = _native_control_cells(module, library_cells)
    comb = {}
    drivers = {}
    for name, cell in cells.items():
        kind = cell['type']
        if kind == '$scopeinfo' or name in native_controls:
            continue  # hierarchy provenance, with no hardware behavior
        state = kind in sequential or _NATIVE_STATE.match(kind)
        if state:
            continue
        if kind not in library_cells:
            raise ValueError(f'unmapped or unknown cell {kind}: {name}')
        comb[name] = cell
        for port, bits in cell['connections'].items():
            direction = cell['port_directions'][port]
            if direction == 'inout':
                raise ValueError(f'bidirectional cell {name}')
            if direction == 'output':
                for bit in bits:
                    if not isinstance(bit, int):
                        continue
                    if bit in drivers and drivers[bit] != name:
                        raise ValueError(f'multiple drivers for bit {bit}')
                    drivers[bit] = name
    dependencies = {}
    consumers = {name: [] for name in comb}
    for name, cell in comb.items():
        parents = {drivers[bit]
                   for port, bits in cell['connections'].items()
                   if cell['port_directions'][port] == 'input'
                   for bit in bits if bit in drivers}
        dependencies[name] = len(parents)
        for parent in parents:
            consumers[parent].append(name)
    ready = deque(name for name, degree in dependencies.items() if not degree)
    levels = {name: 1 for name in ready}
    visited = 0
    while ready:
        name = ready.popleft()
        visited += 1
        for child in consumers[name]:
            levels[child] = max(levels.get(child, 1), levels[name] + 1)
            dependencies[child] -= 1
            if not dependencies[child]:
                ready.append(child)
    if visited != len(comb):
        raise ValueError('combinational cycle in mapped netlist')
    return max(levels.values(), default=0)


def hierarchical_depth(design, top, sequential, library_cells, mapping_cells=None):
    """Evaluate pin arrivals through shared definitions without flattening.

    Instance outputs are evaluated lazily, so independent ready/data outputs
    do not introduce the false cycles of an all-inputs-to-all-outputs model.
    An occurrence retains only its output arrivals once its interior is done.
    """
    modules = design['modules']
    definitions = {}
    counts = {}
    counting = set()
    mapping_cells = library_cells if mapping_cells is None else mapping_cells
    deepest = 0

    def definition(name):
        if name in definitions:
            return definitions[name]
        if name in counting:
            raise ValueError(f'recursive module hierarchy: {name}')
        counting.add(name)
        module = modules[name]
        inputs, drivers, outputs = {}, {}, []
        for port, info in module.get('ports', {}).items():
            if info['direction'] == 'inout':
                raise ValueError(f'bidirectional module {name}')
            for index, bit in enumerate(info['bits']):
                if info['direction'] == 'input' and isinstance(bit, int):
                    inputs[bit] = (port, index)
                elif info['direction'] == 'output':
                    outputs.append(bit)
        cells = {n: c for n, c in module.get('cells', {}).items()
                 if c['type'] != '$scopeinfo'}
        total = [0, 0, 0]
        for cell_name, cell in cells.items():
            kind = cell['type']
            if kind in library_cells:
                total[0 if kind in mapping_cells else 1] += 1
            elif _NATIVE_STATE.match(kind):
                total[2] += 1
            elif kind in modules and not modules[kind].get('attributes', {}).get('blackbox'):
                definition(kind)
                total = [a + b for a, b in zip(total, counts[kind])]
            else:
                raise ValueError(f'unmapped or unknown cell {kind}: {cell_name}')
            for port, bits in cell['connections'].items():
                direction = cell['port_directions'][port]
                if direction == 'inout':
                    raise ValueError(f'bidirectional cell {cell_name}')
                if direction != 'output':
                    continue
                for index, bit in enumerate(bits):
                    if isinstance(bit, int):
                        if bit in drivers and drivers[bit][0] != cell_name:
                            raise ValueError(f'multiple drivers for bit {bit} in {name}')
                        drivers[bit] = (cell_name, port, index)
        counting.remove(name)
        counts[name] = total
        definitions[name] = (cells, inputs, drivers, outputs)
        return definitions[name]

    class Occurrence:
        def __init__(self, name, parent=None, connections=None):
            self.name = name
            self.parent = parent
            self.connections = connections
            self.cells, self.inputs, self.drivers, self.outputs = definition(name)
            self.levels = {}
            self.children = {}
            self.visiting = set()

        def child(self, name):
            if name not in self.children:
                cell = self.cells[name]
                self.children[name] = Occurrence(cell['type'], self, cell['connections'])
            return self.children[name]

        def level(self, bit):
            nonlocal deepest
            if not isinstance(bit, int):
                return 0
            if bit in self.levels:
                return self.levels[bit]
            if bit in self.visiting:
                raise ValueError(f'combinational cycle in mapped netlist: {self.name}')
            self.visiting.add(bit)
            if bit in self.inputs:
                port, index = self.inputs[bit]
                value = (self.parent.level(self.connections[port][index])
                         if self.parent else 0)
            elif bit not in self.drivers:
                value = 0
            else:
                name, port, index = self.drivers[bit]
                cell = self.cells[name]
                kind = cell['type']
                if kind in sequential or _NATIVE_STATE.match(kind):
                    value = 0
                elif kind in library_cells:
                    value = 1 + max((self.level(source)
                                     for pin, bits in cell['connections'].items()
                                     if cell['port_directions'][pin] == 'input'
                                     for source in bits), default=0)
                    deepest = max(deepest, value)
                    for pin, bits in cell['connections'].items():
                        if cell['port_directions'][pin] == 'output':
                            self.levels.update((b, value) for b in bits if isinstance(b, int))
                else:
                    child_bit = modules[kind]['ports'][port]['bits'][index]
                    value = self.child(name).level(child_bit)
            self.visiting.remove(bit)
            self.levels[bit] = value
            return value

        def finish(self):
            # Include paths ending at state, and internal paths that do not
            # reach a top output. Finish children only after the parent has
            # resolved its cells: eagerly visiting an unrelated child input
            # during output evaluation could create a false dependency cycle.
            for bit in self.drivers:
                self.level(bit)
            for bit in self.outputs:
                self.level(bit)
            for name, cell in self.cells.items():
                if cell['type'] not in library_cells and not _NATIVE_STATE.match(cell['type']):
                    self.child(name)
            for child in self.children.values():
                child.finish()
            self.levels = {b: self.level(b) for b in self.outputs if isinstance(b, int)}
            self.children.clear()

    limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(max(limit, 100000))
        Occurrence(top).finish()
    finally:
        sys.setrecursionlimit(limit)
    return deepest, counts[top]


def measure(netlist, liberty, top, report_dir='reports', blackboxes=(), mapping_liberty=None, rtlil=None):
    """Return depth, retaining exact structural replay and failure diagnostics."""
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / 'logicdepth.json'
    script = report_dir / 'logicdepth.ys'
    graph = report_dir / 'logicdepth.netlist.json'
    log = report_dir / 'logicdepth.log'
    guard_report = report_dir / 'logicdepth_memory.json'
    guard_report.unlink(missing_ok=True)
    result = {'kind': 'mapped-combinational-cell-depth', 'top': top,
              'scope': 'whole design; register and memory boundaries cut paths',
              'logicdepth': None}
    try:
        library_cells, sequential = cell_metadata(liberty)

        def quoted(path):
            return json.dumps(str(Path(path).resolve()))
        if re.search(r'[\s;"\\]', top):
            raise ValueError('unsupported top name for Yosys command script')
        constants = report_dir / 'logicdepth_constants.v'
        text = Path(netlist).read_text()
        source = report_dir / "structural.v"
        if not rtlil:
            prepared = prepare_input_bits(text)
            # Moving the analysis copy must preserve source-relative includes.
            # Yosys does not consistently unquote -I paths containing spaces.

            def include_path(match):
                include = Path(netlist).resolve().parent / match.group(2)
                return match.group(1) + json.dumps(str(include)) if include.is_file() else match.group()
            prepared = re.sub(r'(?m)^(\s*`include\s+)"([^"\n]+)"', include_path, prepared)
            source.write_text(prepared)
        constants.write_text('\n'.join(
            f'module _const{value}_(output z); assign z = 1\'b{value}; endmodule'
            for value in (0, 1)
            if not re.search(rf'\bmodule\s+_const{value}_\b', text)))
        del text
        if not rtlil:
            del prepared
        sources = analysis_sources(source) if not rtlil else []
        cleanup = 'opt_clean -purge\n' if rtlil else (
            'proc\nopt_expr -fine -mux_undef\nopt_dff\nopt_clean -purge\nflatten\n'
            'opt_expr -fine -mux_undef\nopt_dff\nopt_clean -purge\nwreduce\n'
            # Restrict iterative cleanup to native cells: merging identical
            # technology cells would change the measured physical inventory.
            'opt -fine -mux_undef t:$*\nsplitcells\n'
            'opt_expr -fine -mux_undef\nopt_dff\nopt_clean -purge\n'
            # Splitting a bus can expose a scalar hold mux on an already
            # enabled flop. Repeat native-only folding to absorb that mux into
            # the state enable before counting combinational technology cells.
            'wreduce\nopt -fine -mux_undef t:$*\nsplitcells\n'
            'opt_expr -fine -mux_undef\nopt_dff\nopt_clean -purge\n'
        )
        script.write_text(
            f'read_verilog {quoted(constants)}\n'
            f'read_rtlil {quoted(structural_library(liberty))}\n'
            + ''.join(f'read_verilog -sv -lib {quoted(model)}\n' for model in blackboxes)
            + (f'read_rtlil {quoted(rtlil)}\n' if rtlil else
               ''.join(f'read_verilog -sv -I {quoted(Path(netlist).parent)} {quoted(part)}\n'
                       for part in sources))
            + f'hierarchy -check -top {top}\n'
            # Fold native flop enables back into state primitives; their hold
            # muxes are not independently mapped combinational cells.
            # Clean each definition before expanding the analysis copy. A
            # whole-design opt_clean builds enormous connectivity tables for
            # repeated native-state wrappers (LPDDR5/AXI/Bitcoin). Finish with
            # expression/state folding to resolve constants across boundaries.
            # Public aliases and source attributes are irrelevant to depth;
            # retaining them made Bwave's analysis JSON exceed 13 GB. This
            # cleanup only affects the analysis copy of the mapped netlist.
            'setattr -unset src -unset hdlname\n'
            + cleanup
            + f'write_json {quoted(graph)}\n')
        yosys = shutil.which('yosys')
        if not yosys:
            raise ValueError('yosys is required for structural depth measurement')
        with log.open('w') as stream:
            # Optional analysis must not kill a sweep after synthesis succeeded.
            # Release source strings before starting a separately bounded reader.
            memory = psutil.virtual_memory().total / 2**30
            subprocess.run([sys.executable, '-m', 'logikbench.memory_guard',
                            # Large designs still need resident RTLIL; the
                            # 16 GiB soft target remains reported separately
                            # from a 32 GiB ceiling and the system reserve.
                            '--limit-gib', str(min(32 if len(sources) > 1 else 16,
                                                   memory * 0.5)),
                            '--reserve-gib', str(min(16, memory * 0.25)),
                            '--timeout', '600', '--report', str(guard_report), '--',
                            yosys, '-Q', '-T', '-s', str(script)],
                           stdout=stream, stderr=subprocess.STDOUT,
                           check=True)
        design = json.loads(graph.read_text())
        # Count physical leaf instances, including mapped registers. Hierarchy
        # wrappers and Yosys provenance records are not technology cells.
        mapping_cells = library_cells
        if mapping_liberty:
            mapping_cells, _ = cell_metadata(mapping_liberty)
        if rtlil:
            result['analysis'] = 'hierarchical pin arrivals'
            result['logicdepth'], counts = hierarchical_depth(
                design, top, sequential, library_cells, mapping_cells)
            result.update(zip(('mapped_cells', 'macro_cells', 'native_cells'), counts))
        else:
            cells = design['modules'][top].get('cells', {}).values()
            macro_cells = library_cells - mapping_cells
            result['mapped_cells'] = sum(cell['type'] in mapping_cells for cell in cells)
            result['macro_cells'] = sum(cell['type'] in macro_cells for cell in cells)
            result['native_cells'] = sum(cell['type'] not in library_cells
                                         and cell['type'] != '$scopeinfo' for cell in cells)
            result['logicdepth'] = cell_depth(design, top, sequential, library_cells)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        result['reason'] = str(error)
        if guard_report.is_file():
            result['resources'] = json.loads(guard_report.read_text())
    report.write_text(json.dumps(result, indent=2) + '\n')
    return result['logicdepth']
