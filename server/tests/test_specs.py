import pytest

from server.specs import read_spec

DOC = """# Brief

intro text

## 1. calc/stats.py — statistics

- mean(xs)
- median(xs)

### detail

nested content

## 2. calc/vector.py — vectors

- dot(u, v)

## Acceptance

pytest passes
"""


@pytest.fixture
def brief(tmp_path):
    p = tmp_path / "brief.md"
    p.write_text(DOC)
    return str(p)


def test_whole_file(brief):
    assert read_spec(brief).startswith("# Brief")


def test_section_stops_at_next_sibling(brief):
    body = read_spec(brief, "calc/stats.py")
    assert "mean(xs)" in body
    assert "nested content" in body  # deeper heading stays inside the section
    assert "dot(u, v)" not in body
    assert "pytest passes" not in body


def test_last_section_runs_to_end(brief):
    assert read_spec(brief, "Acceptance").endswith("pytest passes")


def test_missing_file():
    with pytest.raises(ValueError, match="not found"):
        read_spec("/nonexistent/brief.md")


def test_missing_section_lists_headings(brief):
    with pytest.raises(ValueError, match="calc/convert.py"):
        read_spec(brief, "calc/convert.py")


def test_oversized_file(tmp_path):
    p = tmp_path / "big.md"
    p.write_text("x" * 70_000)
    with pytest.raises(ValueError, match="limit"):
        read_spec(str(p))
