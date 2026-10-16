# Acquisition lifecycle of motors_sync against mocked hardware: a fake
# toolhead/steppers/accelerometer behind a bulk helper that unregisters a
# client only when its callback returns False for a delivered batch (the
# real BatchBulkHelper behaviour; with KALICO_PATH set the real class is
# used as well).
#
# The tests assert observable behaviour (registered clients, running
# stream, collected samples, raised error) so they can also be pointed at
# an older revision: MOTORS_SYNC_PATH=/path/to/old/motors_sync.py
import logging
import multiprocessing
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

from conftest import CommandError, FakeReactor


class FakeToolhead:
    def __init__(self):
        self.wait_calls = 0
        self.wait_error = None
        self.dwell_error = None
    def wait_moves(self):
        self.wait_calls += 1
        if self.wait_error is not None:
            raise self.wait_error
    def get_last_move_time(self):
        return 0.
    def dwell(self, delay):
        if self.dwell_error is not None:
            raise self.dwell_error
    def flush_step_generation(self):
        pass


class FakePrinter:
    command_error = CommandError
    config_error = CommandError
    def __init__(self, reactor):
        self.reactor = reactor
        self.objects = {}
    def get_reactor(self):
        return self.reactor
    def get_state_message(self):
        return "", "ready"
    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)
    def register_event_handler(self, name, cb):
        pass
    def is_shutdown(self):
        return False


class FakeChip:
    # An accelerometer streaming continuously once its bulk helper starts
    data_rate = 1600
    def __init__(self, printer, bulk_cls):
        self.starts = self.stops = 0
        self.rows = 0
        self.overflows = 0
        self.overflows_after_start = 0
        self.mcu = types.SimpleNamespace(get_name=lambda: "indxmcu")
        self.ffreader = types.SimpleNamespace(
            get_last_overflows=lambda: self.overflows)
        self.batch_bulk = bulk_cls(printer, self._batch, self._start,
                                   self._stop, 0.1)
    def _start(self):
        self.starts += 1
        self.overflows = self.overflows_after_start    # note_start()
    def _stop(self):
        self.stops += 1
    def _batch(self, eventtime):
        self.rows += 1
        return {"data": [(float(self.rows), 0., 0., 9.8)],
                "errors": 0, "overflows": self.overflows}


class FakeFan:
    def __init__(self):
        self.toggles = []
        self.error = None
    def toggle(self, state):
        self.toggles.append(state)
        if self.error is not None:
            raise self.error


class Rig:
    def __init__(self, ms, bulk_cls, axes=("x", "y")):
        self.ms = ms
        self.reactor = FakeReactor()
        self.printer = FakePrinter(self.reactor)
        self.toolhead = FakeToolhead()
        self.chip = FakeChip(self.printer, bulk_cls)
        self.gcode = types.SimpleNamespace(
            error=CommandError, respond_info=lambda *a, **k: None)
        self.printer.objects.update({"toolhead": self.toolhead,
                                     "gcode": self.gcode,
                                     "lis2dw indx": self.chip})
        self.stepper_log = []
        self.snapshots = []
        self.msg = MagicMock()
        self.axes = [self._axis(n) for n in axes]
        kin = object.__new__(ms.BaseKinematics)
        kin.gcode = self.gcode
        kin.printer = self.printer
        kin.msg_helper = self.msg
        kin.motion_axes = {a.name: a for a in self.axes}
        kin.home_rails = lambda axes: None
        kin.apply_axes_phase_offsets = lambda axes: None
        self.kin = kin
    def _axis(self, name):
        ms = self.ms
        ax = object.__new__(ms.MotionAxis)
        ax.name = name
        ax.name_prefixed = name.upper()
        ax.printer = self.printer
        ax.gcode = self.gcode
        ax.msg_helper = self.msg
        ax.stepper_move = None
        ax.toolhead = self.toolhead
        ax.fan = FakeFan()
        ax.stats_helper = MagicMock()
        ax.retry_tolerance = 0.
        ax.is_finished = False
        ax.motion_log = []
        ax.phase_offset = None
        ax.toggle_self_steppers = (
            lambda m, n=name: self.stepper_log.append((n, "self", m)))
        ax.toggle_conflict_steppers = (
            lambda m, n=name: self.stepper_log.append((n, "conflict", m)))
        ax.query_phases_offset = lambda: 0
        ax.chip_helper = ms.AccelHelper(ax, "lis2dw indx",
                                        cfp=["median", 3])
        ax.measure_deviation = lambda a=ax: self.measure(a)
        ax.set_move_dir(0)
        return ax
    def measure(self, ax):
        # One measurement window: the chip delivers one batch
        self.reactor.advance(0.1)
        self.snapshots.append((ax.name, list(ax.chip_helper.samples)))
        return 5.0
    def converge(self, axes):
        for ax in axes:
            ax.on_finish()
    def sync(self, axes_sync=None, force_run=False):
        self.kin.axes_sync = axes_sync or self.converge
        self.kin.start_sync(self.axes, force_run)
    def settle(self, seconds=0.5):
        self.reactor.advance(seconds)
    def assert_quiescent(self):
        # nothing registered, stream stopped, every session ended
        bulk = self.chip.batch_bulk
        assert bulk.client_cbs == []
        assert not bulk.is_started
        assert self.chip.starts == self.chip.stops
        assert all(a.chip_helper.is_finished for a in self.axes)
    def assert_state_restored(self):
        assert all(a.fan.toggles[-1] is True for a in self.axes)
        for a in self.axes:
            assert (a.name, "self", 1) in self.stepper_log


