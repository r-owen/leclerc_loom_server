__all__ = ["MockLoom"]

import asyncio
from typing import Any

from base_loom_server.base_mock_loom import BaseMockLoom
from base_loom_server.utils import create_done_future

from leclerc_loom_server.utils import shaft_word_from_bytes


class MockLoom(BaseMockLoom):
    """Simulate a Leclerc dobby loom.

    The user controls this loom by:

    * Call command_reader.create_writer() to create a command writer.
    * Call reply_writer.create_reader() to create a reply reader.
    * Read replies from the reply reader.
    * Write commands to the command writer.
    """

    first_byte = b"\x1b"  # ASCII <ESC>: the first byte of each command and reply.
    terminator = b"\x0d"  # ASCII <CR>; the last byte of each command and reply.
    shed_close_duration = 0.5  # Duration for user to close the shed after opening it.
    max_timeout = 10  # Max time after "E" before the loom times out the shed.
    # 60 seconds is what real looms use, but shorten it for normal use,
    # and shorten it to almost nothing for unit tests where a timeout is wanted.

    def __post_init__(self) -> None:
        if self.num_shafts % 8 != 0:
            raise ValueError(f"num_shafts={self.num_shafts} must be a multiple of 8")

        self.desired_shaft_word: int = 0
        self.shed_closed: bool = True
        self.test_mode: bool = False
        self.timed_out: bool = False
        # Timeout for starting a weave cycle.
        self.timeout_task = create_done_future()
        # Delay for finishing a weave cycle after the shed is open.
        self.shed_close_task = create_done_future()

    async def basic_read(self) -> bytes:
        """Read one command to the loom.

        Perform no error checking, except that self.reader exists.
        """
        assert self.reader is not None  # make mypy happy
        return await self.reader.readuntil(self.terminator)

    async def close(self, **kwargs: Any) -> None:  # noqa: ANN401
        """Shut down the mock loom."""
        self.timeout_task.cancel()
        self.shed_close_task.cancel()
        await super().close(**kwargs)

    async def handle_read_bytes(self, read_bytes: bytes) -> None:
        """Handle one command from the web server."""
        if read_bytes[0:1] != self.first_byte:
            self.log.warning(f"MockLoom: ignoring command {read_bytes!r}; first byte not {self.first_byte!r}")
            return
        cmd = read_bytes[1:2]  # skip first_byte.
        data = read_bytes[2:-1]  # skip terminator.
        if self.test_mode:
            self.log.info("Leaving test mode")
        self.test_mode = False
        match cmd:
            case b"I":
                # Shafts for next pick
                self.desired_shaft_word = shaft_word_from_bytes(data)
            case b"E":
                # Energize the solenoids
                self.move_task = asyncio.create_task(self.move(self.desired_shaft_word))
            case b"R":
                # Release solenoids (if active) and reset self.timed_out.
                # If the shed is closing as part of a normal cycle, that continues.
                if not self.shed_closed:
                    self.log.warning("Solenoids released while shed not closed.")
                if self.solenoids_energized:
                    self.timeout_task.cancel()
                self.timed_out = False
            case b"Q":
                # Get status.
                await self.report_status()
            case b"T":
                # Enter test mode. Note: any command exits test mode.
                self.test_mode = True
                self.log.info("Entering test mode")
            case _:
                self.log.warning(f"Ignoring unrecognized command {cmd!r} in {read_bytes!r}")

    @property
    def solenoids_energized(self) -> bool:
        """Return True if the solenoids are energized (timeout timer running)."""
        return not self.timeout_task.done()

    @property
    def in_cycle(self) -> bool:
        """Are we in a weaving cycle?"""
        return not self.move_task.done()

    async def move(self, shaft_word: int) -> None:
        """Move shafts to the desired state. Triggered by the "E" command.

        This implementation entirely replaces that in the base class.
        """
        if self.solenoids_energized:
            self.log.error("Ignore invalid request to energize solenoids: already energized.")
            return
        if not self.shed_closed:
            self.log.error("Solenoids energized while shed not closed.")

        self.shaft_word = shaft_word
        self.timed_out = False
        try:
            self.shed_closed = False
            self.timeout_task = asyncio.create_task(asyncio.sleep(self.max_timeout))
            await self.timeout_task
            # In normal operation the timeout task is canceled by oob_command_n.
            # If that does not occur, fail the move by setting timed_out.
            self.timed_out = True
        except asyncio.CancelledError:
            # Normal operation; oob command n was received in time.
            pass

        if self.timed_out:
            await self.write_reply(b"T")
            return

        # Wait for the shed to close. If canceled, this happens early.
        try:
            self.shed_close_task = asyncio.create_task(asyncio.sleep(self.shed_close_duration))
            await self.shed_close_task
        except asyncio.CancelledError:
            # Acceptable but not normal.
            self.log.info(f"{self}: shed closed early")
        self.shed_closed = True
        await self.write_reply(b"C")

    async def oob_command_n(self, cmd: str) -> None:  # noqa: ARG002
        """If solenoids are engaged (timeout timer running), stop the timeout timer.

        Otherwise close the shed immediately (short-circuiting the normal close timer).
        """
        if self.solenoids_energized:
            # Normal cycle; simulate opening the shed by canceling
            # the timeout timer
            if self.verbose:
                self.log.info(f"{self}: oob next pick; open shed and start close shed timer")
            self.timeout_task.cancel()
        else:
            # Abort the cycle and make sure the shed is closed.
            if self.verbose:
                self.log.info(f"{self}: oob next pick; closing the shed early")
            self.shed_close_task.cancel()
            self.shed_closed = True

    async def report_direction(self) -> None:
        """Report direction (weave/unweave).

        Leclerc looms do not know anything about direction,
        so just log the request.
        """
        if self.verbose:
            self.log.info(f"{self}.direction_forward={self.direction_forward}")

    async def report_motion_state(self) -> None:
        """Report motion state."""
        if self.verbose:
            self.log.info(f"{self}.moving={self.in_cycle}")

    async def report_pick_wanted(self) -> None:
        """Report pick wanted."""
        if self.verbose:
            pick_wanted = self.shed_closed and not self.in_cycle
            self.log.info(f"{self}.pick_wanted={pick_wanted}")

    async def report_status(self) -> None:
        """Report status.

        Sx where x is a bitmask with bits:
        * 0 are the solenoids engaged?
        * 1: shed partially open (switch #1)? Always 0.
        * 2: shed fully closed (switch #2)? Equals self.shed_closed.
        * 3: did the last cycle time out? Equals self.timed_out.
        """
        if self.verbose:
            self.log.info(
                f"{self}.report_status: {self.in_cycle=}, {self.solenoids_energized=}, "
                f"{self.shed_closed=}, {self.timed_out=}"
            )
        status_word = 0
        status_word |= bool(self.solenoids_energized)
        status_word |= bool(self.shed_closed) << 2
        status_word |= bool(self.timed_out) << 3
        await self.write_reply(b"S" + status_word.to_bytes(length=1))

    async def write_reply(self, data: bytes) -> None:
        """Write a reply.

        This method adds self.first_byte and terminator.
        """
        await self.write(self.first_byte + data + self.terminator)

    async def report_shafts(self) -> None:
        """Report motion state.

        Leclerc looms do not report shafts, so just log if verbose.
        """
        if self.verbose:
            self.log.info(f"{self}.shaft_word={self.shaft_word}")
