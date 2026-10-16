# Overflow accounting of Kalico's load cell stack, driven end to end with a
# mocked MCU and reactor. Needs a Kalico checkout:
#
#   KALICO_PATH=/path/to/kalico python3 -m pytest tests/test_kalico_overflow_accounting.py
#
# These tests describe the *intended* behaviour: the "overflows"/"errors"
# fields of a sensor batch are counts new to that batch (Kalico PR #947,
# commit 17380399). Against a tree without that change the historical
# overflow tests fail, which is the bug under investigation.
import importlib
import os
import struct
import sys
import types
from collections import deque
from pathlib import Path

import pytest

KALICO_PATH = os.environ.get("KALICO_PATH")
pytestmark = pytest.mark.skipif(
    not KALICO_PATH, reason="set KALICO_PATH to a Kalico checkout")

SPS = 80
BATCH = 0.100
MCU_FREQ = 1000000.0
SAMPLES_PER_MSG = 12                       # 51 byte message / 4 byte sample


class CommandError(Exception):
    pass


class FakeReactor:
    NEVER = 9999999999999999.
    def __init__(self):
        self.now = 1000.
        self.timers = []
    def monotonic(self):
        return self.now
    def register_timer(self, cb, waketime):
        handle = [cb, waketime]
        self.timers.append(handle)
        return handle
    def unregister_timer(self, handle):
        if handle in self.timers:
            self.timers.remove(handle)
    def advance(self, seconds):
        end = self.now + seconds
        while True:
            due = [t for t in self.timers if t[1] <= end]
            if not due:
                break
            t = min(due, key=lambda t: t[1])
            self.now = max(self.now, t[1])
            t[1] = t[0](self.now)
            if t[1] >= self.NEVER and t in self.timers:
                self.timers.remove(t)
        self.now = end


class FakePrinter:
    command_error = CommandError
    def __init__(self, reactor):
        self.reactor = reactor
    def get_reactor(self):
        return self.reactor
    def is_shutdown(self):
        return False


class FakeMcu:
    # Simulates an MCU producing a fixed rate stream and a 16 bit
    # possible_overflows counter that is zeroed by sensor_bulk_reset().
    def __init__(self, reactor):
        self.reactor = reactor
        self.on_data = None
        self.sequence = 0
        self.produced = 0
        self.last_t = reactor.now
        self.possible_overflows = 0
    def get_name(self):
        return "fakemcu"
    def register_response(self, cb, name, oid):
        self.on_data = cb
    def lookup_query_command(self, fmt, resp_fmt, oid=None, cq=None):
        mcu = self
        class Query:
            def send(self, args):
                mcu._produce()
                return {"clock": int(mcu.reactor.now * MCU_FREQ),
                        "next_sequence": mcu.sequence & 0xFFFF,
                        "buffered": 0, "query_ticks": 0,
                        "possible_overflows": mcu.possible_overflows & 0xFFFF}
        return Query()
    def clock32_to_clock64(self, c):
        return c
    def clock_to_print_time(self, clock):
        return clock / MCU_FREQ
    def seconds_to_clock(self, s):
        return int(s * MCU_FREQ)
    def estimated_print_time(self, now):
        return now
    def _produce(self):
        total = int((self.reactor.now - self.last_t) * SPS) + self.produced
        due = total // SAMPLES_PER_MSG - self.sequence
        for _ in range(max(due, 0)):
            data = struct.pack("<%di" % SAMPLES_PER_MSG,
                               *range(SAMPLES_PER_MSG))
            self.on_data({"sequence": self.sequence & 0xFFFF, "data": data})
            self.sequence += 1
    def start(self):
        # query_ads1220 rest_ticks!=0 -> sensor_bulk_reset()
        self.sequence = 0
        self.produced = 0
        self.last_t = self.reactor.now
        self.possible_overflows = 0
    def overflow(self, count):
        self.possible_overflows += count


class FakeCmd:
    def __init__(self, mcu):
        self.mcu = mcu
    def send(self, args):
        if args[1]:
            self.mcu.start()
    def send_wait_ack(self, args):
        pass


