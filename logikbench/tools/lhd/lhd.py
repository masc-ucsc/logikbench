"""LiveHD synthesis task for LogikBench."""

import json
import fnmatch
import os
import shlex
import sys
from time import perf_counter
from pathlib import Path

from siliconcompiler import Task
from logikbench.tools.resources import ProcessRSS

from logikbench.tools.lhd.liberty import stage as stage_liberty
from logikbench.tools.lhd.depth import measure as measure_depth
from logikbench.tools.lhd.netlist import NativeStateError, normalize_for_opensta


_TOOLDIR = Path(__file__).resolve().parent
_SIBLING_LHD = _TOOLDIR.parents[3] / "livehd" / "bazel-bin" / "lhd" / "lhd"
_STDIO_WRAPPER = _TOOLDIR / "_merge_stdio.py"


def _lhd_executable():
    return os.environ.get("LHD") or (str(_SIBLING_LHD) if _SIBLING_LHD.is_file() else "lhd")


def synthesis_settings(pdk):
    """Use LiveHD defaults for both technologies; PDK constraints are separate."""
    return []


def option_overrides(base, extra):
    """Collapse explicit overrides before lhd rejects conflicting --set values."""
    def canonical(key):
        return "pass." + key if key.startswith(("color.", "abc.")) else key
    overridden = {canonical(extra[i + 1].split("=", 1)[0])
                  for i, token in enumerate(extra[:-1]) if token == "--set"}
    result = []
    i = 0
    while i < len(base):
        if base[i] == "--set" and i + 1 < len(base):
            if canonical(base[i + 1].split("=", 1)[0]) in overridden:
                i += 2
                continue
        result.append(base[i])
        i += 1
    return result + extra


class LHDTask(ProcessRSS, Task):
    """Tool-level definition for the LiveHD binary."""

    def tool(self):
        return "lhd"

    def parse_version(self, stdout):
        # `lhd --version` prints `lhd <version>`.
        return stdout.split()[1]

    def setup(self):
        super().setup()
        # LiveHD uses stderr for structured progress and informational output,
        # not only errors. Merge it into stdout before SiliconCompiler labels
        # every line LOGERROR; the regular log scrapers still identify actual
        # diagnostics.
        wrapper = str(_STDIO_WRAPPER)
        exe = _lhd_executable()
        self.set_exe(sys.executable,
                     vswitch=[wrapper, exe, "--version"])
        self.add_regex(
            "warnings", r'(^W [0-9]+|(^|:) warning:|"severity":"warning")')
        self.add_regex(
            "errors", r'(^E [0-9]+|(^|:) error:|"severity":"error")')


