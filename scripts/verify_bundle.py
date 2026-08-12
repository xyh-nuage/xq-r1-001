#!/usr/bin/env python3
import argparse
from pathlib import Path

from sealed_runner import verify_manifest


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("root")
    args = p.parse_args()
    verify_manifest(Path(args.root).resolve())


if __name__ == "__main__":
    main()