@pytest.fixture
def rig(ms, bulk_cls):
    return Rig(ms, bulk_cls)


def helper_rig(ms, bulk_cls):
    r = Rig(ms, bulk_cls, axes=("x",))
    return r, r.axes[0].chip_helper


# -- sync outcomes -----------------------------------------------------

def test_normal_completion_leaves_nothing_running(rig):
    rig.sync()
    rig.settle()
    rig.assert_quiescent()
    rig.assert_state_restored()
    assert rig.chip.starts == 1


def test_already_in_tolerance_early_return_leaves_nothing_running(rig):
    for ax in rig.axes:
        ax.retry_tolerance = 10.
    def never(axes):
        raise AssertionError("axes_sync must be skipped")
    rig.sync(never)
    rig.settle()
    rig.msg.in_tolerance_msg.assert_called_once()
    rig.assert_quiescent()
    rig.assert_state_restored()


def test_exception_after_acquisition_started_cleans_up(rig):
    def boom(axes):
        raise RuntimeError("boom")
    with pytest.raises(CommandError, match="^boom$"):
        rig.sync(boom)
    rig.settle()
    rig.assert_quiescent()
    rig.assert_state_restored()


def test_base_exception_cleans_up_too(rig):
    def killed(axes):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        rig.sync(killed)
    rig.settle()
    rig.assert_quiescent()
    rig.assert_state_restored()


def test_original_exception_survives_failing_cleanup(rig):
    def boom(axes):
        rig.toolhead.wait_error = RuntimeError("wait failed")
        rig.axes[0].fan.error = RuntimeError("fan failed")
        raise RuntimeError("boom")
    with pytest.raises(CommandError, match="^boom$"):
        rig.sync(boom)
    rig.settle()
    # acquisition ended and the second axis was still restored although
    # the first axis' cleanup failed twice
    rig.assert_quiescent()
    assert ("y", "self", 1) in rig.stepper_log
    assert rig.axes[1].fan.toggles[-1] is True


def test_failure_before_any_acquisition_started(rig):
    def home_fails(axes):
        raise RuntimeError("homing failed")
    rig.kin.home_rails = home_fails
    with pytest.raises(CommandError, match="homing failed"):
        rig.sync()
    rig.settle()
    rig.assert_quiescent()
    assert rig.chip.starts == 0


# -- repeated runs / stale callbacks ----------------------------------

def test_repeated_runs_stop_the_stream_every_time(rig):
    for run in range(1, 4):
        rig.sync()
        rig.settle(1.0)
        rig.assert_quiescent()
        assert rig.chip.starts == run


