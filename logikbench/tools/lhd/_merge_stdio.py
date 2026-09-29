"""Run LiveHD with its informational stderr merged into stdout."""

import os
import sys


def main():
    if len(sys.argv) < 2:
        sys.exit("missing LiveHD executable")
    os.dup2(sys.stdout.fileno(), sys.stderr.fileno())
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
