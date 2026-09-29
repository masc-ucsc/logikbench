#!/usr/bin/env python3
"""Refresh local LHD results from retained manifests without rerunning tools."""

import argparse
from types import SimpleNamespace

from logikbench.apps.lb import (
    ALL_GROUPS, make_worklist, publish_target_task, save_target,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builddir', default='build')
    parser.add_argument('--publish', action='store_true')
    options = parser.parse_args()
    args = SimpleNamespace(builddir=options.builddir, group=ALL_GROUPS,
                           name=None, label=None)
    worklist = make_worklist(args)
    targets = ['lhd_asap7', 'lhd_sky130']
    for target in targets:
        save_target(target, args, worklist)
    if options.publish:
        publish_target_task('syn', targets, args)


if __name__ == '__main__':
    main()
