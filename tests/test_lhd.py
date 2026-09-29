from types import SimpleNamespace

import pytest

from logikbench.apps.lb import resolve_syn_tokens
from logikbench.tools.lhd.liberty import merge
from logikbench.tools.lhd.netlist import normalize_for_opensta


def test_resolve_targetless_lhd():
    args = SimpleNamespace(target=None, tool="lhd")
    assert resolve_syn_tokens(args) == ["lhd"]


def test_resolve_lhd_pdk_target():
    args = SimpleNamespace(target=["asap7"], tool="lhd")
    assert resolve_syn_tokens(args) == ["lhd_asap7"]


def test_target_still_required_for_yosys():
    args = SimpleNamespace(target=None, tool="yosys")
    with pytest.raises(SystemExit, match="--target is required"):
        resolve_syn_tokens(args)


def test_merge_split_liberties(tmp_path):
    first = tmp_path / "first.lib"
    second = tmp_path / "second.lib"
    first.write_text(
        """library (first) {
  time_unit : \"1ns\";
  lu_table_template (delay) { variable_1 : input_net_transition; }
  cell (A) { area : 1.0; }
}
"""
    )
    second.write_text(
        """library (second) {
  time_unit : \"1ns\";
  lu_table_template (delay) { variable_1 : input_net_transition; }
  cell (B) { area : 2.0; }
}
"""
    )

    output = merge([first, second])

    assert "library (logikbench_lhd_merged)" in output
    assert output.count("lu_table_template (delay)") == 1
    assert "cell (A)" in output
    assert "cell (B)" in output


def test_normalize_lhd_wiring_for_opensta(tmp_path):
    netlist = tmp_path / "mapped.vg"
    netlist.write_text(
        """module mapped(input [1:0] a, output reg y);
reg bit0;
wire cell_y;
CELL mapped_cell(.A(bit0), .Y(cell_y));
always_comb begin
  bit0 = a[0];
  y = ({cell_y, bit0});
end
endmodule
"""
    )

    normalize_for_opensta(netlist)
    output = netlist.read_text()

    assert "always_comb" not in output
    assert "reg" not in output
    assert "assign bit0 = a[0];" in output
    assert "assign y = {cell_y, bit0};" in output
    assert "CELL mapped_cell(.A(bit0), .Y(cell_y));" in output


def test_normalize_lhd_abc_input_wrapper_for_opensta(tmp_path):
    netlist = tmp_path / "mapped.vg"
    netlist.write_text(
        """module __livehd_abc_input_bits_4(
  input signed [3:0] a,
  output wire signed b3,
  output wire signed b0
);
assign b3 = (a >>> (3'sh3));
assign b0 = (a >>> (1'sh0));
endmodule
"""
    )

    normalize_for_opensta(netlist)
    output = netlist.read_text()

    assert "signed" not in output
    assert "assign b3 = a[3];" in output
    assert "assign b0 = a[0];" in output


def test_normalize_lhd_zero_extend_shift_for_opensta(tmp_path):
    netlist = tmp_path / "mapped.vg"
    netlist.write_text(
        """module shift(input [3:0] data, output wire [7:0] out);
assign out = ((({8{1'b0}} | data) << (4'sh4)));
endmodule
"""
    )

    normalize_for_opensta(netlist)

    assert "assign out = {data, 4'b0};" in netlist.read_text()


def test_normalize_lhd_constant_cells_for_opensta(tmp_path):
    netlist = tmp_path / "mapped.vg"
    netlist.write_text(
        """module constants(output wire zero, output wire one);
_const0_ zero_cell(
.z(zero)
);
_const1_ one_cell(.z(one));
endmodule
"""
    )

    normalize_for_opensta(netlist)
    output = netlist.read_text()

    assert "_const" not in output
    assert "assign zero = 1'b0;" in output
    assert "assign one = 1'b1;" in output


def test_normalize_lhd_implicit_vector_truncation_for_opensta(tmp_path):
    netlist = tmp_path / "mapped.vg"
    netlist.write_text(
        """module truncate(output wire [3:0] out);
wire [4:0] temporary;
assign out = temporary;
endmodule
"""
    )

    normalize_for_opensta(netlist)

    assert "assign out = temporary[3:0];" in netlist.read_text()