@pytest.fixture(scope="module")
def kalico():
    root = Path(KALICO_PATH) / "klippy"
    if not (root / "extras" / "bulk_sensor.py").exists():
        pytest.skip("KALICO_PATH is not a Kalico checkout")
    saved = {k: v for k, v in sys.modules.items() if k.startswith("klippy")}
    def stub(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod
        return mod
    # Do not execute klippy/__init__ (imports the whole host) or any
    # package __init__ that registers components: map the packages onto
    # the real directories and load only the leaf modules under test.
    stub("klippy", __path__=[str(root)], Printer=object)
    stub("klippy.mcu", MCU=object)
    stub("klippy.configfile", ConfigWrapper=object)
    # type-only imports in newer Kalico trees
    stub("klippy.gcode", GCodeCommand=object, GCodeDispatch=object)
    stub("klippy.toolhead", ToolHead=object)
    stub("klippy.extras", __path__=[str(root / "extras")])
    stub("klippy.extras.bus")
    stub("klippy.extras.load_cell",
         __path__=[str(root / "extras" / "load_cell")])
    try:
        mods = types.SimpleNamespace(
            bulk=importlib.import_module("klippy.extras.bulk_sensor"),
            ads=importlib.import_module("klippy.extras.load_cell.ads1220"),
            lc=importlib.import_module("klippy.extras.load_cell.load_cell"))
        if "tare_counts_per_channel" in Path(mods.lc.__file__).read_text():
            # Rig() wires LoadCell internals directly; newer trees changed
            # them
            pytest.skip("LoadCell layout differs from the tree this harness "
                        "was written for (Kalico 3ec541c9 era)")
        yield mods
    finally:
        for k in [k for k in sys.modules if k.startswith("klippy")]:
            del sys.modules[k]
        sys.modules.update(saved)


class Rig:
    # A real ADS1220 driver object (config skipped) + LoadCell + always-on
    # clients, exactly as LoadCell._handle_ready wires them at klippy:ready.
    def __init__(self, k):
        self.reactor = FakeReactor()
        self.printer = FakePrinter(self.reactor)
        self.mcu = FakeMcu(self.reactor)
        s = object.__new__(k.ads.ADS1220)
        s.printer = self.printer
        s.name = "loadcell"
        s.mcu = self.mcu
        s.oid = 7
        s.sps = SPS
        s.last_error_count = 0
        s.consecutive_fails = 0
        s.reset_chip = lambda: None
        s.setup_chip = lambda: None
        s.query_ads1220_cmd = FakeCmd(self.mcu)
        s.ffreader = k.bulk.FixedFreqReader(self.mcu, SPS * BATCH * 2, "<i")
        s.ffreader.setup_query_command("query_status", 7, None)
        s.batch_bulk = k.bulk.BatchBulkHelper(
            self.printer, s._process_batch, s._start_measurements,
            s._finish_measurements, BATCH)
        self.sensor = s
        s.get_mcu = lambda: self.mcu
        s.get_samples_per_second = lambda: SPS
        s.add_client = s.batch_bulk.add_client
        lc = object.__new__(k.lc.LoadCell)
        lc.printer = self.printer
        lc.sensor = s
        lc.channel_count = 1
        lc.tare_counts = 0
        lc.reference_tare_counts = 0
        lc.counts_per_gram = 1.0
        lc.invert = 1.0
        lc._force_buffer = deque(maxlen=SPS // 2)
        lc.clients = k.lc.ApiClientHelper(self.printer)
        self.load_cell = lc
        s.add_client(lc._sensor_data_event)      # klippy:ready wiring
        lc.add_client(lc._track_force)
        self.batches = []
        lc.add_client(self._record)
    def _record(self, msg):
        self.batches.append((msg["errors"], msg["overflows"]))
        return True
    def run(self, seconds):
        self.reactor.advance(seconds)
    def window(self, seconds):
        # What LOAD_CELL_DIAGNOSTIC / probe tare see for a collection window
        collector = self.load_cell.get_collector()
        collector.start_collecting()
        self.run(seconds)
        return collector.stop_collecting()


@pytest.fixture
def rig(kalico):
    r = Rig(kalico)
    r.run(1.0)                       # always-on session is up and healthy
    assert r.sensor.batch_bulk.is_started
    return r


def test_clean_session_reports_no_errors(rig):
    samples, errors = rig.window(2.0)
    assert samples and errors == 0


def test_historical_overflow_does_not_poison_later_windows(rig):
    # A brief backlog under SYNC_MOTORS: a few overflows in one interval.
    rig.mcu.overflow(3)
    rig.run(2.0)
    # Later, nothing new overflows. Each probe tare / diagnostic window
    # must be clean; the 3 historical overflows already happened.
    samples, errors = rig.window(10.0)
    assert samples
    assert errors == 0, (
        "historical overflows leaked into a later window: "
        "(errors, overflows)=%r over %d batches" % (errors, len(rig.batches)))


def test_batches_after_the_burst_carry_zero_deltas(rig):
    rig.mcu.overflow(3)
    rig.run(0.5)
    rig.batches.clear()
    rig.run(3.0)
    assert rig.batches
    assert all(b == (0, 0) for b in rig.batches), rig.batches[:5]


def test_new_overflow_inside_window_is_still_reported_exactly(rig):
    rig.mcu.overflow(3)
    rig.run(1.0)
    collector = rig.load_cell.get_collector()
    collector.start_collecting()
    rig.run(1.0)
    rig.mcu.overflow(4)              # genuinely new loss inside the window
    rig.run(1.0)
    samples, errors = collector.stop_collecting()
    # Not suppressed, and not inflated by the earlier 3
    assert errors == (0, 4)


def test_overflow_counter_wraps_at_16_bits(rig):
    rig.mcu.possible_overflows = 0xFFFE
    rig.run(0.5)                     # picked up (session total now large)
    rig.batches.clear()
    rig.mcu.overflow(3)              # 0xFFFE -> 0x0001 on the wire
    rig.run(0.5)
    assert sum(o for _, o in rig.batches) == 3


def test_overflow_events_are_logged_sparsely(rig, caplog):
    # Needs kalico_patches/0002-*: logs the first overflow of a run and
    # each power of two of its total; nothing while the counter is idle.
    if not hasattr(rig.sensor.ffreader, "_note_overflows"):
        pytest.skip("diagnostic patch 0002 not applied")
    caplog.set_level("WARNING")
    rig.run(5.0)
    assert "possible_overflows" not in caplog.text
    for _ in range(5):               # totals 1..5 over five intervals
        rig.mcu.overflow(1)
        rig.run(0.3)
    lines = [r.getMessage() for r in caplog.records
             if "possible_overflows" in r.getMessage()]
    assert len(lines) == 3           # run totals 1, 2 and 4
    assert "+1 (run total 1, mcu counter 1)" in lines[0]
    rig.run(10.0)                    # idle again: stays quiet
    assert len([r for r in caplog.records
                if "possible_overflows" in r.getMessage()]) == 3
