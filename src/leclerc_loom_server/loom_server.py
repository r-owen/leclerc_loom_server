__all__ = ["LoomServer"]

import asyncio
import enum
from typing import Any, TypeAlias

from base_loom_server.base_loom_server import BaseLoomServer
from base_loom_server.enums import ShaftStateEnum
from base_loom_server.utils import create_done_future

from leclerc_loom_server.utils import bytes_from_shaft_word

from .mock_loom import MockLoom

TriState: TypeAlias = bool | None


class StatusBits(enum.IntEnum):
    """Status bits."""

    ENERGIZED = 0b0001
    SHED_OPEN = 0b0010
    SHED_CLOSED = 0b0100
    TIMED_OUT = 0b1000


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
    loom_reports_motion = False
    loom_reports_direction = False
    mock_loom_type = MockLoom
    # Leclerc looms do not offer a physical direction control.
    # Indicate this with supports_full_direction_control=True
    # and loom_reports_direction=False.
    supports_full_direction_control = True
    # Interval between status requests when cancelling a shed (seconds)
    status_interval = 0.1

    def __post_init__(self) -> None:
        # Used to run wait_shed_closed.
        self.cancel_shed_task = create_done_future()
        self.ignore_replies_task = create_done_future()
        # Used to tell wait_shed_closed that the shed is closed.
        self.shed_closed_event = asyncio.Event()
        self.release_solenoids_when_safe = False

    async def close(self, **kwargs: Any) -> None:  # noqa: ANN401
        """Override close to also cancel local tasks."""
        self.ignore_replies_task.cancel()
        self.cancel_shed_task.cancel()
        await super().close(**kwargs)

    async def get_initial_loom_state(self) -> None:
        """Get initial loom state."""
        self.cancel_shed_task = asyncio.create_task(self.wait_shed_closed(aborting=False))

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
        if not self.ignore_replies_task.done():
            self.log.info(f"Ignoring reply {reply_bytes!r} to avoid race conditions")
        reply_code = reply_bytes[1:2]  # skip first_byte.
        reply_data = reply_bytes[2:-1]  # skip terminator.
        new_shaft_state: ShaftStateEnum = self.shaft_state  # type: ignore[has-type]
        shed_closed = False  # False means open or unknown
        match reply_code:
            case b"?":
                # System has started up; ignore this as we don't know if the shed is closed.
                pass
            case b"C":
                # Cycle complete. The shed is closed and we are ready for the next pick.
                shed_closed = True
                new_shaft_state = ShaftStateEnum.DONE
            case b"T":
                # Cycle timed out. Stop weaving.
                # We don't know if the shed is closed, so assume it is not.
                new_shaft_state = ShaftStateEnum.TIMED_OUT
            case b"S":
                # Status. Ignore the SHED_OPEN bit, since we don't have any use for it.
                status_word = reply_data[0]
                is_energized = status_word & StatusBits.ENERGIZED > 0
                shed_closed = status_word & StatusBits.SHED_CLOSED > 0
                is_timed_out = status_word & StatusBits.TIMED_OUT > 0
                if is_energized:
                    # Solenoids are energized. Assume we are weaving
                    # (abort is handled later).
                    new_shaft_state = (
                        ShaftStateEnum.WAITING_FOR_CLOSE
                        if self.release_solenoids_when_safe
                        else ShaftStateEnum.MOVING
                    )
                elif shed_closed:
                    # Solenoids are not energized and shed is closed.
                    new_shaft_state = ShaftStateEnum.TIMED_OUT if is_timed_out else ShaftStateEnum.DONE
                else:
                    # Solenoids are not energized, but the shed is open,
                    # so it is not safe to energize them.
                    new_shaft_state = ShaftStateEnum.WAITING_FOR_CLOSE
            case _:
                self.log.warning(f"Ignoring unrecognized reply from loom: {reply_bytes!r}")
                return

        if shed_closed:
            if not self.cancel_shed_task.done():
                self.ignore_replies_task = asyncio.create_task(asyncio.sleep(self.status_interval))
            self.shed_closed_event.set()
            if self.release_solenoids_when_safe:
                self.release_solenoids_when_safe = False
                await self.write_to_loom(b"R")

        if new_shaft_state != self.shaft_state:  # type: ignore[has-type]
            self.shaft_state = new_shaft_state
            await self.report_shaft_state()

        if self.shaft_state == ShaftStateEnum.DONE:
            await self.handle_next_pick_request()

    async def wait_shed_closed(self, *, aborting: bool) -> None:
        """Wait for the shed to close by polling status.

        This is used when connecting to the loom, and when changing patterns
        or modes, or jumping to a new pick or end.
        It is not needed for normal weaving, since the end of the normal cycle
        is reliably reported and handled by handle_loom_reply.

        Run using:

            self.cancel_shed_task = asyncio.create_task(
                self.wait_shed_closed(aborting=...)
            )

        so handle_loom_reply can reliably tell when this is running.
        """
        if aborting:
            self.release_solenoids_when_safe = True

        self.shed_closed_event.clear()
        while True:
            await self.write_to_loom(b"Q")
            try:
                async with asyncio.timeout(self.status_interval):
                    await self.shed_closed_event.wait()
                    break
            except TimeoutError:
                pass

    async def write_shafts_to_loom(self, shaft_word: int) -> None:
        """Send a shaft_word to the loom."""
        shaft_bytes = bytes_from_shaft_word(shaft_word=shaft_word)
        await self.write_to_loom(b"I" + shaft_bytes)
        self.shaft_word = shaft_word
        await self.write_to_loom(b"E")
        self.shaft_state = ShaftStateEnum.READY

    async def write_to_loom(self, data: bytes) -> None:
        """Send data to the loom.

        Override the base class method to prepend MockLoom.first_byte.

        Args:
            data: The data to send, without prefix or terminator.
                (This method adds the prefix and terminator).
        """
        await super().write_to_loom(MockLoom.first_byte + data)