def test_back_to_back_runs_do_not_duplicate_samples(rig):
    # The second run starts before the bulk helper delivered the batch
    # that unregisters the first run's clients.
    rig.sync()
    assert rig.chip.batch_bulk.client_cbs        # stale clients still there
    rig.snapshots.clear()
    rig.sync()
    assert rig.snapshots
    for name, rows in rig.snapshots:
        assert len(rows) == len(set(rows)), \
            f"{name}: samples delivered more than once: {rows}"
    rig.settle()
    rig.assert_quiescent()


def test_flush_data_in_a_session_does_not_rearm_a_finished_one(ms, bulk_cls):
    r, h = helper_rig(ms, bulk_cls)
    h.start_measurements()
    h.finish_measurements()
    h.flush_data()                      # as measure_deviation() does
    r.settle(0.3)
    assert h.is_finished
    assert h.samples == []
    r.assert_quiescent()


def test_late_callback_of_previous_session_cannot_feed_the_next(ms, bulk_cls):
    r, h = helper_rig(ms, bulk_cls)
    h.start_measurements()
    first = h.session
    h.finish_measurements()
    h.start_measurements()              # before the first one was dropped
    second = h.session
    r.reactor.advance(0.1)              # one batch reaches both callbacks
    assert first is not second
    assert [row[0] for row in h.samples] == [1.0]
    assert first.late_batches == 1 and first.removed
    assert second.active and second.batches == 1 and second.samples == 1
    h.finish_measurements()
    r.settle()
    r.assert_quiescent()


def test_samples_after_finish_are_dropped(ms, bulk_cls):
    r, h = helper_rig(ms, bulk_cls)
    h.start_measurements()
    r.reactor.advance(0.1)
    h.finish_measurements()
    kept = list(h.samples)
    r.settle()
    assert h.samples == kept
    r.assert_quiescent()


# -- idempotent cleanup -------------------------------------------------

def test_finish_is_idempotent(ms, bulk_cls):
    r, h = helper_rig(ms, bulk_cls)
    h.start_measurements()
    for _ in range(3):
        h.finish_measurements()
    assert r.toolhead.wait_calls == 1
    r.settle()
    r.assert_quiescent()


def test_finish_without_start_is_a_noop(ms, bulk_cls):
    r, h = helper_rig(ms, bulk_cls)
    h.finish_measurements()
    assert r.toolhead.wait_calls == 0
    assert h.is_finished


def test_session_ends_even_if_waiting_for_moves_fails(ms, bulk_cls):
    r, h = helper_rig(ms, bulk_cls)
    h.start_measurements()
    r.toolhead.wait_error = RuntimeError("wait failed")
    with pytest.raises(RuntimeError, match="wait failed"):
        h.finish_measurements()
    assert h.is_finished
    r.toolhead.wait_error = None
    r.settle()
    r.assert_quiescent()


def test_on_error_twice_is_harmless(rig):
    for ax in rig.axes:
        ax.on_start()
    for _ in range(2):
        for ax in rig.axes:
            ax.on_error()
    rig.settle()
    rig.assert_quiescent()
    rig.assert_state_restored()


def test_failed_client_registration_leaves_no_active_session(ms, bulk_cls):
    r, h = helper_rig(ms, bulk_cls)
    def broken(cb):
        raise CommandError("chip start failed")
    r.chip.batch_bulk.add_client = broken
    with pytest.raises(CommandError):
        h.start_measurements()
    assert h.is_finished


def test_no_threads_or_processes_remain(rig):
    threads = threading.active_count()
    rig.sync()
    with pytest.raises(CommandError):
        rig.sync(lambda axes: (_ for _ in ()).throw(RuntimeError("x")))
    rig.settle()
    assert threading.active_count() == threads
    assert multiprocessing.active_children() == []


# -- temporary stepper state -------------------------------------------

class FakeStepper:
    def __init__(self):
        self.calls = []
        self.sk = "toolhead-sk"
        self.trapq = "toolhead-trapq"
    def set_stepper_kinematics(self, sk):
        prev, self.sk = self.sk, sk
        self.calls.append(("sk", sk))
        return prev
    def set_trapq(self, tq):
        prev, self.trapq = self.trapq, tq
        self.calls.append(("trapq", tq))
        return prev
    def set_position(self, pos):
        pass
    def generate_steps(self, t):
        pass