def test_normalize_lhd_zero_prefixed_concat_truncation(tmp_path):
    netlist = tmp_path / "mapped.vg"
    netlist.write_text(
        """module truncate(output wire [1:0] out);
wire zero;
wire bit1;
wire bit0;
_const0_ zero_cell(.z(zero));
assign out = {zero,bit1,bit0};
endmodule
"""
    )

    normalize_for_opensta(netlist)

    assert "assign out = {bit1,bit0};" in netlist.read_text()


def test_reject_unmapped_lhd_state(tmp_path):
    netlist = tmp_path / "state.vg"
    netlist.write_text(
        """module state(input clk, input d, output reg q);
always @(posedge clk) begin
  q <= d;
end
endmodule
"""
    )

    with pytest.raises(ValueError, match="left procedural state"):
        normalize_for_opensta(netlist)


def test_signed_wiring_for_opensta(tmp_path):
    netlist = tmp_path / "signed.vg"
    netlist.write_text("""module cast(input [3:0] a, output wire [7:0] z);
assign z = $signed(a);
endmodule
module same_names(input [7:0] a, output wire [3:0] z);
assign z = $signed(a);
endmodule
""")
    normalize_for_opensta(netlist)
    output = netlist.read_text()
    assert "assign z = {a[3],a[3],a[3],a[3],a};" in output
    assert "assign z = a[3:0];" in output
    assert "$signed" not in output


def test_signed_direct_extension_for_opensta(tmp_path):
    netlist = tmp_path / "signed.vg"
    netlist.write_text("""module cast(input signed [3:0] a, output wire [7:0] z);
assign z = a;
endmodule
""")
    normalize_for_opensta(netlist)
    assert "assign z = {a[3],a[3],a[3],a[3],a};" in netlist.read_text()


