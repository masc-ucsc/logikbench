"""Stage the single Liberty file required by ``lhd synth``.

SiliconCompiler PDKs may expose a library as several Liberty files (ASAP7
splits combinational, sequential, and buffer/inverter cells).  LiveHD and its
embedded ABC mapper intentionally take one complete library, so combine those
top-level groups into one deterministic file.
"""

import fnmatch
import gzip
import hashlib
import os
import re
import tempfile
from pathlib import Path


_GROUP_HEADER = re.compile(r"([A-Za-z_]\w*)\s*(?:\(([^{};]*)\))?\s*$")
_KEEP_GROUPS = {
    "cell",
    "type",  # SRAM bus declarations referenced by bus_type
    "lu_table_template",
    "output_current_template",
    "power_lut_template",
}


def _read(path):
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", errors="replace") as stream:
            return stream.read()
    return path.read_text(errors="replace")


# Consume strings/comments in C rather than walking 100+ MB libraries byte by
# byte in Python. Braces within strings and comments remain non-structural.
_STRUCTURAL = re.compile(r'/\*.*?\*/|//[^\n]*|"[^"\\]*(?:\\.[^"\\]*)*"|[{}]', re.DOTALL)


def _structural(text):
    """Yield braces outside comments and quoted strings."""
    for match in _STRUCTURAL.finditer(text):
        token = match.group()
        if token in ("{", "}"):
            yield match.start(), token


def _library_bounds(text):
    depth = 0
    opening = -1
    for offset, char in _structural(text):
        if char == "{":
            if opening < 0:
                opening = offset
            depth += 1
        else:
            depth -= 1
            if opening >= 0 and depth == 0:
                return opening, offset
    raise ValueError("input has no balanced top-level library group")


def _top_groups(text):
    opening, closing = _library_bounds(text)
    groups = []
    depth = 0
    group_start = -1
    header = None
    boundary = opening + 1
    for offset, char in _structural(text[opening + 1:closing]):
        offset += opening + 1
        if char == "{":
            if depth == 0:
                match = _GROUP_HEADER.search(text[boundary:offset])
                if match:
                    group_start = boundary + match.start()
                    header = (match.group(1),
                              " ".join((match.group(2) or "").split()))
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                if header is not None and group_start >= 0:
                    groups.append((header, text[group_start:offset + 1]))
                header = None
                group_start = -1
                boundary = offset + 1
    return groups


_UNIT_PREFIX = {"": 1.0, "f": 1e-15, "p": 1e-12, "n": 1e-9,
                "u": 1e-6, "m": 1e-3, "k": 1e3}
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_NUMBERS = re.compile(_NUMBER)
_TIME_TABLES = {"cell_rise", "cell_fall", "rise_transition", "fall_transition",
                "rise_constraint", "fall_constraint", "retaining_rise", "retaining_fall"}


def _units(text):
    # Unit declarations precede cells; avoid repeatedly scanning their large tables.
    first_cell = re.search(r"^\s*cell\s*\(", text, re.M)
    if first_cell:
        text = text[:first_cell.start()]
    units = {}
    for kind, attribute, suffix in (
        ("time", "time_unit", "s"),
        ("power", "leakage_power_unit", "W"),
        ("current", "current_unit", "A"),
        ("voltage", "voltage_unit", "V"),
        ("resistance", "pulling_resistance_unit", "ohm"),
    ):
        match = re.search(rf'\b{attribute}\s*:\s*"?({_NUMBER})([fpnumk]?){suffix}\b', text, re.I)
        if match:
            units[kind] = float(match[1]) * _UNIT_PREFIX[match[2].lower()]
    match = re.search(rf'\bcapacitive_load_unit\s*\(\s*({_NUMBER})\s*,\s*([fpnumk]?)f\s*\)', text, re.I)
    if match:
        units['capacitance'] = float(match[1]) * _UNIT_PREFIX[match[2].lower()]
    return units


