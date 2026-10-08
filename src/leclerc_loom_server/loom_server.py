__all__ = ["LoomServer"]


from base_loom_server.base_loom_server import BaseLoomServer
from base_loom_server.enums import ShaftStateEnum

from leclerc_loom_server.utils import bytes_from_shaft_word

from .mock_loom import MockLoom


class LoomServer(BaseLoomServer):
    """Communicate with the client software and the loom.

    The preferred way to create and run a LoomServer is to call
    LoomServer.amain(...).

    Args:
        num_shafts: The number of shafts the loom has.
        serial_port: The name of the serial port, e.g. "/dev/tty0".
            If the name is "mock" then use a mock loom.
        reset_db: If True, delete the old database and create a new one.
        verbose: If True, log diagnostic information.
        db_path: Path to the pattern database. Specify None for the
            default path. Unit tests specify a non-None value, to avoid
            stomping on the real database.
    """

    default_name = "leclerc"
    loom_reports_motion = True
    loom_reports_direction = False
    mock_loom_type = MockLoom
    # Leclerc looms do not offer a physical direction control.
    # Indicate this with supports_full_direction_control=True
    # and loom_reports_direction=False.
    supports_full_direction_control = True

    async def get_initial_loom_state(self) -> None:
        """Get initial loom state."""
        await self.write_to_loom(MockLoom.first_byte + b"Q")

    async def write_shafts_to_loom(self, shaft_word: int) -> None:
        """Send a shaft_word to the loom."""
        shaft_bytes = bytes_from_shaft_word(shaft_word=shaft_word)
        await self.write_to_loom(MockLoom.first_byte + b"I" + shaft_bytes)
        self.shaft_word = shaft_word
        await self.write_to_loom(MockLoom.first_byte + b"E")
        self.shaft_state = ShaftStateEnum.MOVING

    async def handle_loom_reply(self, reply_bytes: bytes) -> None:
        """Process one reply_bytes from the loom."""
        # The replies from the loom are (ignoring the first and last bytes):
        # "?": ready. Sent only when the loom starts up.
        # "C": cycle complete. Send the next pick.
        # "T": timed out. Wait for the user to restart before sending a pick.
        # "Sx": status.
        # An unknown message that describes the firmware version.
        # Sent once when the loom starts up.
        if reply_bytes[0:1] != MockLoom.first_byte:
            message = f"Unexpected loom reply {reply_bytes!r} does not start with {MockLoom.first_byte!r}"
            self.log.warning(f"LoomServer: {message}")
        reply_code = reply_bytes[1:2]  # skip first_byte.
        reply_data = reply_bytes[2:-1]  # skip terminator.
        initial_shaft_state = self.shaft_state
        match reply_code:
            case b"?":
                # Ready for next pick.
                self.shaft_state = ShaftStateEnum.DONE
            case b"C":
                # Ready for next pick.
                self.shaft_state = ShaftStateEnum.DONE
            case b"T":
                self.shaft_state = ShaftStateEnum.AWAITING_RESUME
            case b"S":
                # reply_data is a single byte with these bits:
                # * 0: are solenoids engaged?
                # * 1: is switch 2 (shed open) engaged?
                # * 2: is switch 1 (shed closed) engaged?
                # * 3: did the last cycle time out?
                #
                # Ignore the time out bit, because we only ask for status after
                # connecting or reconnecting, at which point we assume the user
                # wants to weave.
                status_word = reply_data[0]
                is_ready = (status_word & 0b0001 == 0) and (status_word & 0b0100 > 0)
                self.shaft_state = ShaftStateEnum.DONE if is_ready else ShaftStateEnum.MOVING
            case _:
                self.log.warning(f"Ignoring unrecognized reply from loom: {reply_bytes!r}")
                return

        if self.shaft_state == initial_shaft_state:
            return
        await self.report_shaft_state()
        if self.shaft_state == ShaftStateEnum.DONE:
            await self.handle_next_pick_request()

    async def read_client_loop(self) -> None:
        """If connected to the loom, trigger weaving."""
        if self.loom_connected and self.shaft_state == ShaftStateEnum.DONE:
            await self.handle_next_pick_request()
        await super().read_client_loop()
