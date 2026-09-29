"""Make LHD's mapped netlist acceptable to OpenSTA's structural reader."""

import os
import json
import subprocess
from logikbench.tools.lhd.liberty import structural_library
from collections import Counter
import re
import tempfile
from pathlib import Path


class NativeStateError(ValueError):
    """The valid synthesized netlist contains state OpenSTA cannot read."""


_COMB_BLOCK = re.compile(
    r"^[ \t]*always_comb[ \t]+begin[ \t]*\r?\n"
    r"(?P<body>.*?)"
    r"^[ \t]*end[ \t]*;?[ \t]*(?:\r?\n|$)",
    re.MULTILINE | re.DOTALL,
)
_LHS = re.compile(
    r"(?:\\\S+(?:\s+\[[^]]+\])?"
    r"|[A-Za-z_$][\w$]*(?:\s*\[[^]]+\])?)"
)
_PROCEDURAL = re.compile(
    r"^[ \t]*(?:always(?:_ff|_latch)?\b|initial\b)", re.MULTILINE)
_ABC_INPUT_BIT = re.compile(
    r"assign\s+b(?P<bit>\d+)\s*=\s*"
    r"\(a\s*>>>\s*\((?P<shift>[^)]*)\)\)\s*;")
_ZERO_EXTEND_SHIFT = re.compile(
    r"\(\(\(\{(?P<width>\d+)\{1'b0\}\}\s*\|\s*"
    r"(?P<value>[A-Za-z_$][\w$]*(?:\s*\[[^]]+\])?)\)\s*"
    r"<<\s*\((?P<size>\d+)'s?(?P<base>[bodh])"
    r"(?P<shift>[0-9a-fA-F]+)\)\)\)")
_CONST_CELL = re.compile(
    r"^[ \t]*_const(?P<value>[01])_[ \t]+\S+[ \t]*\([ \t\r\n]*"
    r"\.z\((?P<net>[A-Za-z_$][\w$]*)\)[ \t\r\n]*"
    r"\)[ \t]*;[ \t]*(?:\r?\n|$)", re.MULTILINE)
_VECTOR_DECL = re.compile(
    r"(?:^|[(,;])[ \t]*(?:input|output|inout|wire|reg)"
    r"(?:[ \t]+(?:wire|reg|logic))?(?:[ \t]+signed)?[ \t]+"
    r"\[(?P<left>\d+):(?P<right>\d+)\][ \t]+"
    r"(?P<name>[A-Za-z_$][\w$]*)", re.MULTILINE)
_DIRECT_ASSIGN = re.compile(
    r"(?P<prefix>\bassign[ \t]+)(?P<lhs>[A-Za-z_$][\w$]*)"
    r"(?P<middle>[ \t]*=[ \t]*)(?P<rhs>[A-Za-z_$][\w$]*)"
    r"(?P<suffix>[ \t]*;)")
_CONCAT_ASSIGN = re.compile(
    r"(?P<prefix>\bassign[ \t]+)(?P<lhs>[A-Za-z_$][\w$]*)"
    r"(?P<middle>[ \t]*=[ \t]*)\{(?P<body>[^{}]+)\}"
    r"(?P<suffix>[ \t]*;)")


def _shift_to_concat(match):
    shift = int(match.group("shift"), {
        "b": 2,
        "o": 8,
        "d": 10,
        "h": 16,
    }[match.group("base")])
    if shift == 0:
        return match.group("value")
    if shift >= int(match.group("width")):
        return "1'b0"
    return f"{{{match.group('value')}, {shift}'b0}}"


def _normalize_signed_assigns(text):
    """Express signed wiring casts/extension without SystemVerilog functions."""
    ranges = {m.group("name"): (int(m.group("left")), int(m.group("right")))
              for m in _VECTOR_DECL.finditer(text)}
    signed = {m.group("name") for m in _VECTOR_DECL.finditer(text)
              if re.search(r"\bsigned\b", m.group(0))}
    pattern = re.compile(
        r"\bassign\s+(?P<lhs>[A-Za-z_$][\w$]*)\s*=\s*"
        r"(?:(?P<cast>\$signed|\$unsigned)\(\s*)?"
        r"(?P<rhs>[A-Za-z_$][\w$]*)\s*(?(cast)\))\s*;")

    def convert(match):
        lhs, rhs, cast = match.group('lhs', 'rhs', 'cast')
        if lhs not in ranges or rhs not in ranges:
            return match.group(0)
        left, right = ranges[rhs]
        source_width = abs(left - right) + 1
        target_width = abs(ranges[lhs][0] - ranges[lhs][1]) + 1
        is_signed = cast == '$signed' or (cast is None and rhs in signed)
        value = rhs
        if target_width < source_width:
            end = right + (target_width - 1) * (1 if left > right else -1)
            value = f'{rhs}[{end}:{right}]'
        elif target_width > source_width and is_signed:
            # Repetition-free concatenation is accepted by OpenSTA.
            sign = ','.join([f'{rhs}[{left}]'] * (target_width - source_width))
            value = '{' + sign + ',' + rhs + '}'
        return f'assign {lhs} = {value};'

    return pattern.sub(convert, text)