def _normalize_units(text, base):
    """Express appended groups in the destination library's physical units.

    In particular, ASAP7 SRAM models use ns/pF/uW while its standard cells use
    ps/fF/pW. Copying groups beneath the latter header silently changes timing
    and power by factors of 1000 or 1000000.
    """
    source, destination = _units(text), _units(base)
    factors = {kind: value / destination.get(kind, value) for kind, value in source.items()}
    if all(abs(value - 1.0) < 1e-12 for value in factors.values()):
        return text

    def variable_factor(variable):
        if variable in {'input_net_transition', 'related_pin_transition',
                        'constrained_pin_transition', 'input_transition_time', 'time'}:
            return factors.get('time', 1)
        if variable in {'total_output_net_capacitance', 'related_out_total_output_net_capacitance'}:
            return factors.get('capacitance', 1)
        if variable in {'input_voltage', 'output_voltage'}:
            return factors.get('voltage', 1)
        return 1

    templates = {}
    for (kind, name), group in _top_groups(text):
        if kind.endswith('template'):
            templates[name] = {int(axis): variable_factor(variable)
                               for axis, variable in re.findall(r'\bvariable_([123])\s*:\s*(\w+)', group)}

    def numbers(value, factor):
        if abs(factor - 1.0) < 1e-12:
            return value
        return _NUMBERS.sub(lambda m: format(float(m[0]) * factor, '.17g'), value)

    def segment(value, kind, name):
        axes = templates.get(name, {})
        result_factor = (factors.get('time', 1) if kind in _TIME_TABLES else
                         factors.get('power', 1) if kind in {'rise_power', 'fall_power', 'power'} else
                         factors.get('current', 1) if kind == 'vector' else 1)

        def table_values(match):
            prefix, contents = match[0].split('(', 1)
            factor = result_factor if match[1] == 'values' else axes.get(int(match[1][-1]), 1)
            return prefix + '(' + numbers(contents, factor)

        value = re.sub(r'\b(index_[123]|values)\s*\([^)]*\)', table_values, value)

        def attribute(match):
            attr = match[2]
            unit = None
            if 'capacitance' in attr or attr in {
                'default_input_pin_cap', 'default_output_pin_cap', 'default_inout_pin_cap',
            }:
                unit = 'capacitance'
            elif attr in {'max_transition', 'min_transition', 'default_max_transition',
                          'min_pulse_width_high', 'min_pulse_width_low', 'minimum_period', 'reference_time'}:
                unit = 'time'
            elif (attr in {'cell_leakage_power', 'default_cell_leakage_power'}
                  or (attr == 'value' and kind == 'leakage_power')):
                unit = 'power'
            if unit is None:
                return match[0]
            return match[1] + numbers(match[3], factors.get(unit, 1))

        return re.sub(rf'\b((\w+)\s*:\s*)({_NUMBER})',
                      attribute, value)

    def group_convert(group, kind, name):
        cursor = 0
        pieces = []
        for (child_kind, child_name), child in _top_groups(group):
            offset = group.index(child, cursor)
            pieces.append(segment(group[cursor:offset], kind, name))
            pieces.append(group_convert(child, child_kind, child_name))
            cursor = offset + len(child)
        pieces.append(segment(group[cursor:], kind, name))
        return ''.join(pieces)

    # The caller only appends the converted child groups, so the original
    # header is deliberately preserved (and remains useful for provenance).
    return group_convert(text, 'library', '')


def merge(inputs):
    """Return one Liberty containing the unique cells/templates in inputs."""
    paths = [Path(path) for path in inputs]
    if not paths:
        raise ValueError("at least one input Liberty is required")
    texts = [_read(path) for path in paths]
    base = texts[0]
    opening, closing = _library_bounds(base)
    prefix = re.sub(
        r"\blibrary\s*\([^)]*\)",
        "library (logikbench_lhd_merged)",
        base[:opening + 1],
        count=1,
    )

    seen = {signature for signature, _ in _top_groups(base)}
    additions = []
    for path, text in zip(paths[1:], texts[1:]):
        text = _normalize_units(text, base)
        for signature, group in _top_groups(text):
            if signature[0] not in _KEEP_GROUPS or signature in seen:
                continue
            seen.add(signature)
            additions.append(
                f"\n\n  /* merged from {path.name} */\n" + group.rstrip())

    provenance = ("\n  /* merged inputs: "
                  + ", ".join(path.name for path in paths) + " */\n")
    return (prefix + provenance + base[opening + 1:closing].rstrip()
            + "".join(additions) + "\n}\n")


