# Kalico patches and manual verification

Nothing here has been applied to your printer. These are patches for you to
apply (or not), plus a procedure to check what they change. Run the
read-only identification steps first; nothing in steps 0 to 2 moves the
machine.

## What the patches are

| File | Repo | Purpose |
| --- | --- | --- |
| `0001-fix-load_cell-report-per-batch-overflow-deltas.patch` | Kalico | Upstream PR #947 (commit `17380399`, merged upstream after `3ec541c9`), verified to apply cleanly to `M1K8/kalico` at `3ec541c9`. **The fix** for historical overflows being re-reported on every batch. |
| `0002-bulk_sensor-log-possible_overflows-events.patch` | Kalico | **Diagnostic only.** One `klippy.log` line at the first overflow of a bulk run and each time its total doubles. Lets you see *when* the first overflow happens relative to the `motors_sync: sync start/done` lines. |
| `../motors_sync.py` | motors-sync | Acquisition lifecycle hardening and diagnostics (this branch). |

## 0. Identify what is actually installed (read-only)

```
# Active Kalico checkout and commit
ps -eo args | grep '[k]lippy.py'            # path of klippy.py -> Kalico dir
KAL=~/klipper                               # adjust to what the line above shows
git -C $KAL rev-parse HEAD; git -C $KAL branch --show-current
git -C $KAL remote -v; git -C $KAL status --short

# The motors_sync.py Klipper really loads (resolve the symlink)
MS=$(readlink -f $KAL/klippy/extras/motors_sync.py); echo $MS
MSD=$(dirname $MS)
git -C $MSD rev-parse HEAD; git -C $MSD branch --show-current
git -C $MSD remote -v; git -C $MSD status --short; git -C $MSD diff --stat

# Is Kalico PR #947 already in this checkout? (the ads*.py drivers are the
# SPI load cell ADCs; hx71x.py already had part of this logic before #947)
#   prints lines = #947 present;  nothing = not present
grep -n "get_last_overflows() - prev_overflows" $KAL/klippy/extras/load_cell/ads*.py
#   prints lines = NOT present (each batch still carries lifetime totals)
grep -nE '"overflows": self.ffreader.get_last_overflows\(\),$' $KAL/klippy/extras/load_cell/ads*.py

# The exact origin of "Sensor readout error" in the INSTALLED tree
# (it is not in Kalico 3ec541c9, upstream main, or Bondtech's module)
grep -rn "readout error" $KAL/klippy $KAL/src ~/printer_data/config 2>/dev/null

# Which accelerometer motors_sync uses, and which MCU/bus every sensor is on
grep -n -E '^\[(mcu|indx|motors_sync|lis2dw|adxl345|load_cell|probe_eddy_current|ldc1612)' ~/printer_data/config/*.cfg
grep -n -E 'accel_chip|encoder_chip|i2c_mcu|spi_bus|cs_pin|sensor_type' ~/printer_data/config/*.cfg
```

The INDX firmware build enables `CONFIG_WANT_LIS2DW=y`. If `accel_chip` is a
`lis2dw` on the INDX MCU, SYNC_MOTORS runs a bulk accelerometer stream on the
same MCU (and USB link) as the load cell and the LDC1612. The new
`motors_sync: ... session N started mcu=<name>` log line reports this directly.

## 1. Install (your call)

```
# motors-sync (symlinked into Klipper by install.sh, so the checkout is what runs)
git -C $MSD fetch origin claude/modest-euler-t99dhb
git -C $MSD log --oneline HEAD..FETCH_HEAD          # review first
# only if $MSD status is clean; otherwise stash or commit your changes first
git -C $MSD checkout claude/modest-euler-t99dhb

# Kalico: working-tree changes only, no commit and no git identity needed.
# 0002 is the diagnostic, 0001 is the fix (to A/B the fix, apply 0002 first,
# run step 3, then apply 0001 and run step 3 again).
for p in 0002 0001; do git -C $KAL apply --check $MSD/kalico_patches/$p-*.patch || break; done
git -C $KAL apply $MSD/kalico_patches/0002-*.patch
git -C $KAL apply $MSD/kalico_patches/0001-*.patch
```
Then `RESTART` from the console (host-only restart; no firmware change).

## 2. Stationary diagnostics (no motion)

Do this with the printer idle, nozzle cold or at rest temperature, nothing
on the bed, load cell untouched.

