"""Tests for the calc package."""

import pytest

from calc import add, multiply
from calc.cli import main


def test_add():
    """Test addition."""
    assert add(1, 2) == 3
    assert add(-1, 1) == 0
    assert add(0, 0) == 0


def test_multiply():
    """Test multiplication."""
    assert multiply(2, 3) == 6
    assert multiply(-1, 5) == -5
    assert multiply(0, 100) == 0


def test_cli_add(capsys):
    """Test CLI add command."""
    main(["add", "1", "2"])
    captured = capsys.readouterr()
    assert captured.out.strip() == "3"


def test_cli_multiply(capsys):
    """Test CLI multiply command."""
    main(["multiply", "5", "4"])
    captured = capsys.readouterr()
    assert captured.out.strip() == "20"
