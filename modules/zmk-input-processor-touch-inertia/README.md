# Touch-release wheel inertia

An independent MIT-licensed ZMK input processor. It observes a relative wheel
axis and a touch key without modifying or consuming input events. A real touch
release starts integer-only Q8 inertia; a new touch cancels it immediately.
The source is newly written from the requested algorithm, without copying
IQS7211E source, comments, or function organization.

## Integration

This directory is a standalone Zephyr module. In Nickey44A it is temporarily
bundled through the configuration repository's CMakeLists.txt and dts_root.
There is no invented remote commit SHA in west.yml.

To publish, create `nixiy/zmk-input-processor-touch-inertia`, commit the contents
of this directory at its root, and push. Add the project to west.yml with the
actual full commit SHA and the nixiy remote. Then remove the configuration
repository's bundling CMakeLists.txt and its `cmake`/`dts_root` module entries.
Use either the bundled module or the west project, never both.

```dts
touch_inertia: touch_inertia {
    compatible = "zmk,input-processor-touch-inertia";
    #input-processor-cells = <0>;
    wheel-code = <INPUT_REL_WHEEL>;
    touch-code = <INPUT_BTN_TOUCH>;
    tick-ms = <10>;
    decay-permille = <920>;
    stop-threshold-q8 = <96>;
    launch-threshold-q8 = <384>;
    ema-new-permille = <250>;
    release-timeout-ms = <100>;
    cancel-scroll-inertia-on-ctrl;
};
```

Place the node only on the central side, and append it to input-processors
immediately after orientation correction. HWHEEL and REL_MISC are untouched
when wheel-code is INPUT_REL_WHEEL. Touch events also pass through unchanged.
Use one instance for one listener/device; state and synchronization are per
instance. Split peripherals with no instance compile no mouse HID code.

## Behavior and limits

Nonzero wheel samples during touch are normalized to wheel units per tick.
The first sample uses delta times 256; subsequent samples use measured uptime
intervals clamped to at least one tick, preventing queued/bunched samples from
artificially multiplying the launch velocity. EMA applies even to the first sample.
A direction reversal or a gap longer than `release-timeout-ms` resets the old
velocity. Release more than this timeout after the last nonzero wheel sample
does not launch inertia; stopping the fingers before lifting stops scrolling.
Release without a wheel sample does nothing. Repeated release events do not
restart inertia. Wheel input cancels an existing coast, including zero input.

With `cancel-scroll-inertia-on-ctrl`, either left or right Ctrl in the ZMK
keyboard HID report suppresses velocity sampling and touch-release inertia.
Manual Ctrl+wheel events still pass through unchanged. An active coast checks
Ctrl before each report and stops at the next tick (10 ms by default), clearing
its velocity and remainder so releasing Ctrl cannot resume it. The property is
optional and enabled in Nickey44A's TPS43 overlay. Ctrl on another keyboard is
not visible to this firmware.

Each delayed work tick accumulates fractional motion, emits only vertical
scroll directly through ZMK HID, and multiplies speed by decay-permille/1000.
Direct HID output bypasses downstream processors. Do not place a wheel scaler
after this processor if normal and inertial wheel units must match.
Reports are limited to +/-127; excess is retained while inertia is running.
The instance mutex serializes touch reset with work/report emission. Input
callbacks must run in thread context (the normal Zephyr input listener path).

tick-ms=0 becomes 1. Negative tick values, decay outside 0..999, nonpositive
stop or launch thresholds, launch below stop, thresholds exceeding INT32_MAX,
and EMA weights outside 0..1000 fail at compile time.
No floating point, flick classification, or axis guessing.
`release-timeout-ms` must be positive and fit int32_t.

## Tuning and hardware checks

Defaults: 10 ms, decay 920, stop threshold 96 Q8 (0.375 units/tick), launch
threshold 384 Q8 (1.5 units/tick), EMA weight 250.
`launch-threshold-q8` is the minimum speed magnitude required to start inertia
on touch release (inclusive). `stop-threshold-q8` stops running inertia when
the speed magnitude falls below it. Keeping launch higher than stop prevents
unintended inertia after slow scrolling while preserving the decaying tail.
Raise decay to 940/960 for longer inertia; lower to 900/880 for shorter inertia.
Raise stop threshold to 128 for an earlier stop; lower to 64 for a longer tail.
Lower EMA weight to 200 for more smoothing; raise to 350 for quicker response.

Verify slow scroll, both flick directions, exponential stopping, re-touch
cancellation, and reversal after cancellation. Tap, hold, touch alone, pinch
alone, and horizontal swipe alone must never initiate vertical inertia.
Confirm Back/Forward and pinch zoom remain unaffected if processors exist.
Check both Ctrl keys with scrolling and touch release, and press Ctrl during
an active coast. Releasing Ctrl must not resume the old coast; a fresh normal
scroll must still launch inertia.
Check deep sleep/wake followed by scrolling and flicking. No activity-state
subscription is included; add one only if hardware testing demonstrates a
stale state across sleep. Firmware build success does not verify touch feel
or power-management behavior.

## Nickey44A Mac scroll adjustment

The right overlay uses stop threshold 256 Q8 (one wheel unit per tick) to
avoid the sparse one-unit pulses at the end of a sub-unit coast. The generic
module default remains 96. Direction is corrected with driver `invert-scroll-x`
before the listener swaps axes, so both manual and inertial vertical scrolling
reverse together. Pointer motion and the horizontal swipe axis are unaffected.
This is relative mouse-wheel output, not native macOS trackpad momentum.
Host acceleration and BLE delivery still require hardware testing.

Run `python3 tests/test_touch_inertia.py` from the repository root for production
C regression tests with a fake clock, work queue, and HID sink. They exercise
bursty delivery, pauses, reversals, both signs, tail continuity, touch cancellation,
and Ctrl cancellation; they do not simulate Zephyr concurrency or BLE timing.