def _truncate_direct_assigns(text):
    """Make Verilog's implicit vector truncation explicit for OpenSTA."""
    ranges = {
        match.group("name"): (int(match.group("left")),
                              int(match.group("right")))
        for match in _VECTOR_DECL.finditer(text)
    }

    def explicit_slice(match):
        lhs_range = ranges.get(match.group("lhs"))
        rhs_range = ranges.get(match.group("rhs"))
        if lhs_range is None or rhs_range is None:
            return match.group(0)
        lhs_width = abs(lhs_range[0] - lhs_range[1]) + 1
        rhs_width = abs(rhs_range[0] - rhs_range[1]) + 1
        if lhs_width >= rhs_width:
            return match.group(0)
        rhs_lsb = rhs_range[1]
        rhs_msb = (rhs_lsb + lhs_width - 1
                   if rhs_range[0] > rhs_lsb
                   else rhs_lsb - lhs_width + 1)
        return (f"{match.group('prefix')}{match.group('lhs')}"
                f"{match.group('middle')}{match.group('rhs')}"
                f"[{rhs_msb}:{rhs_lsb}]{match.group('suffix')}")

    return _DIRECT_ASSIGN.sub(explicit_slice, text)


def _truncate_zero_prefixed_concats(text, constant_values):
    """Remove leading zero bits discarded by an output assignment."""
    ranges = {
        match.group("name"): (int(match.group("left")),
                              int(match.group("right")))
        for match in _VECTOR_DECL.finditer(text)
    }

    def explicit_concat(match):
        lhs_range = ranges.get(match.group("lhs"))
        if lhs_range is None:
            return match.group(0)
        lhs_width = abs(lhs_range[0] - lhs_range[1]) + 1
        items = [item.strip() for item in match.group("body").split(",")]
        discarded = len(items) - lhs_width
        if discarded <= 0:
            return match.group(0)
        if any(constant_values.get(item) != "0"
               for item in items[:discarded]):
            return match.group(0)
        if any(item in ranges for item in items[discarded:]):
            return match.group(0)
        body = ",".join(items[discarded:])
        return (f"{match.group('prefix')}{match.group('lhs')}"
                f"{match.group('middle')}{{{body}}}{match.group('suffix')}")

    return _CONCAT_ASSIGN.sub(explicit_concat, text)


def _continuous_assigns(match):
    assignments = []
    for statement in match.group("body").split(";"):
        statement = statement.strip()
        if not statement:
            continue
        if "=" not in statement:
            raise ValueError(
                "LHD emitted a non-assignment inside always_comb")
        lhs, rhs = (part.strip() for part in statement.split("=", 1))
        if not _LHS.fullmatch(lhs) or not rhs:
            raise ValueError(
                f"LHD emitted a non-structural always_comb statement: "
                f"{statement[:120]}")
        # OpenSTA's structural Verilog reader accepts concatenations, but not
        # LHD's redundant expression wrapper around one.
        wrapped_concat = re.fullmatch(r"\(\s*(\{.*\})\s*\)", rhs, re.DOTALL)
        if wrapped_concat:
            rhs = wrapped_concat.group(1)
        assignments.append(f"assign {lhs} = {rhs};\n")
    return "".join(assignments)


def _input_bit_select(match):
    literal = re.fullmatch(r"(?P<size>\d+)?'(?P<signed>s)?(?P<base>[bodh])(?P<value>[0-9a-fA-F_]+)",
                           match.group('shift').strip())
    if not literal:
        return match.group()
    shift = int(literal.group('value').replace('_', ''),
                {'b': 2, 'o': 8, 'd': 10, 'h': 16}[literal.group('base')])
    if literal.group('size'):
        width = int(literal.group('size'))
        shift &= (1 << width) - 1
        if literal.group('signed') and width and shift & (1 << (width - 1)):
            shift -= 1 << width
    bit = int(match.group('bit'))
    if shift != bit:
        return match.group()
    return f"assign b{bit} = a[{bit}];"


def prepare_input_bits(text):
    """Keep generated scalar bit extraction linear in the source bus width."""
    return re.sub(
        r"^module __livehd_abc_input_bits_\d+\b.*?^endmodule\b",
        lambda module: _ABC_INPUT_BIT.sub(
            _input_bit_select, _COMB_BLOCK.sub(_continuous_assigns, module.group())),
        text, flags=re.MULTILINE | re.DOTALL)