_CELL_HEAD = re.compile(r'(\bcell\s*\(\s*"?)([^")\s]+)("?\s*\)\s*\{)')


def mark_dont_use(text, patterns):
    """Stamp `dont_use : true;` into every cell whose name matches a pattern.

    The PDK keeps its do-not-use cells (weak drives, hold buffers, clock
    cells) as glob patterns in SiliconCompiler config, which yosys receives
    as `abc -dont_use`. LiveHD reads only the Liberty, and honours the
    attribute the way any Liberty consumer does, so the staged mapping
    library carries the mark instead.
    """
    patterns = [p for p in (patterns or []) if p]
    if not patterns:
        return text

    def stamp(match):
        if not any(fnmatch.fnmatchcase(match.group(2), p) for p in patterns):
            return match.group(0)
        return match.group(0) + "\n    dont_use : true;"

    return _CELL_HEAD.sub(stamp, text)


def stage(inputs, builddir, link_name="merged.lib", dont_use=()):
    """Cache a merged Liberty under builddir and link it into the task cwd.

    `dont_use` patterns (see mark_dont_use) are part of the cache identity.
    """
    paths = [Path(path).resolve() for path in inputs]
    if not paths:
        return None

    dont_use = sorted({p for p in (dont_use or []) if p})
    digest = hashlib.sha256(b"livehd-liberty-merge-v4-physical-units\0")
    for path in paths:
        stat = path.stat()
        digest.update(f"{path}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    digest.update(("dont_use:" + "\0".join(dont_use) + "\n").encode())

    cache_dir = Path(builddir).resolve() / ".lhd"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"liberty-{digest.hexdigest()[:20]}.lib"
    if not cached.is_file() or cached.stat().st_size == 0:
        text = mark_dont_use(merge(paths), dont_use)
        with tempfile.NamedTemporaryFile(
                mode="w", dir=cache_dir, prefix="liberty-", suffix=".tmp",
                delete=False) as stream:
            stream.write(text)
            temporary = Path(stream.name)
        os.replace(temporary, cached)

    link = Path(link_name)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(cached)
    return str(link)


def structural_library(liberty):
    """Cache Yosys blackbox declarations, omitting expensive timing tables.

    The source Liberty remains unchanged for LHD and OpenSTA. Atomic writes
    make concurrent benchmark readers safe.
    """
    import json
    import subprocess
    source = Path(liberty).resolve()
    stat = source.stat()
    key = hashlib.sha256(f"{source}\0{stat.st_size}\0{stat.st_mtime_ns}".encode()).hexdigest()[:20]
    cached = source.parent / f"structural-{key}.il"
    if not cached.is_file():
        with tempfile.TemporaryDirectory(dir=source.parent, prefix="structural-") as directory:
            output = Path(directory) / "cells.il"
            script = Path(directory) / "read.ys"
            script.write_text(f"read_liberty -lib {json.dumps(str(source))}\n"
                              f"write_rtlil {json.dumps(str(output))}\n")
            result = subprocess.run(["yosys", "-Q", "-T", "-s", str(script)],
                                    capture_output=True, text=True, timeout=180)
            if result.returncode:
                raise ValueError(f"Cannot read structural library: {result.stdout[-2000:]}")
            os.replace(output, cached)
    return cached


def cell_metadata(liberty):
    """Cache cell names and state boundaries independently of design size."""
    import json
    source = Path(liberty).resolve()
    stat = source.stat()
    key = hashlib.sha256(f"{source}\0{stat.st_size}\0{stat.st_mtime_ns}".encode()).hexdigest()[:20]
    cache = source.parent / f"cells-{key}.json"
    if cache.is_file():
        data = json.loads(cache.read_text())
        return set(data['cells']), set(data['sequential'])
    cells, sequential = set(), set()
    for signature, body in _top_groups(source.read_text()):
        if signature[0] == 'cell':
            name = signature[1].strip('"')
            cells.add(name)
            if re.search(r'\b(?:ff|ff_bank|latch|latch_bank|memory)\s*\(', body):
                sequential.add(name)
    with tempfile.NamedTemporaryFile(mode='w', dir=cache.parent, delete=False) as stream:
        json.dump({'cells': sorted(cells), 'sequential': sorted(sequential)}, stream)
        temporary = stream.name
    os.replace(temporary, cache)
    return cells, sequential
