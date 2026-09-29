import pytest
from logikbench.tools.lhd.depth import cell_depth


def test_analysis_batches_modules_without_changing_bytes(tmp_path):
    from logikbench.tools.lhd.depth import analysis_sources
    source = tmp_path / 'structural.v'
    text = (b'/* header\nendmodule\n*/\nmodule a(input x, output y);\n'
            b'assign y=x;\nendmodule\n\nmodule b(input x, output y);\n'
            b'a child(x,y);\nendmodule\n// trailing comment\n')
    source.write_bytes(text)
    parts = analysis_sources(source, threshold=0)
    assert b''.join(p.read_bytes() for p in parts) == text
    assert parts[0].read_bytes().endswith(b'assign y=x;\nendmodule\n')
    assert parts[1].read_bytes().endswith(b'a child(x,y);\nendmodule\n')


def test_analysis_keeps_preprocessor_state_in_one_read(tmp_path):
    from logikbench.tools.lhd.depth import analysis_sources
    source = tmp_path / 'structural.v'
    source.write_text('`define VALUE 1\nmodule a;\nendmodule\n'
                      'module b; wire x = `VALUE;\nendmodule\n')
    assert analysis_sources(source, threshold=0) == [source]


def test_depth_with_batched_module_reads(tmp_path, monkeypatch):
    import json
    import shutil
    from logikbench.tools.lhd import depth
    if not shutil.which('yosys'):
        pytest.skip('Yosys required')
    source = tmp_path / 'net.v'
    source.write_text('module child(input a, output y);\n'
                      'BUF b(.A(a),.Y(y));\nendmodule\n'
                      'module top(input a, output y);\n'
                      'wire x; child c(a,x); BUF b(.A(x),.Y(y));\nendmodule\n')
    library = tmp_path / 'cells.lib'
    library.write_text('library(test) { cell(BUF) { area: 1; pin(A) { direction: input; } '
                       'pin(Y) { direction: output; function: "A"; } } }')
    original = depth.analysis_sources
    monkeypatch.setattr(depth, 'analysis_sources', lambda p: original(p, threshold=0))
    assert depth.measure(source, library, 'top', tmp_path / 'reports') == 2
    report = json.loads((tmp_path / 'reports/logicdepth.json').read_text())
    assert report['mapped_cells'] == 2


def gate(kind, a, y):
    return {'type': kind, 'port_directions': {'A': 'input', 'Y': 'output'},
            'connections': {'A': a, 'Y': y}}


def test_depth_crosses_cell_chain_and_cuts_state():
    cells = {'a': gate('G', [2], [3]), 'b': gate('G', [3], [4]),
             'ff': gate('F', [4], [5]), 'c': gate('G', [5], [6])}
    graph = {'modules': {'top': {'cells': cells}}}
    assert cell_depth(graph, 'top', {'F'}, {'G', 'F'}) == 2


def test_parallel_pins_count_one_dependency():
    cells = {'a': gate('G', [2], [3, 4]), 'b': gate('G', [3, 4], [5])}
    assert cell_depth({'modules': {'top': {'cells': cells}}}, 'top', set(), {'G'}) == 2


def test_cycle_has_no_fabricated_depth():
    graph = {'modules': {'top': {'cells': {'a': gate('G', [3], [4]), 'b': gate('G', [4], [3])}}}}
    with pytest.raises(ValueError, match='cycle'):
        cell_depth(graph, 'top', set(), {'G'})


def test_unknown_cell_has_no_fabricated_depth():
    graph = {'modules': {'top': {'cells': {'a': gate('unknown', [2], [3])}}}}
    with pytest.raises(ValueError, match='unknown cell'):
        cell_depth(graph, 'top', set(), {'G'})


@pytest.mark.parametrize('endpoint', ['enable', 'data', 'output', 'mapped'])
def test_native_control_cone_is_cut_without_hiding_datapath(endpoint):
    cells = {'a': gate('G', [2], [3]), 'b': gate('G', [3], [4]),
             'inv': gate('$not', [4], [5]), 'reduce': gate('$reduce_and', [5], [6]),
             'ff': {'type': '$dffe',
                    'port_directions': {'D': 'input', 'EN': 'input', 'Q': 'output'},
                    'connections': {'D': [2], 'EN': [6], 'Q': [7]}},
             'tail': gate('G', [7], [8])}
    module = {'cells': cells, 'ports': {'y': {'direction': 'output', 'bits': [8]}}}
    if endpoint == 'data':
        cells['ff']['connections'].update(D=[6], EN=[2])
    elif endpoint == 'output':
        module['ports']['control'] = {'direction': 'output', 'bits': [5]}
    elif endpoint == 'mapped':
        cells['extra'] = gate('G', [5], [9])
    graph = {'modules': {'top': module}}
    if endpoint == 'enable':
        assert cell_depth(graph, 'top', set(), {'G'}) == 2
    else:
        with pytest.raises(ValueError, match='unmapped or unknown cell'):
            cell_depth(graph, 'top', set(), {'G'})


def test_depth_resolves_includes_beside_netlist_with_spaces(tmp_path):
    import shutil
    from logikbench.tools.lhd.depth import measure
    if not shutil.which("yosys"):
        pytest.skip("Yosys is required for structural depth measurement")
    path = tmp_path / "netlist directory"
    path.mkdir()
    (path / "defs.vh").write_text('`define EXPR a\n')
    (path / "net.v").write_text(
        '`include "defs.vh"\nmodule top(input a, output y); '
        'BUF b(.A(`EXPR),.Y(y)); endmodule\n')
    (path / "cells.lib").write_text(
        'library(test) { cell(BUF) { area: 1; pin(A) { direction: input; } '
        'pin(Y) { direction: output; function: "A"; } } }')
    assert measure(path / "net.v", path / "cells.lib", "top", path / "reports") == 1


