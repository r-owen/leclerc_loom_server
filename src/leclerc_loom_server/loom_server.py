__all__ = ["LoomServer"]

import asyncio
import enum
from types import SimpleNamespace
from typing import Any, TypeAlias

from base_loom_server.base_loom_server import BaseLoomServer
from base_loom_server.enums import RunStateEnum, ShaftStateEnum
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
    # Supports start and stop commands
    supports_start_stop = True
    # Interval between status requests when cancelling a shed (seconds)
    status_interval = 0.1

    def __post_init__(self) -> None:
        self.ignore_replies_task = create_done_future()
        # Used to tell wait_shed_closed that the shed is closed.
        self.shed_closed_event = asyncio.Event()
        self.wait_shed_closed_task = create_done_future()
        # Release the solenoids when the shed closes?
        # Set true if changing the sequence while running
        # (e.g. a new pattern, new mode, jumping to a pick...).
        self.release_on_close = False
        # Set true during a weave cycle.
        self.in_cycle = False

    async def close(self, **kwargs: Any) -> None:  # noqa: ANN401
        """Override close to also cancel local tasks."""
        self.ignore_replies_task.cancel()
        self.wait_shed_closed_task.cancel()
        await super().close(**kwargs)

    async def cmd_start(
        self,
        command: SimpleNamespace,
    ) -> None:
        """Handle the start command, which has no parameters."""
        await super().cmd_start(command=command)
        if self.run_state != RunStateEnum.RUNNING and self.wait_shed_closed_task.done():  #  type: ignore[has-type]
            self.run_state = RunStateEnum.STARTING
            await self.report_run_state()
            self.wait_shed_closed_task = asyncio.create_task(self.wait_shed_closed())

    async def cmd_stop(
        self,
        command: SimpleNamespace,
    ) -> None:
        """Handle the stop command, which has no parameters."""
        await super().cmd_stop(command=command)
        self.in_cycle = False
        if self.run_state != RunStateEnum.STOPPED and self.wait_shed_closed_task.done():
            self.run_state = RunStateEnum.STOPPING
            await self.report_run_state()
            self.wait_shed_closed_task = asyncio.create_task(self.wait_shed_closed())

    async def get_initial_loom_state(self) -> None:
        """Get initial loom state."""
        if self.wait_shed_closed_task.done():
            self.wait_shed_closed_task = asyncio.create_task(self.wait_shed_closed())

    async def handle_change(
        self,
        command: SimpleNamespace,  # noqa: ARG002
    ) -> None:
        """Handle a discontiguous change to the weaving or threading.

        This is any of: a new pattern, a different mode, or jumping
        to a new pick or end.
        """
        self.release_on_close = True
        await self.restore_previous_state()
        if self.wait_shed_closed_task.done():
            self.wait_shed_closed_task = asyncio.create_task(self.wait_shed_closed())

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
        initial_run_state = self.run_state
        cycle_done = False
        match reply_code:
            case b"?":
                # System has started up; ignore this as we don't know if the shed is closed.
                pass
            case b"C":
                # Cycle complete. The shed is closed and we are ready for the next pick.
                # Clear saveed state, as we don't need or want to undo.
                shed_closed = True
                cycle_done = True
                new_shaft_state = ShaftStateEnum.RUNNING if self.in_cycle else ShaftStateEnum.CLOSED
                self.previous_state = None
                self.in_cycle = False
            case b"T":
                # Cycle timed out. Stop weaving and restore previous state.
                # We don't know if the shed is closed, so assume it is not.
                new_shaft_state = ShaftStateEnum.TIMED_OUT
                if self.run_state != RunStateEnum.STOPPED:
                    self.run_state = RunStateEnum.STOPPING
                self.in_cycle = False
                await self.restore_previous_state()
            case b"S":
                # Status. Ignore the ENERGIZED and SHED_OPEN bits,
                # since we don't have any use for them.
                status_word = reply_data[0]
                shed_closed = status_word & StatusBits.SHED_CLOSED > 0
                is_timed_out = status_word & StatusBits.TIMED_OUT > 0
                if self.in_cycle:
                    new_shaft_state = ShaftStateEnum.TIMED_OUT if is_timed_out else ShaftStateEnum.RUNNING
                else:
                    new_shaft_state = (
                        ShaftStateEnum.CLOSED if shed_closed else ShaftStateEnum.WAITING_FOR_CLOSE
                    )
                if is_timed_out and shed_closed:
                    cycle_done = True
            case _:
                self.log.warning(f"Ignoring unrecognized reply from loom: {reply_bytes!r}")
                return

        if shed_closed:
            if not self.wait_shed_closed_task.done():
                # Still polling for status, so start the ignore_replies_task
                # to handle potential race conditions.
                self.ignore_replies_task = asyncio.create_task(asyncio.sleep(self.status_interval))
            self.shed_closed_event.set()

            # Release the solenoids and abort the cycle, if wanted.
            if self.release_on_close or self.run_state in {RunStateEnum.STOPPING, RunStateEnum.STOPPED}:
                self.in_cycle = False
                await self.write_to_loom(b"R")
                if self.run_state == RunStateEnum.STOPPING:
                    self.run_state = RunStateEnum.STOPPED
                self.release_on_close = False

            # Send the next pick, if wanted.
            if self.in_cycle and not cycle_done:
                # Ignore this shed_closed because it's not end-of-cycle:
                # the weaver has yet to open the shed.
                pass
            elif self.run_state in {RunStateEnum.STARTING, RunStateEnum.RUNNING}:
                await self.handle_next_pick_request()
                if self.run_state == RunStateEnum.STARTING:
                    self.run_state = RunStateEnum.RUNNING

        if new_shaft_state != self.shaft_state:  # type: ignore[has-type]
            self.shaft_state = new_shaft_state
            await self.report_shaft_state()

        if self.run_state != initial_run_state:
            await self.report_run_state()
            if self.run_state == RunStateEnum.STOPPING and self.wait_shed_closed_task.done():
                self.wait_shed_closed_task = asyncio.create_task(self.wait_shed_closed())

    async def wait_shed_closed(self) -> None:
        """Wait for the shed to close by polling status.

        This is used when connecting to the loom, and when changing patterns
        or modes, or jumping to a new pick or end.
        It is not needed for normal weaving, since the end of the normal cycle
        is reliably reported and handled by handle_loom_reply.

        Run using:


            if not self.wait_shed_closed_task.done():
                self.wait_shed_closed_task = asyncio.create_task(
                    self.wait_shed_closed(aborting=...)
                )

        Using wait_shed_closed_task means handle_loom_reply can reliably
        tell when this is running.
        """
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
        """Send a shaft_word to the loom and activate solenoids.

        Warning: the shed must be closed.
        """
        shaft_bytes = bytes_from_shaft_word(shaft_word=shaft_word)
        await self.write_to_loom(b"I" + shaft_bytes)
        self.shaft_word = shaft_word
        await self.write_to_loom(b"E")
        self.in_cycle = True
        if self.shaft_state != ShaftStateEnum.RUNNING:
            self.shaft_state = ShaftStateEnum.RUNNING
            await self.report_shaft_state()

    async def write_to_loom(self, data: bytes) -> None:
        """Send data to the loom.

        Override the base class method to prepend MockLoom.first_byte.

        Args:
            data: The data to send, without prefix or terminator.
                (This method adds the prefix and terminator).
        """
        await super().write_to_loom(MockLoom.first_byte + data)
