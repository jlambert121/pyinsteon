"""ALDB Read Manager."""
import asyncio
import logging
# from . import ALDB
from .aldb_record import ALDBRecord


RETRIES_ALL_MAX = 5
RETRIES_ONE_MAX = 20
RETRIES_WRITE_MAX = 5
TIMER = 10
TIMER_INCREMENT = 3
_LOGGER = logging.getLogger(__name__)
READ_ALL = 1
READ_ONE = 2
WRITE = 3


class ALDBReadManager():
    """ALDB Read Manager."""

    def __init__(self, aldb): # : ALDB):
        """Init the ALDBReadManager class."""
        from ..handlers.read_aldb import ReadALDBCommandHandler, ReceiveALDBRecordHandler
        self._aldb = aldb

        self._retries_all = 0
        self._retries_one = 0
        self._retries_write = 0
        self._last_command = None
        self._last_mem_addr = 0
        self._read_handler = ReadALDBCommandHandler(self._aldb.address)
        self._record_handler = ReceiveALDBRecordHandler(self._aldb.address)
        self._read_handler.subscribe(self._receive_record)
        self._record_handler.subscribe(self._receive_record)
        self._load_lock = asyncio.Lock()

    def read(self, mem_addr: int = 0x00, num_recs: int = 0):
        """Read one or more ALDB records.

        Parameters:

            mem_addr: int (Default 0x0000) - Memory address of the record to retrieve. 
            When mem_addr is 0x0000 the device will return the first record.

            num_recs: int (Default 0)  Number of database records to return. When num_recs is 0 and
            mem_addr is 0x0000 the database will return all records.
        """
        asyncio.ensure_future(self.async_read(mem_addr=mem_addr, num_recs=num_recs))

    async def async_read(self, mem_addr: int = 0x00, num_recs: int = 0):
        """Read one or more ALDB records asyncronously.

        Parameters:

            mem_addr: int (Default 0x0000) - Memory address of the record to retrieve. 
            When mem_addr is 0x0000 the device will return the first record.

            num_recs: int (Default 0)  Number of database records to return. When num_recs is 0 and
            mem_addr is 0x0000 the database will return all records.
        """
        if mem_addr == 0x0000 and num_recs == 0:
            self._last_command = READ_ALL
            retries = self._retries_all
        else:
            self._last_command = READ_ONE
            retries = self._retries_one
        await self._load_lock.acquire()
        await self._async_read(mem_addr=mem_addr, num_recs=num_recs)
        await self._load_lock.acquire()
        return True

    async def _async_read(self, mem_addr: int = 0x00, num_recs: int = 0):
        """Perform the device read function."""
        if (self._last_command == READ_ALL and
                self._retries_all < RETRIES_ALL_MAX):
            retries = self._retries_all
        else:
            retries = self._retries_one
        _LOGGER.debug('Attempting to read %x', mem_addr)
        await self._read_handler.async_send(mem_addr=mem_addr, num_recs=num_recs)
        timer = TIMER + retries * TIMER_INCREMENT
        asyncio.ensure_future(self._timer(timer, mem_addr, num_recs))

    def _receive_record(self, is_response: bool, record: ALDBRecord):
        """Receive an ALDB record."""
        num_recs = len(self._aldb)
        self._aldb[record.mem_addr] = record
        if num_recs != len(self._aldb):
            _LOGGER.info('Received %d records', len(self._aldb))

    async def _timer(self, timer, mem_addr, num_recs):
        """Set a timer to confirm if the last get command completed."""
        await asyncio.sleep(timer)
        if self._last_command == READ_ALL:
            self._manage_get_all_cmd(mem_addr, num_recs)
        else:
            self._manage_get_one_cmd(mem_addr, num_recs)

    def _manage_get_all_cmd(self, mem_addr, num_recs):
        """Manage the READ_ALL command process."""
        _LOGGER.debug('In _manage_get_all_cmd')
        if self._aldb.calc_load_status():
            # The ALDB is fully loaded so stop
            self._load_lock.release()
            return
        if self._retries_all < RETRIES_ALL_MAX:
            # Attempt to read all records again
            asyncio.ensure_future(self._async_read(0x0000, 0))
            self._retries_all += 1
            _LOGGER.info('Retry reading all records %d times', self._retries_all)
        else:
            # Read the next missing record
            next_mem_addr = self._next_missing_record()
            if next_mem_addr is None:
                _LOGGER.error('Tried to get mem address after HWM')
                return
            if next_mem_addr == self._last_mem_addr:
                # We are still trying to get the same record as the last read
                if self._retries_one < RETRIES_ONE_MAX:
                    asyncio.ensure_future(self._async_read(next_mem_addr, 1))
                    self._retries_one += 1
                    _LOGGER.info('Retry reading 0x%04x record %d times',
                                 next_mem_addr, self._retries_one)
                else:
                    # Tried to read the same record max times so quit
                    self._load_lock.release()
            else:
                # Reading a different record than the last attempt so reset
                # the retry count
                self._last_mem_addr = next_mem_addr
                self._retries_one = 0
                asyncio.ensure_future(self._async_read(next_mem_addr, num_recs))

    def _manage_get_one_cmd(self, mem_addr, num_recs):
        """Manage the READ_ONE command process."""
        if self._aldb.get(mem_addr):
            self._load_lock.release()
        elif self._retries_one < RETRIES_ONE_MAX:
            asyncio.ensure_future(self._async_read(mem_addr=mem_addr, num_recs=num_recs))
            self._retries_one += 1
            _LOGGER.info('Retry reading 0x%04x record %d times',
                         mem_addr, self._retries_one)
        else:
            # Trigger aldb.loaded but this will check the load status.
            self._load_lock.release()

    def _next_missing_record(self):
        last_addr = 0
        if not self._has_first_record():
            if (self._last_mem_addr == 0x0000 and
                    self._retries_one >= RETRIES_ONE_MAX):
                return self._aldb.first_mem_addr
            return 0x0000
        for mem_addr in self._aldb:
            rec = self._aldb[mem_addr]
            if rec.control_flags.is_high_water_mark:
                return None
            if last_addr != 0:
                if not last_addr - 8 == mem_addr:
                    return last_addr - 8
            last_addr = mem_addr
        return last_addr - 8

    def _has_first_record(self):
        """Test if the first record is loaded."""
        for mem_addr in self._aldb:
            if mem_addr == self._aldb.first_mem_addr or mem_addr == 0x0fff:
                _LOGGER.debug('Found First record: 0x%04x', mem_addr)
                return True
        return False