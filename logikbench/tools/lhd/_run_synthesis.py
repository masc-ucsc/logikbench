"""Run synthesis, retaining a diagnosed unsupported-reader retry."""

import json
import os
import re
import resource
import shutil
from pathlib import Path
import subprocess
import sys


def retry_command(command, result):
    """Only the default reader may retry a frontend unsupported diagnostic."""
    readers = [i for i, value in enumerate(command[:-1]) if value == "--reader"]
    if len(readers) != 1 or command[readers[0] + 1] != "slang":
        return None
    if (result.get("error") or {}).get("class") != "unsupported":
        return None
    recipe = result.get("recipe") or []
    if not recipe or not recipe[0].startswith("inou.slang "):
        return None
    if any(step.startswith("pass.abc") for step in recipe):
        return None
    retry = list(command)
    retry[readers[0] + 1] = "yosys"
    if "--workdir" in retry:
        index = retry.index("--workdir") + 1
        retry[index] += "-yosys"
    return retry


def synthesis_reader_flags(command):
    """Match synthesis semantics for mapping and compile-only benchmark checks."""
    readers = [i for i, value in enumerate(command[:-1]) if value == "--reader"]
    if (len(command) < 2 or command[1] not in ("synth", "compile") or not readers
            or command[readers[-1] + 1] not in ("yosys", "yosys-slang")):
        return command
    command = list(command)
    if "--" not in command:
        command.append("--")
    for flag in ("--ignore-assertions", "--relax-enum-conversions"):
        if flag not in command:
            command.append(flag)
    return command


def stage_runtime(command):
    """Keep generated memory includes next to the emitted netlist."""
    executable = Path(shutil.which(command[0]) or command[0]).resolve()
    roots = [executable.parent, executable.parent.parent,
             executable.parent.parent.parent, Path.cwd()]
    roots += [p / workspace for p in executable.parent.glob("*.runfiles")
              for workspace in ("_main", "livehd", "livehd+")]
    if os.environ.get("RUNFILES_DIR"):
        roots = [Path(os.environ["RUNFILES_DIR"]) / workspace
                 for workspace in ("_main", "livehd", "livehd+")] + roots
    for index, option in enumerate(command[:-1]):
        if option != "--emit" or not command[index + 1].startswith("verilog:"):
            continue
        netlist = Path(command[index + 1].split(":", 1)[1])
        if not netlist.is_file():
            continue
        pending = [netlist]
        seen = set()
        while pending:
            source = pending.pop()
            for name in re.findall(r'`include\s+"(cgen_memory_[^"/]+\.v)"', source.read_text()):
                if name in seen:
                    continue
                seen.add(name)
                target = netlist.parent / name
                original = next((root / "ware" / "rtl" / name for root in roots
                                 if (root / "ware" / "rtl" / name).is_file()), None)
                if original is not None:
                    # A reused output directory must receive the runtime from
                    # the compiler used for this invocation, including fixes.
                    if original.resolve() != target.resolve():
                        shutil.copyfile(original, target)
                elif not target.exists():
                    raise FileNotFoundError(f"LHD runtime include {name} missing beside {executable}")
                pending.append(target)


def main():
    command = sys.argv[1:]
    reports = Path("reports")
    reports.mkdir(exist_ok=True)
    result_path = Path(command[command.index("--result-json") + 1])
    attempts = []

    def run(argv):
        argv = synthesis_reader_flags(argv)
        readers = [i for i, value in enumerate(argv[:-1]) if value == "--reader"]
        reader = argv[readers[-1] + 1] if readers else "default"
        reader = "".join(c if c.isalnum() else "_" for c in reader)
        log = reports / f"reader_{reader}.log"
        # A timeout must not leave the previous reader's failure masquerading
        # as the current attempt's result. Persist argv before starting it.
        result_path.unlink(missing_ok=True)
        attempt = {"argv": argv, "exit_code": None, "log": str(log)}
        attempts.append(attempt)
        record = reports / "reader_attempts.json"
        record.write_text(json.dumps(attempts, indent=2) + "\n")
        with log.open("w") as stream:
            rc = subprocess.call(argv, stdout=stream, stderr=subprocess.STDOUT)
        # A short-lived compiler can exit between SC's 0.5 s samples. The OS
        # retains its high-water RSS after wait(), including reader retries.
        highwater = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        rss_bytes = int(highwater if sys.platform == "darwin" else highwater * 1024)
        (reports / "process_resources.json").write_text(json.dumps({
            "peak_child_rss_bytes": rss_bytes,
            "method": "OS child-process RSS high-water mark"}, indent=2) + "\n")
        attempt["exit_code"] = rc
        record.write_text(json.dumps(attempts, indent=2) + "\n")
        return rc, log

    rc, log = run(command)
    try:
        result = json.loads(result_path.read_text())
    except (OSError, ValueError):
        result = {}
    retry = retry_command(command, result) if rc else None
    if retry is not None:
        (reports / "reader_slang.json").write_text(json.dumps(result, indent=2) + "\n")
        print("LHD: Slang frontend unsupported; retrying with Yosys-Slang "
              "(original diagnostic: reports/reader_slang.log)", flush=True)
        rc, log = run(retry)
    if rc == 0:
        stage_runtime(command)
    with log.open(errors="replace") as stream:
        shutil.copyfileobj(stream, sys.stdout)
    return rc


if __name__ == "__main__":
    sys.exit(main())