1. **Baseline** right after a fresh boot or `RESTART`:
   `LOAD_CELL_DIAGNOSTIC` (10 s, no motion). Record the "Sensor reported
   errors" line (or its absence), "Measured samples per second" vs
   "configured", and "Samples Collected".
2. **Eddy, stationary**: `LDC_CALIBRATE_DRIVE_CURRENT CHIP=<your ldc1612 name>`
   (no motion; it briefly reads/writes chip registers and restores them. It
   stages a `reg_drive_current` value that is only written if you later run
   `SAVE_CONFIG`, so do not). It fails with a clear error if the I2C path or
   chip id is bad.
3. Note the last lines of `~/printer_data/logs/klippy.log` so you can find
   where each later step starts.

## 3. Synchronization, then health (supervised; this moves the motors)

SYNC_MOTORS homes if needed, travels XY to the middle of the rails and makes
small motor moves. Raise Z well clear of the bed first (e.g. `G28`, then
`G1 Z30 F600`), keep a hand on the emergency stop, and have nothing on the bed.

1. Run `SYNC_MOTORS`.
2. **Immediately after**, with the printer standing still:
   `LOAD_CELL_DIAGNOSTIC`, then **again** straight away, then once more
   after about a minute.
3. Repeat steps 1 and 2 two more times (three sync cycles in total).
4. Repeat the eddy check from 2.2.

How to read the results:

* **Overflows fixed or zero on the 2nd and 3rd check** (with 0001 applied):
  the loss happened during the sync and has stopped; any earlier persistence
  was the accounting bug.
* **Overflows increasing on consecutive checks (0001 applied):** acquisition
  loss is still continuing after the sync. Look at the next point.
* Without 0001 the number is the *lifetime total times the number of batches
  in the window*, so it is large, grows with window length, and cannot tell
  these cases apart. That is why 0001 should be on before judging.

In `klippy.log` (patch 0002 plus this branch's motors_sync):

* `motors_sync: sync start axes=... ` / `sync done` / `sync aborted` bracket the run.
* `bulk_sensor: oid=N possible_overflows +d (run total T, mcu counter C)` lines
  that fall **between** sync start and sync done mean the loss happened during
  the sync. Lines that keep appearing **after** sync done mean it is
  continuing. Note which `oid` (load cell, eddy, accelerometer).
* `... session K unregistered X.XXXs after finish` should appear for every
  session. If a session never gets this line, its client is still registered.
* `... session K started ... bulk_clients=N`: N above the number of active
  sessions means clients are accumulating.
* `... blocked the reactor for X.XXXs`: compute that did not yield to the
  reactor. Short and rare is normal.

## 4. Probing (separate, supervised step)

Only after step 3 looked sane. Bed clear and clean, nozzle clean, emergency
stop in reach, a low probing speed. Run one `PROBE` (or `PROBE_ACCURACY
SAMPLES=3`) for each probe type (load cell, then eddy) **before** any
`SYNC_MOTORS` in the session, then after one, then after three. Compare.

## 5. Minimum reset experiment (only if it fails again)

Run one at a time and re-check with `LOAD_CELL_DIAGNOSTIC` after each; stop at
the first that clears it:

1. `RESTART` (host process only; the MCUs keep running).
2. `FIRMWARE_RESTART` (resets the MCUs).
3. Power-cycle the INDX toolboard.

By the code, `RESTART` should be enough for the load cell counters (the host
counter is reset by `note_start()` and the MCU counter by `sensor_bulk_reset`
when the sensor's bulk run starts). If `RESTART` does not clear the failure,
the cause is not the counter accounting, and the log lines above are what to
send back.

## Rollback (keeps unrelated local modifications)

Write down, before step 1: `git -C $KAL rev-parse HEAD`,
`git -C $KAL status --short`, `git -C $KAL stash list`, and the same for `$MSD`.

```
# Kalico: both patches are plain working-tree changes, so reverse-apply them.
# This only touches the lines the patches added; your other edits stay.
git -C $KAL apply -R $MSD/kalico_patches/0001-*.patch
git -C $KAL apply -R $MSD/kalico_patches/0002-*.patch
git -C $KAL status --short                         # should match what you recorded

# motors-sync: go back to the branch/commit you recorded
git -C $MSD status --short                         # must not touch your own edits
git -C $MSD checkout <recorded-branch>             # or: git checkout <recorded-sha>
```
If `checkout` refuses because of your own local changes to `motors_sync.py`,
stash them (`git -C $MSD stash push motors_sync.py`), checkout, then
`git stash pop`. Finish with `RESTART`. Neither step touches firmware.