def test_structural_reader_preserves_cells_and_signed_wiring(tmp_path):
    import shutil
    if not shutil.which("yosys"):
        pytest.skip("Yosys is required for structural wiring conversion")
    lib = tmp_path / "cells.lib"
    lib.write_text('library(test) { cell(BUF) { area: 1; pin(A) { direction: input; } '
                   'pin(Y) { direction: output; function: "A"; } } }')
    netlist = tmp_path / "mapped.v"
    netlist.write_text("""module mapped(input signed [3:0] a, output reg signed [7:0] y, output z);
BUF keep_gate(.A(a[0]), .Y(z));
always_comb begin
  y = ((($signed(8'sb0) | $signed(a)) << (3'sh2)));
end
endmodule
""")
    original = tmp_path / "original.v"
    original.write_text(netlist.read_text())
    normalize_for_opensta(netlist, lib)
    result = netlist.read_text()
    model = tmp_path / "model.v"
    model.write_text("module BUF(input A, output Y); assign Y=A; endmodule\n")
    script = tmp_path / "equiv.ys"
    script.write_text(
        f'read_verilog -sv "{model}" "{original}"\nprep -top mapped -flatten\n'
        'rename mapped gold\ndesign -stash gold\n'
        f'read_verilog -sv "{model}" "{netlist}"\nprep -top mapped -flatten\n'
        'rename mapped gate\ndesign -stash gate\n'
        'design -copy-from gold -as gold gold\ndesign -copy-from gate -as gate gate\n'
        'equiv_make gold gate equiv\nhierarchy -top equiv\nequiv_simple\nequiv_status -assert\n'
    )
    import subprocess
    check = subprocess.run(["yosys", "-Q", "-T", "-s", str(script)], capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr
    assert "keep_gate" in result
    assert "always" not in result
    assert "$shl" not in result
    assert "$signed" not in result
    assert "a[3]" in result


def test_structural_reader_refuses_unmapped_division(tmp_path):
    import shutil
    if not shutil.which("yosys"):
        pytest.skip("Yosys is required for structural wiring conversion")
    lib = tmp_path / "cells.lib"
    lib.write_text('library(test) { cell(BUF) { area: 1; pin(A) { direction: input; } '
                   'pin(Y) { direction: output; function: "A"; } } }')
    netlist = tmp_path / "mapped.v"
    source = "module mapped(input [7:0] a,b, output [7:0] y); assign y=a/b; endmodule\n"
    netlist.write_text(source)
    with pytest.raises(ValueError, match="unmapped computational cells.*div"):
        normalize_for_opensta(netlist, lib)
    assert netlist.read_text() == source


def test_reader_retry_is_limited_to_unsupported_default_frontend():
    from logikbench.tools.lhd._run_synthesis import retry_command
    command = ["lhd", "synth", "--reader", "slang", "--workdir", "w"]
    result = {"error": {"class": "unsupported"}, "recipe": ["inou.slang files:x.v"]}
    assert retry_command(command, result) == ["lhd", "synth", "--reader", "yosys", "--workdir", "w-yosys"]
    for kind in ["syntax", "internal", "usage"]:
        assert retry_command(command, {**result, "error": {"class": kind}}) is None
    assert retry_command(command + ["--reader", "slang"], result) is None
    assert retry_command(command, {**result, "recipe": result["recipe"] + ["pass.abc top:x"]}) is None
    assert retry_command(command, {}) is None


def test_reader_retry_accepts_absent_error_details():
    from logikbench.tools.lhd._run_synthesis import retry_command
    command = ["lhd", "synth", "--reader", "slang"]
    assert retry_command(command, {"error": None}) is None
    assert retry_command(command, {"error": {"class": "unsupported"}, "recipe": None}) is None


def test_collection_uses_reanalyzed_depth_with_existing_timing(tmp_path):
    import json
    from logikbench.common import read_metrics
    job = tmp_path / 'design/job0'
    reports = job / 'synthesis/0/reports'
    reports.mkdir(parents=True)
    (job / 'design.pkg.json').write_text(json.dumps({'metric': {
        'logicdepth': {'node': {'timing': {'0': {'value': 2}}}},
        'fmax': {'node': {'timing': {'0': {'value': 100}}}}}}))
    (reports / 'logicdepth.json').write_text(json.dumps({
        'kind': 'mapped-combinational-cell-depth', 'logicdepth': 5, 'mapped_cells': 7}))
    assert read_metrics('design', ['logicdepth', 'cells', 'fmax'], str(tmp_path)) == {
        'logicdepth': 5, 'cells': 7, 'fmax': 100}


def test_reader_retry_records_attempt_before_launch(tmp_path, monkeypatch):
    import json
    from logikbench.tools.lhd import _run_synthesis as runner
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner.sys, "argv", [
        "wrapper", "lhd", "synth", "--reader", "slang",
        "--workdir", "w", "--result-json", "result.json"])
    calls = []

    def call(argv, stdout, stderr):
        assert not (tmp_path / "result.json").exists()
        attempts = json.loads((tmp_path / "reports/reader_attempts.json").read_text())
        assert attempts[-1]["exit_code"] is None
        assert attempts[-1]["argv"] == argv
        calls.append(argv)
        if len(calls) == 1:
            (tmp_path / "result.json").write_text(json.dumps({
                "error": {"class": "unsupported"}, "recipe": ["inou.slang files:x.v"]}))
            return 7
        assert (tmp_path / "reports/reader_slang.json").exists()
        return 0
    monkeypatch.setattr(runner.subprocess, "call", call)
    assert runner.main() == 0
    assert len(calls) == 2
    assert json.loads((tmp_path / "reports/reader_attempts.json").read_text())[-1]["exit_code"] == 0


def test_merge_retains_macro_bus_types(tmp_path):
    cells = tmp_path / "cells.lib"
    cells.write_text('library(cells) { cell(BUF) { area: 1; } }')
    macros = tmp_path / "macros.lib"
    macros.write_text("""library(macros) {
      type(bus8) { base_type: array; data_type: bit; bit_width: 8; bit_from: 7; bit_to: 0; downto: true; }
      cell(SRAM) { bus(D) { bus_type: bus8; direction: input; } }
    }""")
    text = merge([cells, macros])
    assert 'type(bus8)' in text
    assert 'bus_type: bus8' in text