def test_depth_cuts_native_enabled_register(tmp_path):
    import shutil
    from logikbench.tools.lhd.depth import measure
    if not shutil.which("yosys"):
        pytest.skip("Yosys is required for structural depth measurement")
    netlist = tmp_path / "net.v"
    netlist.write_text(
        'module top(input clk,rst,en,a, output reg q, output y); '
        'always @(posedge clk or posedge rst) '
        'if(rst) q<=0; else if(en) q<=a; '
        'BUF b(.A(q),.Y(y)); endmodule\n')
    library = tmp_path / "cells.lib"
    library.write_text(
        'library(test) { cell(BUF) { area: 1; pin(A) { direction: input; } '
        'pin(Y) { direction: output; function: "A"; } } }')
    assert measure(netlist, library, "top", tmp_path / "reports") == 1
    import json
    report = json.loads((tmp_path / "reports" / "logicdepth.json").read_text())
    assert report["mapped_cells"] == 1
    assert report["native_cells"] == 1


def test_depth_folds_disjoint_bit_assembly(tmp_path):
    import shutil
    from logikbench.tools.lhd.depth import measure
    if not shutil.which('yosys'):
        pytest.skip('Yosys required')
    net = tmp_path / 'net.v'
    net.write_text('module top(input a,b, output [3:0] y); wire x,z; '
                   'BUF u(.A(a),.Y(x)); BUF v(.A(b),.Y(z)); '
                   'assign y = ({x,3\'b0} | {2\'b0,z,1\'b0}); endmodule')
    lib = tmp_path / 'cells.lib'
    lib.write_text('library(test) { cell(BUF) { area: 1; pin(A) { direction: input; } '
                   'pin(Y) { direction: output; function: "A"; } } }')
    assert measure(net, lib, 'top', tmp_path / 'reports') == 1


def test_depth_folds_unused_sign_lane_and_dontcare_state(tmp_path):
    import shutil
    from logikbench.tools.lhd.depth import measure
    if not shutil.which('yosys'):
        pytest.skip('Yosys required')
    net = tmp_path / 'net.v'
    net.write_text('module top(input clk,en,a,b, output reg [62:0] q); wire x; '
                   'BUF u(.A(a),.Y(x)); '
                   'wire [63:0] value = en ? {b,{63{x}}} : {a,63\'bx}; '
                   'always @(posedge clk) q <= value[62:0]; endmodule')
    lib = tmp_path / 'cells.lib'
    lib.write_text('library(test) { cell(BUF) { area: 1; pin(A) { direction: input; } '
                   'pin(Y) { direction: output; function: "A"; } } }')
    assert measure(net, lib, 'top', tmp_path / 'reports') == 1


def test_hierarchical_depth_tracks_each_instance_arrival_and_internal_state():
    from logikbench.tools.lhd.depth import hierarchical_depth
    tile = {'ports': {'a': {'direction': 'input', 'bits': [2]},
                      'y': {'direction': 'output', 'bits': [4]}},
            'cells': {'a': gate('G', [2], [3]), 'b': gate('G', [3], [4]),
                      'ff': gate('FF', [4], [5]), 'tail': gate('G', [5], [6])}}
    top = {'ports': {'a': {'direction': 'input', 'bits': [2]}},
           'cells': {'first': {'type': 'tile',
                               'port_directions': {'a': 'input', 'y': 'output'},
                               'connections': {'a': [2], 'y': [3]}},
                     'second': {'type': 'tile',
                                'port_directions': {'a': 'input', 'y': 'output'},
                                'connections': {'a': [3], 'y': [4]}}}}
    depth, counts = hierarchical_depth(
        {'modules': {'top': top, 'tile': tile}}, 'top', {'FF'}, {'G', 'FF'})
    assert depth == 4
    assert counts == [8, 0, 0]


@pytest.mark.parametrize('feedback', [False, True])
def test_hierarchical_depth_uses_output_cones_for_cycles(feedback):
    from logikbench.tools.lhd.depth import hierarchical_depth
    tile = {'ports': {'d': {'direction': 'input', 'bits': [2]},
                      'r': {'direction': 'input', 'bits': [3]},
                      'q': {'direction': 'output', 'bits': [4]},
                      's': {'direction': 'output', 'bits': [5]}},
            'cells': {'data': gate('G', [2], [4]), 'ready': gate('G', [3], [5])}}
    top = {'ports': {'a': {'direction': 'input', 'bits': [2]}}, 'cells': {
        'a': {'type': 'tile',
              'port_directions': {'d': 'input', 'r': 'input', 'q': 'output', 's': 'output'},
              'connections': {'d': [6 if feedback else 2], 'r': [6], 'q': [3], 's': [4]}},
        'b': {'type': 'tile',
              'port_directions': {'d': 'input', 'r': 'input', 'q': 'output', 's': 'output'},
              'connections': {'d': [3], 'r': [3 if feedback else 2], 'q': [5], 's': [6]}}}}
    design = {'modules': {'top': top, 'tile': tile}}
    if feedback:
        with pytest.raises(ValueError, match='combinational cycle'):
            hierarchical_depth(design, 'top', set(), {'G'})
    else:
        assert hierarchical_depth(design, 'top', set(), {'G'}) == (2, [4, 0, 0])
