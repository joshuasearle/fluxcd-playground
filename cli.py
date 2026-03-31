#!/usr/bin/env python3

import argparse
import subprocess
from pathlib import Path

ROOT_PATH = Path(__file__).parent.parent
CLUSTER_NAME = "fluxcd-playground"


def main():
    parser = argparse.ArgumentParser()

    # Add `setup` and `delete` subcommands
    subparsers = parser.add_subparsers(dest="command")
    setup_parser = subparsers.add_parser("setup")
    delete_parser = subparsers.add_parser("delete")

    args = parser.parse_args()

    if args.command == "setup":
        subprocess.run(
            ["kind", "create", "cluster", "--name", CLUSTER_NAME], check=True
        )
        subprocess.run(["flux", "install"], check=True)
    elif args.command == "delete":
        subprocess.run(
            ["kind", "delete", "cluster", "--name", CLUSTER_NAME], check=True
        )


if __name__ == "__main__":
    main()
