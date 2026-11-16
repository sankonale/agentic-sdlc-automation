# covers: FR-1, FR-5
import pytest

from app.codegen import ALPHABET, CodeGenerator, decode_base62, encode_base62, validate_alias


@pytest.mark.parametrize("n", [0, 1, 61, 62, 63, 3843, 3844, 10**12, 2**63 - 1])
def test_base62_round_trip(n):
    assert decode_base62(encode_base62(n)) == n


def test_encode_known_values():
    assert encode_base62(0) == "0"
    assert encode_base62(61) == "z"
    assert encode_base62(62) == "10"


def test_decode_rejects_invalid_chars():
    with pytest.raises(ValueError):
        decode_base62("ab$")


def test_generated_codes_have_fixed_length_and_alphabet():
    gen = CodeGenerator({{CODE_LENGTH}})
    for _ in range(500):
        code = gen.generate()
        assert len(code) == {{CODE_LENGTH}}
        assert set(code) <= set(ALPHABET)


def test_generated_codes_are_effectively_unique():
    gen = CodeGenerator({{CODE_LENGTH}})
    codes = {gen.generate() for _ in range(5000)}
    assert len(codes) >= 4995


@pytest.mark.parametrize("bad", ["ab", "has space", "x" * 33, "api", "HealthZ", "emoji🙂"])
def test_invalid_aliases_rejected(bad):
    with pytest.raises(ValueError):
        validate_alias(bad)


def test_valid_alias_accepted():
    validate_alias("spring-sale_2026")
