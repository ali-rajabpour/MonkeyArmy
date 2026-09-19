"""Command-line interface for the calc package."""

import argparse
import sys

from . import add, multiply


def main(argv=None):
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Calculator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Add two numbers")
    add_parser.add_argument("a", type=int, help="First number")
    add_parser.add_argument("b", type=int, help="Second number")

    mult_parser = subparsers.add_parser("multiply", help="Multiply two numbers")
    mult_parser.add_argument("a", type=int, help="First number")
    mult_parser.add_argument("b", type=int, help="Second number")

    args = parser.parse_args(argv)

    if args.command == "add":
        result = add(args.a, args.b)
    elif args.command == "multiply":
        result = multiply(args.a, args.b)

    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