class Synthesis(LHDTask):
    """Run the one-shot LiveHD synthesis flow and emit mapped Verilog."""

    def __init__(self):
        super().__init__()
        self.add_parameter(
            "liberty", "[str]",
            "standard-cell Liberty file(s); split files are merged into the "
            "single library required by lhd synth", [])
        self.add_parameter("library_cache", "str", "shared absolute library cache root", "")
        self.add_parameter("pdk", "str", "LogikBench PDK token selecting synthesis defaults", "")
        self.add_parameter("delay_ps", "str", "ABC delay target in picoseconds from the PDK clock", "")
        self.add_parameter("macrolib", "[str]", "hard-macro Liberty declarations", [])
        self.add_parameter(
            "dontuse", "[str]",
            "Liberty cell-name patterns stamped `dont_use : true` in the staged "
            "mapping library (the PDK groups yosys passes as -dont_use)", [])
        self.add_parameter(
            "driver_cell", "str",
            "PDK driving cell for primary inputs, passed as abc.boundary_drive", "")
        self.add_parameter("blackbox", "[str]", "Verilog hard-macro declarations", [])
        self.add_parameter(
            "options", "str",
            "extra lhd synth arguments passed by lb syn --options", "")
        self.add_parameter(
            "opensta_netlist", "bool",
            "normalize wiring-only SystemVerilog constructs for OpenSTA", False)
        self.add_parameter(
            "lintonly", "bool",
            "compile and elaborate the design without technology mapping", False)

    def task(self):
        return "synthesis"

    def setup(self):
        super().setup()
        for lib, key in (self.get_fileset_file_keys("systemverilog")
                         + self.get_fileset_file_keys("verilog")):
            self.add_required_key(lib, *key)
        if self.get("var", "lintonly"):
            self.add_output_file("lg")
        else:
            self.add_output_file(ext="vg")
        if not self.get("var", "library_cache"):
            self.set("var", "library_cache", str(Path(self.project.get("option", "builddir")).resolve()))

    def pre_process(self):
        super().pre_process()
        proj = self.project
        fileset = proj.get("option", "fileset")[0]
        depalias = {}
        for dep, depfs, alib, afs in proj.option.get_alias():
            lib = proj.get("library", alib, field="schema")
            depalias[(dep, depfs)] = (lib, afs)
        proj.design.write_fileset("cmd.f", fileset=fileset,
                                  depalias=depalias)

        liberties = self.get("var", "liberty")
        if liberties:
            stage_liberty(liberties, self.get("var", "library_cache"),
                          dont_use=self.get("var", "dontuse"))
        macros = self.get("var", "macrolib")
        if macros:
            stage_liberty(macros, self.get("var", "library_cache"), "macros.lib")
            stage_liberty(liberties + macros, self.get("var", "library_cache"), "analysis.lib",
                          dont_use=self.get("var", "dontuse"))

    def runtime_options(self):
        opts = [str(_TOOLDIR / "_run_synthesis.py"), _lhd_executable()]
        opts += super().runtime_options()
        design = self.project.design
        fileset = self.project.get("option", "fileset")[0]
        top = design.get_topmodule(fileset)
        lintonly = self.get("var", "lintonly")

        opts += ["compile" if lintonly else "synth",
                 "--reader", "slang", "--top", top,
                 "--workdir", "lhd-work",
                 "--result-json", "result.json"]
        if lintonly:
            opts += ["--emit-dir", "lg:outputs/lg"]
        else:
            opts += ["--emit", f"verilog:outputs/{top}.vg"]
        if not lintonly:
            if self.get("var", "liberty"):
                library = "analysis.lib" if self.get("var", "macrolib") else "merged.lib"
                opts += ["--set", f"synth.liberty={library}"]
                driver = self.get("var", "driver_cell")
                # The same primary-input driver yosys's abc constraints name,
                # unless the PDK also lists it as dont_use (then LiveHD's
                # default, the library's smallest ordinary buffer, stands in).
                if driver and not any(fnmatch.fnmatchcase(driver, p)
                                      for p in self.get("var", "dontuse")):
                    opts += ["--set", f"abc.boundary_drive={driver}"]
            # Keep compiler optimization defaults identical across PDKs.
            opts += synthesis_settings(self.get("var", "pdk"))
            if self.get("var", "delay_ps"):
                opts += ["--set", "pass.abc.delay=" + self.get("var", "delay_ps")]
        if self.get("var", "macrolib") or self.get("var", "blackbox"):
            # These readers preserve declared opaque instances through LGraph.
            opts[opts.index("--reader") + 1] = "yosys"
            if self.get("var", "macrolib"):
                opts += ["--set", "compile.yosys.macrolib=macros.lib"]
            if self.get("var", "blackbox"):
                opts += ["--set", "compile.yosys.blackbox=" + "\x1f".join(self.get("var", "blackbox"))]
        options = self.get("var", "options")
        raw_options = []
        if options:
            extra = shlex.split(options)
            if "--" in extra:
                separator = extra.index("--")
                raw_options = extra[separator + 1:]
                extra = extra[:separator]
            opts = option_overrides(opts, extra)

        # The resolved SC fileset carries sources, include directories and
        # defines. Parameters are separate schema entries, so forward them as
        # Slang -G overrides after the raw-argument separator.
        opts += ["--", "-F", "cmd.f", "-DSYNTHESIS", *raw_options]
        if self.get("var", "blackbox"):
            # Yosys-Slang cannot inline bidirectional pad connections. Retain
            # module boundaries during elaboration; the LHD Yosys reader then
            # flattens the synthesizable hierarchy around the opaque pads.
            opts += ["--keep-hierarchy"]
        for key in design.getkeys("fileset", fileset, "param"):
            opts += ["-G", f"{key}={design.get_param(key, fileset)}"]
        return opts

    def post_process(self):
        try:
            self._post_process()
        except Exception as error:
            Path("reports").mkdir(exist_ok=True)
            Path("reports/adapter_error.json").write_text(json.dumps({
                "type": type(error).__name__, "message": str(error)}, indent=2) + "\n")
            raise

    def _post_process(self):
        super().post_process()
        resource_report = Path("reports/process_resources.json")
        if resource_report.is_file():
            highwater = json.loads(resource_report.read_text())["peak_child_rss_bytes"]
            sampled = self.schema_metric.get("memory", step="synthesis", index="0") or 0
            self.record_metric("memory", max(sampled, highwater),
                               source_file=str(resource_report), source_unit="B")
        for runtime in Path("outputs").glob("cgen_memory_*.v"):
            self.add_output_file(runtime.name)
        if self.get("var", "lintonly"):
            return
        result = {}
        if os.path.isfile("result.json"):
            with open("result.json") as stream:
                result = json.load(stream)
        total = ((result.get("qor") or {}).get("abc") or {}).get("total") or {}
        # Preserve completed mapping metrics even if structural conversion or
        # timing later fails. Depth analysis may refine the leaf cell count.
        for metric, key in (("cells", "gates"), ("cellarea", "area")):
            if total.get(key) is not None:
                value = int(total[key]) if metric == "cells" else float(total[key])
                self.record_metric(metric, value, source_file="result.json",
                                   source_unit="um^2" if metric == "cellarea" else None)
        top = self.project.design.get_topmodule(
            self.project.get("option", "fileset")[0])
        netlist = os.path.join("outputs", f"{top}.vg")
        analysis_lib = "analysis.lib" if self.get("var", "macrolib") else "merged.lib"
        blackboxes = self.get("var", "blackbox")
        phases = {}
        started = perf_counter()
        rtlil = None
        if self.get("var", "opensta_netlist") and os.path.isfile(netlist):
            try:
                candidate = Path("reports/normalized.il")
                normalize_for_opensta(netlist, analysis_lib, f"reports/{top}.normalize.log",
                                      blackboxes, rtlil=candidate)
                rtlil = candidate
            except NativeStateError as error:
                Path("reports").mkdir(exist_ok=True)
                Path("reports/timing_unavailable.json").write_text(
                    json.dumps({"fmax": None, "reason": str(error)}, indent=2) + "\n")
                original = Path(netlist).read_text()
                Path(netlist).write_text(
                    "// livehd_timing_unavailable: native state\n" + original)
                del original  # Do not retain a multi-GB netlist during depth analysis.

        phases["normalize_seconds"] = perf_counter() - started
        started = perf_counter()
        if not os.path.isfile("result.json"):
            return
        if os.path.isfile(netlist) and os.path.isfile("merged.lib"):
            depth = measure_depth(netlist, analysis_lib, top, blackboxes=blackboxes,
                                  mapping_liberty="merged.lib", rtlil=rtlil)
            if depth is not None:
                self.record_metric("logicdepth", depth,
                                   source_file="reports/logicdepth.json")
        phases["depth_seconds"] = perf_counter() - started
        Path("reports/adapter_time.json").write_text(json.dumps(phases, indent=2) + "\n")
        structural = Path("reports/logicdepth.json")
        structural_counts = json.loads(structural.read_text()) if structural.is_file() else {}
        if structural_counts.get("mapped_cells") is not None:
            self.record_metric("cells", structural_counts["mapped_cells"],
                               source_file=str(structural))
