"""Real C++ digit separators must not hide a following function."""
import pytest

from anila_core.ingestion.source_code import source_units


@pytest.mark.parametrize('literal', ["1'000", "0xCA'FE", "0b1010'0011", "3.141'592", "1'000.25e+2"])
def test_cpp_numeric_separators_keep_both_function_boundaries(literal):
    source = f"auto first() {{ return {literal}; }}\nauto second() {{ return {literal}; }}\n"
    units = source_units(source, 'cpp')
    functions = [unit for unit in units if unit['symbol'] != 'module']
    assert [(unit['symbol'], unit['line_start'], unit['line_end']) for unit in functions] == [
        ('first', 1, 1), ('second', 2, 2),
    ]
    assert all(unit['source_boundary_quality'] == 'lexical' for unit in functions)
    assert [line for unit in units for line in range(unit['line_start'], unit['line_end'] + 1)] == [1, 2]


def test_cpp_char_literal_still_hides_literal_braces():
    source = "char first() { return '}'; }\nchar second() { return '{'; }\n"
    assert [unit['symbol'] for unit in source_units(source, 'cpp')] == ['first', 'second']