def test_stage_lhd_memory_runtime_includes(tmp_path):
    from logikbench.tools.lhd._run_synthesis import stage_runtime
    binary = tmp_path / "bin" / "lhd" / "lhd-frozen"
    binary.parent.mkdir(parents=True)
    binary.touch()
    runtime = binary.parent / "lhd.runfiles" / "_main" / "ware" / "rtl"
    runtime.mkdir(parents=True)
    (runtime / "cgen_memory_1rd_1wr.v").write_text(
        '`include "cgen_memory_helper.v"\nmodule cgen_memory_1rd_1wr; endmodule\n')
    (runtime / "cgen_memory_helper.v").write_text('// helper\n')
    netlist = tmp_path / "out" / "mapped.v"
    netlist.parent.mkdir()
    netlist.write_text('`include "cgen_memory_1rd_1wr.v"\nmodule top; endmodule\n')
    command = [str(binary), "synth", "--emit", "verilog:" + str(netlist)]
    stage_runtime(command)
    assert (netlist.parent / "cgen_memory_helper.v").read_text() == '// helper\n'
    assert (netlist.parent / "cgen_memory_1rd_1wr.v").read_text() == (
        runtime / "cgen_memory_1rd_1wr.v").read_text()
    (runtime / "cgen_memory_helper.v").write_text('// updated helper\n')
    stage_runtime(command)
    assert (netlist.parent / "cgen_memory_helper.v").read_text() == '// updated helper\n'


def test_stage_memory_runtime_from_external_runfiles(tmp_path, monkeypatch):
    from logikbench.tools.lhd._run_synthesis import stage_runtime
    runfiles = tmp_path / "staged"
    runtime = runfiles / "livehd+" / "ware/rtl"
    runtime.mkdir(parents=True)
    (runtime / "cgen_memory_1rd_1wr.v").write_text("// staged runtime\n")
    monkeypatch.setenv("RUNFILES_DIR", str(runfiles))
    binary = tmp_path / "lhd"
    binary.touch()
    netlist = tmp_path / "mapped.v"
    netlist.write_text('`include "cgen_memory_1rd_1wr.v"\n')
    stage_runtime([str(binary), "synth", "--emit", f"verilog:{netlist}"])
    assert (tmp_path / "cgen_memory_1rd_1wr.v").read_text() == "// staged runtime\n"


def test_native_memory_preserves_netlist_before_timing(tmp_path):
    from logikbench.tools.lhd.netlist import NativeStateError
    path = tmp_path / "memory.v"
    source = '`include "cgen_memory_1rd_1wr.v"\nmodule memory; endmodule\n'
    path.write_text(source)
    with pytest.raises(NativeStateError, match="memory"):
        normalize_for_opensta(path, tmp_path / "unused.lib")
    assert path.read_text() == source


def test_liberty_scanner_ignores_quoted_and_commented_braces():
    from logikbench.tools.lhd.liberty import _top_groups
    text = '''library(test) {
      comment : "quoted \\" brace { and }";
      /* cell(bogus) { } */
      // cell(bogus2) { }
      cell(BUF) { pin(A) { direction: input; } }
    }'''
    assert [signature for signature, _ in _top_groups(text)] == [('cell', 'BUF')]


def test_structural_generated_input_bits_do_not_expand_shifts(tmp_path):
    import json
    import shutil
    if not shutil.which('yosys'):
        pytest.skip('Yosys is required')
    netlist = tmp_path / 'mapped.v'
    netlist.write_text('''module __livehd_abc_input_bits_64(
input signed [63:0] a,
output reg signed b63
);
always_comb begin
b63 = (a >>> (7'sh3f));
end
endmodule
module top(input [63:0] a, output y);
wire b;
__livehd_abc_input_bits_64 bits(.a(a), .b63(b));
BUF cell0(.A(b), .Y(y));
endmodule
''')
    liberty = tmp_path / 'cells.lib'
    liberty.write_text('library(test) { cell(BUF) { area: 1; '
                       'pin(A) { direction: input; } '
                       'pin(Y) { direction: output; function: "A"; } } }')
    rtlil = tmp_path / 'normalized.il'
    normalize_for_opensta(netlist, liberty, rtlil=rtlil)
    assert 'assign b63 = a[63];' in netlist.read_text()
    assert '$sshr' not in rtlil.read_text()
    from logikbench.tools.lhd.depth import measure
    assert measure(netlist, liberty, 'top', tmp_path/'reports', rtlil=rtlil) == 1
    report = json.loads((tmp_path/'reports/logicdepth.json').read_text())
    assert report['mapped_cells'] == 1


