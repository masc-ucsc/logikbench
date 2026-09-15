"""ASIC synthesis flow (`lb syn`, ASIC targets).

SiliconCompiler Flowgraph containing a standard-cell mapping node and,
normally, an OpenSTA timing node. The 'synthesis' node maps the design to a
liberty library; the '--tool' selection picks which mapper runs it (see
_SYNTH). With a PDK target, every mapper feeds its gate-level Verilog to the
same SC OpenSTA TimingTask.

Adding a mapper (including a proprietary tool such as Design Compiler): add its
SC Task subclass under logikbench/tools/<tool>/ following the tools/tardigrade
pattern, then add one entry to _SYNTH. See logikbench/tools/README.md.
"""

from pathlib import Path

from siliconcompiler import Flowgraph, TaskSkip
from siliconcompiler.tools.opensta.timing import TimingTask

from logikbench.tools.yosys.yosys import Synthesis as YosysSynthesis
from logikbench.tools.tardigrade.tardigrade import Synthesis as TardigradeSynthesis
from logikbench.tools.lhd.lhd import Synthesis as LHDSynthesis


class SynthesisTiming(TimingTask):
    """Share one STA worker per benchmark across all synthesis mappers."""

    def task(self):
        return "synthesis_timing"

    def setup(self):
        super().setup()
        # lb already schedules independent benchmarks in parallel. OpenSTA's
        # all-core default also contends on its parasitic lookup mutex on
        # large pre-PNR netlists, even though no extracted parasitics exist.
        self.set_threads(1, clobber=True)


class LHDTiming(SynthesisTiming):
    """Keep synthesis results when native state has no structural STA model."""

    def task(self):
        return "lhd_timing"

    def pre_process(self):
        netlist = Path(f"inputs/{self.design_topmodule}.vg")
        if netlist.is_file():
            with netlist.open() as stream:
                if stream.readline().startswith("// livehd_timing_unavailable: native state"):
                    raise TaskSkip("LHD retains native state; Fmax is unavailable")
        super().pre_process()

    def post_process(self):
        super().post_process()
        # Preserve the whole-design structural definitions across timing:
        # standard-cell leaf count and combinational depth with state cuts.
        for metric in ("cells", "logicdepth"):
            value = self.schema_metric.get(metric, step="synthesis", index="0")
            if value is not None:
                self.set("report", metric, [])
                self.record_metric(metric, value,
                                   source_file="../../synthesis/0/reports/logicdepth.json")


class ASICSynthesis(Flowgraph):
    """Standard-cell mapping, optionally followed by SC OpenSTA timing."""

    # synthesis mapper by --tool name -> its SC Task class
    _SYNTH = {
        "yosys": YosysSynthesis,
        "tardigrade": TardigradeSynthesis,
        "lhd": LHDSynthesis,
    }

    def __init__(self, tool="yosys", name="asic_synth", timing=True):
        super().__init__()
        self.set_name(name)
        self.node("synthesis", self._SYNTH[tool]())
        if timing:
            self.node("timing", LHDTiming() if tool == "lhd" else SynthesisTiming())
            self.edge("synthesis", "timing")