def manual_mover(ms, toolhead):
    smm = object.__new__(ms.StepperManualMove)
    smm.toolhead = toolhead
    smm.travel_speed, smm.travel_accel = 100., 5000.
    smm.trapq, smm.stepper_kin = "private-trapq", "private-sk"
    smm.trapq_append = lambda *a: None
    smm.wiped = []
    smm.motion_queuing = types.SimpleNamespace(
        note_mcu_movequeue_activity=lambda t: None,
        wipe_trapq=smm.wiped.append)
    return smm


def test_manual_move_restores_stepper_state(ms):
    stepper, th = FakeStepper(), FakeToolhead()
    smm = manual_mover(ms, th)
    smm.manual_move(stepper, [0.01])
    assert (stepper.sk, stepper.trapq) == ("toolhead-sk", "toolhead-trapq")
    assert smm.wiped == ["private-trapq"]


def test_manual_move_restores_stepper_state_when_it_fails(ms):
    stepper, th = FakeStepper(), FakeToolhead()
    th.dwell_error = CommandError("Timer too close")
    smm = manual_mover(ms, th)
    with pytest.raises(CommandError, match="Timer too close"):
        smm.manual_move(stepper, [0.01])
    assert (stepper.sk, stepper.trapq) == ("toolhead-sk", "toolhead-trapq")
    assert smm.wiped == ["private-trapq"]


def test_kalico_adapter_wipe_drops_queued_moves(ms):
    never = FakeReactor.NEVER
    ad = object.__new__(ms.KalicoMotionAdapter)
    ad.printer = types.SimpleNamespace(
        get_reactor=lambda: types.SimpleNamespace(NEVER=never))
    calls = []
    ad.trapq_finalize_moves = lambda *a: calls.append(a)
    ad.wipe_trapq("tq")
    assert calls == [("tq", never, never)]


# -- beacon adapter ----------------------------------------------------

def test_beacon_handler_accepts_empty_and_multiple_batches(ms):
    h = object.__new__(ms.BeaconAccelHelper)
    h.samples, h.session = [], None
    assert h._handle_batch([]) is True
    assert h._handle_batch([{"data": [(1., 0, 0, 1)]},
                            {"data": [(2., 0, 0, 1)]}]) is True
    assert len(h.samples) == 2


# -- diagnostics -------------------------------------------------------

def test_session_lifecycle_is_logged(rig, caplog):
    caplog.set_level(logging.INFO)
    rig.chip.overflows = 50             # left over from an earlier bulk run
    def with_overflows(axes):
        rig.chip.overflows = 3          # lost while this sync was running
        rig.converge(axes)
    rig.sync(with_overflows)
    rig.settle()
    text = caplog.text
    assert "sync start axes=x,y" in text and "sync done axes=x,y" in text
    assert "lis2dw indx session 1 started mcu=indxmcu" in text
    # counter was reset by the chip's run start, so the delta is not
    # computed against the stale 50
    assert "chip_overflows_total=0 bulk_clients=" in text
    assert "chip_overflows_total=3 new=3" in text
    assert "session 1 unregistered" in text


def test_unregistered_previous_session_is_reported(ms, bulk_cls, caplog):
    caplog.set_level(logging.INFO)
    r, h = helper_rig(ms, bulk_cls)
    h.start_measurements()
    h.finish_measurements()
    h.start_measurements()
    assert "has not unregistered it yet" in caplog.text
    h.finish_measurements()
    h.start_measurements()
    assert "was still active" not in caplog.text
    h.start_measurements()
    assert "was still active at the start of a new one" in caplog.text


def test_long_reactor_blocking_section_is_logged(ms, bulk_cls, caplog):
    r, h = helper_rig(ms, bulk_cls)
    h._note_blocking(time.perf_counter() - 1.0, "deviation calc (N samples)")
    assert "blocked the reactor for" in caplog.text
    caplog.clear()
    h._note_blocking(time.perf_counter(), "deviation calc")
    assert caplog.text == ""
