# Touch swipe

Optional `override-layer`, `override-left-keycode`, and `override-right-keycode`
properties select alternate keycodes while the specified layer ID is active.
The layer is checked when the swipe threshold is reached. The same touch state
is retained across layer changes, so changing layers cannot retrigger a swipe
until the finger is lifted. The default `override-layer = -1` disables this feature.

Local MIT-licensed processor, ordered after orientation. One instance must serve
one input listener. A shared mutex serializes input callbacks, pending shortcut
work, and key emission. Optional pairing coordinates exactly two instances on
the central side; the peripheral still sends the normal split input stream.
The configuration repository adds the source via its root CMakeLists.txt.
Because Zephyr 3.5 module metadata accepts only one dts_root (already used by
touch-inertia), Nickey44A also carries an identical binding mirror in its
shield's dts/bindings directory, an automatic DTS root. Keep these YAML files
in sync. The standalone module uses its own binding; do not load it twice.

TOUCH down resets only on the idle-to-touch transition. Every TOUCH up fully
resets touching, fired, both signed accumulators and total vertical travel.
HWHEEL accumulates signed horizontal motion; WHEEL accumulates signed vertical
motion and absolute vertical travel. Using total vertical travel prevents
repeated up/down scrolling from cancelling the dominance guard.
Detection requires abs(horizontal) >= threshold AND
abs(horizontal) >= total_vertical_travel * axis-ratio. After firing, no further
samples are accumulated until all fingers release, including duplicate down
events and reversals. Integer arithmetic saturates safely at int32 limits and
uses int64 for magnitude and ratio calculations.

HWHEEL always returns ZMK_INPUT_PROC_STOP, even outside touch. WHEEL, TOUCH,
cursor and button events pass unchanged. Nickey44A firmware inertia remains
disabled. Snipe still scales only cursor X/Y before the common chain.

left-keycode/right-keycode are required encoded ZMK keycodes; Nickey44A sets
C_AC_BACK/C_AC_FORWARD. The same keycode-state events used by &kp produce a
press then release through ZMK's HID listener. Arbitrary behavior bindings
are not implemented. Tab switching uses Ctrl+Tab/Ctrl+Shift+Tab; the TPS43 driver
is unchanged.

## Tuning and hardware validation

Nickey44A starts with threshold=8 and axis-ratio=2. The pinned TPS43 driver
outputs only its dominant scroll axis per sample, truncated as movement *
scroll-sensitivity / 100. At sensitivity=5, eight emitted units correspond
to approximately 160 controller movement units before per-sample truncation.
This is a starting candidate, not a measured physical distance or guarantee.
The processor can observe only the selected, transformed axis; discarded
diagonal components and future vertical motion cannot inform an earlier fire.
Very slow movements can truncate to zero. Confirm direction on hardware.

Edit threshold and axis-ratio in nickey44a_r.overlay and rebuild the right
firmware. Try threshold 12/16 for accidental fires, or 4/6 for shorter swipes;
axis-ratio 3 requires stronger horizontal dominance. Vertical-heavy touches
may need release before a horizontal gesture, deliberately.

Check left/right once per touch, same-direction continuation, reversal,
duplicate down, complete release/re-touch, tap/right-click, single-finger
cursor/tap/hold, repeated vertical scrolling, layer-1 Snipe,
deep sleep/wake, and USB/BLE Consumer reports in the target browser.
Build success does not establish physical gesture feel or sleep behavior.

## Paired outward swipe / Mission Control

`pair-slot = <0>` and `<1>` opt two central-side instances into one shared
pair. The default `-1` retains independent immediate swipes. Both instances
must set the same positive `pair-window-ms` (250 here) and nonzero encoded
`pair-keycode` (`LC(UP)` here). Duplicate slots or mismatched pair configuration
fail device initialization. `pair-direction` is the outward sign on each
instance's transformed horizontal input, either 1 or -1. Nickey44A uses +1 on
left REL_DIAL and -1 on right REL_HWHEEL, following its documented measured
physical directions. Left split firmware and the TPS43 driver are unchanged.

An outward swipe meeting the existing distance/dominance threshold holds its
normal shortcut for up to 250 ms. If the other pad meets its outward threshold
before the deadline while both touch states are active, the processor cancels
the pending shortcut and taps Control+Up once. It emits neither normal shortcut.
Both pads must be released before another paired gesture; re-touching only one
pad while the other stays down cannot retrigger or emit an extra swipe shortcut.
This applies in layer 1 too. The Mac must have Control+Up assigned to Mission
Control (the default shortcut); this is a discrete shortcut, not an interactive
native-trackpad animation driven by swipe distance.

If no partner qualifies, timeout or the originating pad's release emits the
normal shortcut once. The normal keycode is captured at recognition so a later
layer change cannot alter it. Inward swipes remain immediate. Closing gestures,
same-direction physical swipes, cursor-only motion, touch-only input and
vertical-heavy scrolls do not form a pair. The existing driver produces the
scroll axes used for two-finger swipes; this processor receives no raw finger
count or individual finger positions and does not implement pinch recognition.

The window compares recognition timestamps on the central side, including BLE
transport delay, not the original sensor timestamps. Confirm the 250 ms window,
threshold 8, directions, USB/BLE behavior, and left hold/right tap interactions
on hardware. Adjust both window properties together if needed.

`python3 tests/test_paired_swipe.py` executes the production C state machine with
mocked time, workqueue and key output under the undefined-behavior sanitizer.
It checks both arrival orders, window edges and delayed timeout work, matching
and nonmatching gestures, fallback, cancellation, both-pad rearming, layer
capture, passthrough and integer extremes. It does not simulate concurrent
Zephyr scheduling, BLE transport or physical gesture detection.
