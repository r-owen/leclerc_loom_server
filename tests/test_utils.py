import random

from leclerc_loom_server.utils import (
    NUM_BYTES,
    bytes_from_shaft_word,
    shaft_word_from_bytes,
)

rnd = random.Random(25)


def get_random_word(num_bits: int) -> int:
    return rnd.randrange(1, 1 << num_bits)


def get_random_byte() -> int:
    return get_random_word(num_bits=8)


def test_bytes_from_shaft_word() -> None:
    assert bytes_from_shaft_word(0) == b"\x00\x00\x00\x00"
    assert bytes_from_shaft_word(1) == b"\x01\x00\x00\x00"
    assert bytes_from_shaft_word(0x12345678) == b"\x78\x56\x34\x12"
    for _ in range(100):
        rand_byte = get_random_byte()
        for ind in range(NUM_BYTES):
            rand_word = rand_byte << ind * 8
            bytestr = bytes_from_shaft_word(rand_word)
            assert bytestr[ind] == rand_byte
            for ind2 in range(NUM_BYTES):
                if ind2 == ind:
                    continue
                assert bytestr[ind2] == 0

        assert shaft_word_from_bytes(bytes_from_shaft_word(rand_word)) == rand_word
