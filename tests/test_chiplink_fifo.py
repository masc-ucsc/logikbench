"""Exercise full capacity, overflow blocking, ordering and pointer wrap."""

from pathlib import Path
import shutil
import subprocess

import pytest

import logikbench


@pytest.mark.eda
def test_chiplink_fifo_full_and_wrap(tmp_path):
    if not shutil.which('iverilog') or not shutil.which('vvp'):
        pytest.skip('iverilog/vvp required')
    source = (Path(logikbench.__file__).parent / 'benchmarks/blocks/chiplink/rtl'
              / 'chiplink_cdc_fifo.v')
    tb = tmp_path / 'tb.v'
    tb.write_text('''
module tb;
  reg wc=0, rc=0, rst=0, we=0, re=0;
  reg [7:0] wd=0;
  wire [7:0] rd;
  wire full, empty;
  integer round, i;
  always #5 wc=~wc;
  always #7 rc=~rc;
  chiplink_cdc_fifo #(.DW(8),.AW(3)) dut(
    .wr_clk(wc),.wr_rst_n(rst),.wr_en(we),.wr_data(wd),.wr_full(full),
    .rd_clk(rc),.rd_rst_n(rst),.rd_en(re),.rd_data(rd),.rd_empty(empty));
  initial begin
    #20; rst=1;
    for(round=0;round<3;round=round+1) begin
      repeat(4) @(negedge wc);
      for(i=0;i<8;i=i+1) begin
        if(full !== 0) $fatal(1,"full before capacity");
        wd=round*16+i; we=1;
        @(negedge wc);
      end
      if(full !== 1) $fatal(1,"full missing at capacity");
      wd=255; // attempted overflow must not overwrite the oldest entry
      repeat(2) @(negedge wc);
      we=0;
      repeat(4) @(negedge rc);
      for(i=0;i<8;i=i+1) begin
        if(empty !== 0 || rd !== round*16+i) $fatal(1,"FIFO ordering");
        re=1;
        @(negedge rc);
      end
      re=0;
      if(empty !== 1) $fatal(1,"empty missing after drain");
    end
    $display("PASSED"); $finish;
  end
  initial begin #10000; $fatal(1,"timeout"); end
endmodule
''')
    executable = tmp_path / 'fifo.vvp'
    subprocess.run(['iverilog', '-g2012', '-s', 'tb', '-o', str(executable),
                    str(source), str(tb)], check=True, capture_output=True, timeout=10)
    run = subprocess.run(['vvp', str(executable)], check=True,
                         capture_output=True, text=True, timeout=10)
    assert 'PASSED' in run.stdout
