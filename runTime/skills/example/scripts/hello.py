#!/usr/bin/env python3

import argparse


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--name", default="world")
    args = p.parse_args()
    print(f"hello {args.name}")


if __name__ == "__main__":
    main()