def test_yosys_synthesis_flags_cover_compile_only_and_leave_native_unchanged():
    from logikbench.tools.lhd._run_synthesis import synthesis_reader_flags
    command = ['lhd', 'synth', '--reader', 'yosys-slang', '--', '-F', 'cmd.f']
    result = synthesis_reader_flags(command)
    assert result[-2:] == ['--ignore-assertions', '--relax-enum-conversions']
    assert synthesis_reader_flags(result) == result
    native = ['lhd', 'synth', '--reader', 'slang']
    compile_only = ['lhd', 'compile', '--reader', 'yosys-slang']
    assert synthesis_reader_flags(native) == native
    assert synthesis_reader_flags(compile_only)[-3:] == ['--', '--ignore-assertions', '--relax-enum-conversions']


def test_synthesis_profiles_use_compiler_defaults():
    from logikbench.tools.lhd.lhd import synthesis_settings
    for pdk in ("asap7", "sky130", ""):
        assert synthesis_settings(pdk) == []


def test_explicit_options_replace_canonical_default_aliases():
    from logikbench.tools.lhd.lhd import option_overrides
    base = ["synth", "--set", "pass.color.max_gate=30000", "--set", "abc.threads=1"]
    extra = ["--set", "color.max_gate=12000", "--set", "pass.abc.threads=2"]
    assert option_overrides(base, extra) == ["synth", *extra]
    assert base[2] == "pass.color.max_gate=30000"


def test_mapping_delay_uses_pdk_clock_and_cli_override():
    from logikbench.asic import _lhd_delay_ps
    assert _lhd_delay_ps("asap7", None) == "200"
    assert _lhd_delay_ps("sky130", None) == "500"
    assert _lhd_delay_ps("asap7", 0.4) == "400"
    assert _lhd_delay_ps("sky130", 20) == "20000"
    with pytest.raises(ValueError, match="positive finite"):
        _lhd_delay_ps("asap7", 0)


def test_merge_macro_physical_units(tmp_path):
    cells = tmp_path / "cells.lib"
    macro = tmp_path / "macro.lib"
    cells.write_text('''library(cells) {
      time_unit : "1ps"; capacitive_load_unit(1,ff);
      leakage_power_unit : "1pW";
      cell(BUF) { area : 2; pin(A) { direction : input; capacitance : 3; } }
    }''')
    macro.write_text('''library(macro) {
      time_unit : "1ns"; capacitive_load_unit(1,pf);
      leakage_power_unit : "1uW";
      lu_table_template(delay) {
        variable_1 : input_net_transition;
        variable_2 : total_output_net_capacitance;
        index_1("1,2"); index_2("3,4");
      }
      cell(RAM) {
        area : 8; cell_leakage_power : 2;
        pin(A) { direction : input; capacitance : 0.5; }
        pin(Q) { direction : output;
          timing() { related_pin : A;
            cell_rise(delay) { values("1,2", "3,4"); }
          }
          internal_power() {
            rise_power(delay) { values("5,6", "7,8"); }
          }
        }
      }
    }''')
    output = merge([cells, macro])
    assert 'time_unit : "1ps"' in output
    import re

    def values(pattern):
        block = re.search(pattern, output).group(1)
        return [float(value) for value in re.findall(r"[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?", block)]

    assert values(r'index_1\(([^)]*)\)') == pytest.approx([1000, 2000])
    assert values(r'index_2\(([^)]*)\)') == pytest.approx([3000, 4000])
    assert values(r'cell_rise\(delay\)\s*\{\s*values\(([^)]*)\)') == pytest.approx([1000, 2000, 3000, 4000])
    assert values(r'rise_power\(delay\)\s*\{\s*values\(([^)]*)\)') == pytest.approx([5e6, 6e6, 7e6, 8e6])
    assert 'cell_leakage_power : 2000000' in output
    capacitances = [float(value) for value in re.findall(r'\bcapacitance\s*:\s*([0-9.e+-]+)', output)]
    assert capacitances == pytest.approx([3, 500])
    assert 'area : 8' in output
