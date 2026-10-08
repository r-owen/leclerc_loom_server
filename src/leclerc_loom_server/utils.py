NUM_BYTES = 4


def bytes_from_shaft_word(shaft_word: int) -> bytes:
    """Convert a shaft word to the bytes format needed by LeClerc.

    Args:
        shaft_word: Shaft word as a bit mask (bit N high if shaft N should rise).

    Returns:
        bytes string of length 4 in the format used by LeClerc:
        the first byte (left-most) is for shafts 1-8, then 9-16, etc.
        Within each byte, the LSB is for the lowest-numbered shaft.
    """
    return shaft_word.to_bytes(length=NUM_BYTES, byteorder="little")


def shaft_word_from_bytes(data_bytes: bytes) -> int:
    """Convert a bytes in the format needed by LeClerc to a shaft word.

    Args:
        data_bytes: Shaft bytes string in LeClerc format.

    See bytes_from_shaft_word for the data format.
    """
    return int.from_bytes(data_bytes, byteorder="little")
