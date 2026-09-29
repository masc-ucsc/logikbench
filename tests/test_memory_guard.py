import json
import os
import sys

import psutil

from logikbench.memory_guard import run
from logikbench.tools.resources import process_footprint


def test_physical_footprint_includes_resident_pages():
    process = psutil.Process(os.getpid())
    assert process_footprint(process) >= process.memory_info().rss * 0.9


def test_guard_kills_allocating_child_tree(tmp_path):
    pidfile = tmp_path / 'child.pid'
    child_code = ("import os,time; from pathlib import Path; "
                  f"Path({str(pidfile)!r}).write_text(str(os.getpid())); "
                  "data=bytearray(100_000_000); time.sleep(20)")
    parent_code = ("import subprocess,sys; "
                   f"subprocess.run([sys.executable,'-c',{child_code!r}])")
    report = tmp_path / 'guard.json'
    assert run([sys.executable, '-c', parent_code], 70_000_000, 0, report) == 137
    data = json.loads(report.read_text())
    assert data['reason'] == 'command memory limit exceeded'
    assert data['peak_tree_bytes'] > 70_000_000
    child = int(pidfile.read_text())
    try:
        process = psutil.Process(child)
        assert process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        pass


def test_guard_preserves_command_exit(tmp_path):
    assert run([sys.executable, '-c', 'raise SystemExit(7)'],
               500_000_000, 0, tmp_path / 'guard.json') == 7


def test_timeout_kills_child_tree(tmp_path):
    pidfile = tmp_path / 'child.pid'
    child_code = ("import os,time; from pathlib import Path; "
                  f"Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(20)")
    parent_code = ("import subprocess,sys; "
                   f"subprocess.run([sys.executable,'-c',{child_code!r}])")
    report = tmp_path / 'timeout.json'
    assert run([sys.executable, '-c', parent_code], 500_000_000, 0,
               report, timeout=1) == 124
    assert json.loads(report.read_text())['reason'] == 'command timeout exceeded'
    try:
        child = psutil.Process(int(pidfile.read_text()))
        assert child.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        pass


def test_soft_target_records_without_killing(tmp_path):
    report = tmp_path / 'soft.json'
    assert run([sys.executable, '-c', 'import time; time.sleep(0.3)'],
               500_000_000, 0, report, soft_bytes=1) == 0
    assert json.loads(report.read_text())['soft_target_exceeded']
