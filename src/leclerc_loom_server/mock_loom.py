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
        self.moving = False
        self.shed_closed: bool = True
        self.test_mode: bool = False
        self.timed_out: bool = False
        self.timeout_task = create_done_future()

    async def basic_read(self) -> bytes:
        """Read one command to the loom.

        Perform no error checking, except that self.reader exists.
        """
        assert self.reader is not None  # make mypy happy
        return await self.reader.readuntil(self.terminator)

    async def close(self, **kwargs: Any) -> None:  # noqa: ANN401
        """Shut down the mock loom."""
        self.timeout_task.cancel()
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
                if self.moving:  # type: ignore[has-type]
                    self.log.warning(f"MockLoom: ignoring command: {read_bytes!r}; moving")
                    return
                self.move_task = asyncio.create_task(self.move(self.desired_shaft_word))
            case b"R":
                # Release solenoids.
                # A no-op if not moving.
                # If moving, cancel motion and reset self.moving and self.timed_out,
                # but leave self.shed_closed alone.
                if self.moving:  # type: ignore[has-type]
                    return
                self.move_task.cancel()
                self.moving = False
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

    async def move(self, shaft_word: int) -> None:
        """Move shafts to the desired state.

        Triggered by the "E" command.
        """
        if self.moving:  # type: ignore[has-type]
            self.log.warning("Request to move ignored; already moving.")
            return

        self.moving = True
        self.pick_wanted = False
        self.shed_closed = False
        self.timed_out = False

        try:
            self.timeout_task = asyncio.create_task(asyncio.sleep(self.max_timeout))
            await self.timeout_task
            self.timed_out = True
        except asyncio.CancelledError:
            # Normal operation; oob command n was received in time.
            pass

        if self.timed_out:
            await self.write_reply(b"T")
            return

        self.shaft_word = shaft_word
        self.shed_closed = False
        await asyncio.sleep(self.shed_close_duration)
        self.shed_closed = True
        self.moving = False
        self.pick_wanted = True
        await self.write_reply(b"C")

    async def oob_command_n(self, cmd: str) -> None:  # noqa: ARG002
        """Open the shed, if moving.

        If the solenoids are engaged, interrupt the timeout timer.
        Otherwise a no-op.
        """
        if self.verbose:
            if self.timeout_task.done():
                self.log.info(f"{self}: oob next pick; ignored because not moving")
            else:
                self.log.info(f"{self}: oob next pick; start closing shed")
        self.timeout_task.cancel()

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
            self.log.info(f"{self}.moving={self.moving}")

    async def report_pick_wanted(self) -> None:
        """Report pick wanted."""
        if self.verbose:
            self.log.info(f"{self}.pick_wanted={self.pick_wanted}")

    async def report_status(self) -> None:
        """Report status.

        Sx where x is a bitmask with bits:
        * 0 are the solenoids engaged. Equals self.moving.
        * 1: shed partially open (switch #1)? Always 0.
        * 2: shed fully closed (switch #2)? Equals self.shed_closed.
        * 3: did the last cycle time out? Equals self.timed_out.
        """
        if self.verbose:
            self.log.info(f"{self}.report_status: {self.moving=}, {self.shed_closed=}, {self.timed_out=}")
        status_word = 0
        status_word |= bool(self.moving)
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
