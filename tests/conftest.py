# Test harness for motors_sync.py with no Klipper/Kalico host and no
# hardware. motors_sync is a Klipper "extras" module (relative imports of
# sibling extras, chelper), so a stub package provides those, and the
# module under test is loaded from MOTORS_SYNC_PATH (default: the
# repository's motors_sync.py; point it at another revision to compare).
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PKG = "extras_stub"


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
    def pause(self, waketime):
        self.advance(max(waketime - self.now, 0.))
        return self.now
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


class ModelBatchBulk:
    # Mirrors klippy/extras/bulk_sensor.py BatchBulkHelper (Kalico
    # 3ec541c9): a client is unregistered only when its callback returns
    # False for a delivered, non-empty batch.
    def __init__(self, printer, batch_cb, start_cb=None, stop_cb=None,
                 batch_interval=0.5):
        self.printer = printer
        self.batch_cb = batch_cb
        self.start_cb = start_cb or (lambda: None)
        self.stop_cb = stop_cb or (lambda: None)
        self.batch_interval = batch_interval
        self.is_started = False
        self.batch_timer = None
        self.client_cbs = []
    def _start(self):
        if self.is_started:
            return
        self.is_started = True
        self.start_cb()
        reactor = self.printer.get_reactor()
        self.batch_timer = reactor.register_timer(
            self._proc_batch, reactor.monotonic() + self.batch_interval)
    def _stop(self):
        del self.client_cbs[:]
        self.printer.get_reactor().unregister_timer(self.batch_timer)
        self.batch_timer = None
        if not self.is_started:
            return
        self.stop_cb()
        self.is_started = False
        if self.client_cbs:
            self._start()
    def _proc_batch(self, eventtime):
        msg = self.batch_cb(eventtime)
        if not msg:
            return eventtime + self.batch_interval
        for client_cb in list(self.client_cbs):
            if not client_cb(msg):
                self.client_cbs.remove(client_cb)
                if not self.client_cbs:
                    self._stop()
                    return self.printer.get_reactor().NEVER
        return eventtime + self.batch_interval
    def add_client(self, client_cb):
        self.client_cbs.append(client_cb)
        self._start()


def _load_kalico_bulk():
    path = os.environ.get("KALICO_PATH")
    if not path:
        return None
    f = Path(path) / "klippy" / "extras" / "bulk_sensor.py"
    if not f.exists():
        return None
    spec = importlib.util.spec_from_file_location("kalico_bulk_sensor", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.BatchBulkHelper


@pytest.fixture(params=["model", "kalico"])
def bulk_cls(request):
    if request.param == "model":
        return ModelBatchBulk
    cls = _load_kalico_bulk()
    if cls is None:
        pytest.skip("set KALICO_PATH to also run against Kalico's real "
                    "BatchBulkHelper")
    return cls


@pytest.fixture(scope="session")
def ms():
    # motors_sync module with its host-side dependencies stubbed
    target = Path(os.environ.get("MOTORS_SYNC_PATH", REPO / "motors_sync.py"))
    saved = {k: v for k, v in sys.modules.items()
             if k == "chelper" or k.startswith(PKG)}
    pkg = types.ModuleType(PKG)
    pkg.__path__ = []
    sys.modules[PKG] = pkg
    def stub(name, **attrs):
        mod = types.ModuleType(f"{PKG}.{name}")
        mod.__dict__.update(attrs)
        sys.modules[mod.__name__] = mod
        setattr(pkg, name, mod)
    class ZAdjustStatus:
        def __init__(self, printer): pass
        def reset(self): pass
        def check_retry_result(self, r): pass
        def get_status(self, t): return {}
    def calc_move_time(dist, speed, accel):
        return 1.0 if dist >= 0 else -1.0, 0.0, abs(dist) / speed, speed
    stub("z_tilt", ZAdjustStatus=ZAdjustStatus)
    stub("force_move", calc_move_time=calc_move_time)
    stub("controller_fan", ControllerFan=type("ControllerFan", (), {}))
    stub("heater_fan", PrinterHeaterFan=type("PrinterHeaterFan", (), {}))
    stub("temperature_fan", TemperatureFan=type("TemperatureFan", (), {}))
    sys.modules["chelper"] = types.ModuleType("chelper")
    spec = importlib.util.spec_from_file_location(f"{PKG}.motors_sync",
                                                  target)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yield mod
    for k in [k for k in sys.modules if k == "chelper" or k.startswith(PKG)]:
        del sys.modules[k]
    sys.modules.update(saved)