def _normalize_with_yosys(path, liberty, log_path=None, blackboxes=(), rtlil=None):
    """Lower only structural wiring syntax, preserving every technology cell."""
    original = path.read_text()
    if _PROCEDURAL.search(original) or re.search(r'`include\s+"cgen_memory_', original):
        raise NativeStateError("mapped netlist contains native procedural state or memory")
    with tempfile.TemporaryDirectory(prefix="lhd-wiring-", dir=path.parent) as scratch:
        scratch = Path(scratch)
        source = scratch / "source.v"
        original = prepare_input_bits(original)
        source.write_text(_CONST_CELL.sub(
            lambda m: f"assign {m.group('net')} = 1'b{m.group('value')};\n", original))
        script = scratch / "normalize.ys"

        def quote(value):
            return json.dumps(str(Path(value).resolve()))

        script.write_text(
            f"read_rtlil {quote(structural_library(liberty))}\n"
            + "".join(f"read_verilog -sv -lib {quote(model)}\n" for model in blackboxes)
            + f"read_verilog -sv {quote(source)}\n"
            "proc\ndelete t:$scopeinfo\n"
            "select -write before.cells c:* t:$* %d\n"
            "tee -o before.json stat -json\n"
            "opt_expr\n"
            "select -write after.cells c:* t:$* %d\n"
            "tee -o after.json stat -json\n"
            f"write_verilog -noattr -noexpr {quote(scratch / 'normalized.v')}\n"
            + (f"write_rtlil {quote(rtlil)}\n" if rtlil else ""))
        result = subprocess.run(["yosys", "-Q", "-T", "-s", str(script.resolve())],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=600, cwd=scratch)
        log = Path(log_path) if log_path is not None else path.with_suffix(".normalize.log")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(result.stdout)
        if result.returncode:
            raise ValueError(f"structural wiring reader failed; see {log}")
        before = json.loads((scratch / "before.json").read_text())
        after = json.loads((scratch / "after.json").read_text())

        def mapped_cells(data):
            return Counter({(name, kind): count
                           for name, module in data["modules"].items()
                           for kind, count in module.get("num_cells_by_type", {}).items()
                           if not kind.startswith("$")})
        before_names = set((scratch / 'before.cells').read_text().splitlines())
        after_names = set((scratch / 'after.cells').read_text().splitlines())
        if mapped_cells(before) != mapped_cells(after) or before_names != after_names:
            raise ValueError("structural normalization changed technology cells")
        primitives = {kind for module in after["modules"].values()
                      for kind in module.get("num_cells_by_type", {})
                      if kind.startswith("$")}
        if primitives:
            raise ValueError("unmapped computational cells remain: " + ", ".join(sorted(primitives)))
        # No techmap, ABC, flattening or gate optimization is performed. opt_expr
        # resolves constant shifts/casts/selects to connections in the RTLIL.
        text = (scratch / "normalized.v").read_text()
        # RTLIL already made every extension/truncation an explicit connection.
        # OpenSTA rejects signed declarations, which carry no remaining arithmetic.
        text = re.sub(r"\b((?:input|output|inout|wire|reg)\s+)signed\s+", r"\1", text)
        path.write_text(text)


def normalize_for_opensta(path, liberty=None, log_path=None, blackboxes=(), rtlil=None):
    """Replace LHD wiring processes with assigns, rejecting real processes.

    This is intentionally not a synthesis pass: it makes only
    semantics-preserving wiring rewrites around LHD's technology-cell
    instances. If LHD leaves a clocked, latched, initial, or otherwise
    non-wiring process in the mapped netlist, fail instead of asking another
    mapper to alter the QoR being measured.
    """
    path = Path(path)
    if liberty is not None:
        return _normalize_with_yosys(path, liberty, log_path, blackboxes, rtlil)
    text = path.read_text(errors="replace")
    normalized = _COMB_BLOCK.sub(_continuous_assigns, text)
    if re.search(r"\balways_comb\b", normalized):
        raise ValueError(
            "LHD emitted an unsupported nested or malformed always_comb block")

    process = _PROCEDURAL.search(normalized)
    if process:
        line = normalized.count("\n", 0, process.start()) + 1
        raise NativeStateError(
            "LHD left procedural state in its mapped netlist at line "
            f"{line}; refusing to remap it before OpenSTA")

    # With every process removed, every procedural-looking declaration was
    # only an LHD wiring temporary or a cell-driven net.
    normalized = re.sub(r"\breg\b", "wire", normalized)

    # ABC partition wrappers extract individual vector bits with signed
    # SystemVerilog shifts.  OpenSTA's Verilog reader supports neither the
    # signed ANSI declarations nor those signed literals.  A one-bit output of
    # ``a >>> bit`` is exactly ``a[bit]``, so retain the same wiring in plain
    # structural Verilog and discard declaration signedness.
    normalized = _ABC_INPUT_BIT.sub(_input_bit_select, normalized)
    normalized = _ZERO_EXTEND_SHIFT.sub(_shift_to_concat, normalized)
    constant_values = {
        match.group("net"): match.group("value")
        for match in _CONST_CELL.finditer(normalized)
    }
    normalized = _CONST_CELL.sub(
        lambda match: f"assign {match.group('net')} = "
        f"1'b{match.group('value')};\n",
        normalized)

    def wiring(module):
        text = _normalize_signed_assigns(module.group(0))
        text = _truncate_direct_assigns(text)
        return _truncate_zero_prefixed_concats(text, constant_values)

    normalized = re.sub(r"^module\b.*?^endmodule\b", wiring, normalized,
                        flags=re.MULTILINE | re.DOTALL)
    normalized = re.sub(r"\bsigned\s+", "", normalized)
    with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=path.name, suffix=".tmp",
            delete=False) as stream:
        stream.write(normalized)
        temporary = Path(stream.name)
    os.replace(temporary, path)
