"""Check built Devicetrees/configs and model the standard left processor chain.

Run with the Python environment used by west. This checks configuration and
event semantics; it does not simulate BLE, TPS43 silicon, or host HID timing.
"""

import argparse
from pathlib import Path
import re
import struct
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zephyr-base", required=True, type=Path)
    parser.add_argument("--left-build", required=True, type=Path)
    parser.add_argument("--right-build", required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.zephyr_base / "scripts/dts/python-devicetree/src"))
    from devicetree import dtlib

    left = dtlib.DT(str(args.left_build / "zephyr/zephyr.dts"))
    right = dtlib.DT(str(args.right_build / "zephyr/zephyr.dts"))
    constants = dict(re.findall(
        r"^#define\s+(INPUT_(?:EV|REL|BTN)_\w+)\s+(0x[0-9a-fA-F]+|\d+)\b",
        (args.zephyr_base / "include/zephyr/dt-bindings/input/input-event-codes.h")
        .read_text(), re.MULTILINE))
    c = {name: int(value, 0) for name, value in constants.items()}

    def cells_of(prop):
        # Mixed phandle/number properties cannot use dtlib's to_nums().
        return list(struct.unpack(">" + "I" * (len(prop.value) // 4), prop.value))

    def enabled(node, name):
        return name in node.props

    def config(build):
        return dict(re.findall(r"^(CONFIG_\w+)=(.+)$",
                              (build / "zephyr/.config").read_text(), re.MULTILINE))

    lc, rc = config(args.left_build), config(args.right_build)
    for cfg, build in ((lc, args.left_build), (rc, args.right_build)):
        for name in ("GPIO", "I2C", "INPUT", "ZMK_POINTING", "INPUT_TPS43",
                     "ZMK_INPUT_SPLIT", "ZMK_SPLIT", "ZMK_SLEEP",
                     "ZMK_BATTERY_REPORTING"):
            assert cfg.get("CONFIG_" + name) == "y", name
        assert (build / "zephyr/zmk.uf2").is_file(), "Missing firmware"
    assert lc.get("CONFIG_ZMK_SPLIT_ROLE_CENTRAL") != "y"
    assert rc.get("CONFIG_ZMK_SPLIT_ROLE_CENTRAL") == "y"
    assert rc.get("CONFIG_ZMK_INPUT_LISTENER") == "y"
    for name in ("ZMK_INPUT_PROCESSOR_SCALER", "ZMK_INPUT_PROCESSOR_CODE_MAPPER"):
        assert lc.get("CONFIG_" + name) == "y", name
    for name in ("ZMK_SPLIT_BLE_CENTRAL_BATTERY_LEVEL_PROXY",
                 "ZMK_SPLIT_BLE_CENTRAL_BATTERY_LEVEL_FETCHING", "ZMK_STUDIO"):
        assert rc.get("CONFIG_" + name) == "y", name

    lt = left.label2node["left_tps43"]
    rt = right.label2node["tps43"]
    for dt, node in ((left, lt), (right, rt)):
        assert node.props["reg"].to_nums() == [0x74]
        assert cells_of(node.props["rst-gpios"]) == [
            dt.label2node["gpio0"].props["phandle"].to_num(), 16, 0]
        assert cells_of(node.props["rdy-gpios"]) == [
            dt.label2node["gpio1"].props["phandle"].to_num(), 10, 0]
        assert enabled(node, "enable-power-management")
        for state in ("default", "sleep"):
            label = ("nickey44a_left_tps43_i2c0_" if dt is left
                     else "nickey44a_tps43_i2c0_") + state
            psels = dt.label2node[label].nodes["group1"].props["psels"].to_nums()
            assert [v & 0x1ff for v in psels] == [9, 10]
        # No other direct GPIO consumer or pinctrl group owns P0.16.
        gpio0 = dt.label2node["gpio0"].props["phandle"].to_num()
        for candidate in dt.node_iter():
            for prop in candidate.props.values():
                if prop.name == "gpios" or prop.name.endswith("-gpios"):
                    values = cells_of(prop)
                    for index in range(0, len(values), 3):
                        if values[index:index + 2] == [gpio0, 16]:
                            assert candidate is node and prop.name == "rst-gpios"
                if prop.name == "psels":
                    assert all(v & 0x1ff != 16 for v in prop.to_nums())
    assert enabled(lt, "press-and-hold") and not enabled(lt, "single-tap")
    assert not enabled(lt, "switch-xy")
    assert lt.props["hold-time"].to_num() == 1
    assert enabled(lt, "invert-scroll-x") and enabled(lt, "invert-scroll-y")
    assert not enabled(rt, "press-and-hold")
    assert enabled(rt, "invert-scroll-x") and enabled(rt, "invert-scroll-y")
    inertia = right.label2node["tps43_touch_inertia"]
    assert inertia.props["stop-threshold-q8"].to_num() == 256
    assert inertia.props["release-timeout-ms"].to_num() == 100
    for name in ("single-tap", "two-finger-tap", "scroll", "switch-xy", "invert-x"):
        assert enabled(rt, name), name
    assert enabled(lt, "two-finger-tap") and enabled(lt, "scroll")

    ls = left.label2node["left_tps43_split"]
    rs = right.label2node["left_tps43_split"]
    assert ls.props["reg"].to_num() == rs.props["reg"].to_num() == 0
    assert ls.props["device"].to_node() is lt
    assert "device" not in rs.props
    listener = right.label2node["left_tps43_listener"]
    assert listener.props["device"].to_node() is rs
    swipe = right.label2node["left_tps43_tab_swipe"]
    assert cells_of(listener.props["input-processors"]) == [
        swipe.props["phandle"].to_num()]
    assert swipe.props["hwheel-code"].to_num() == c["INPUT_REL_DIAL"]
    assert swipe.props["wheel-code"].to_num() == c["INPUT_REL_HWHEEL"]
    assert swipe.props["touch-code"].to_num() == c["INPUT_BTN_TOUCH"]
    assert swipe.props["threshold"].to_num() == 8
    assert swipe.props["axis-ratio"].to_num() == 2
    # Physical right swipe produces negative DIAL; encoded TAB with Ctrl / Ctrl+Shift.
    assert swipe.props["left-keycode"].to_num() == 0x0107002b
    assert swipe.props["right-keycode"].to_num() == 0x0307002b
    right_swipe = right.label2node["tps43_touch_swipe"]
    assert right_swipe.props["left-keycode"].to_num() == 0x000c0224
    assert right_swipe.props["right-keycode"].to_num() == 0x000c0225
    assert right_swipe.props["override-layer"].to_num() == 1
    assert right_swipe.props["override-left-keycode"].to_num() == 0x0107002b
    assert right_swipe.props["override-right-keycode"].to_num() == 0x0307002b

    chain, cells = [], cells_of(ls.props["input-processors"])
    while cells:
        node = left.phandle2node[cells.pop(0)]
        count = node.props["#input-processor-cells"].to_num()
        chain.append((node, cells[:count]))
        del cells[:count]
    assert [n.name for n, _ in chain] == [
        "zip_xy_scaler", "left_swipe_axis_mapper", "left_wheel_to_hwheel",
        "left_middle_click_mapper"]
    assert [params for _, params in chain] == [[0, 1], [], [], []]

    def process(kind, code, value, sync=True):
        # Model the pinned standard processors using the actual built properties.
        for node, params in chain:
            if kind != node.props["type"].to_num():
                continue
            compatible = node.props["compatible"].to_string()
            if compatible == "zmk,input-processor-scaler":
                if code in node.props["codes"].to_nums():
                    value = int(value * params[0] / params[1])
            elif compatible == "zmk,input-processor-code-mapper":
                mapping = node.props["map"].to_nums()
                for original, replacement in zip(mapping[::2], mapping[1::2]):
                    if code == original:
                        code = replacement
                        break
            else:
                raise AssertionError(compatible)
        return kind, code, value, sync

    rel, key = c["INPUT_EV_REL"], c["INPUT_EV_KEY"]
    for value in (-32768, -100, -1, 0, 1, 100, 32767):
        for code in ("INPUT_REL_X", "INPUT_REL_Y"):
            assert process(rel, c[code], value) == (rel, c[code], 0, True)
        assert process(rel, c["INPUT_REL_HWHEEL"], value) == (
            rel, c["INPUT_REL_DIAL"], value, True)
        assert process(rel, c["INPUT_REL_WHEEL"], value) == (
            rel, c["INPUT_REL_HWHEEL"], value, True)
    for value in (0, 1):
        assert process(key, c["INPUT_BTN_1"], value) == (key, c["INPUT_BTN_2"], value, True)
        assert process(key, c["INPUT_BTN_0"], value) == (key, c["INPUT_BTN_0"], value, True)
    assert process(rel, c["INPUT_REL_X"], 25, False)[3] is False

    # Model the shared HID reference count: left hold + right tap + movement.
    count = 0
    for value, expected in ((1, 1), (1, 2), (0, 1)):
        assert process(key, c["INPUT_BTN_0"], value)[2] == value
        count += 1 if value else -1
        assert count == expected
    for _ in range(100):
        assert process(rel, c["INPUT_REL_X"], 5)[2] == 0
        assert count == 1  # Right movement sends no button release.
    count -= 1
    assert count == 0
    print("PASS: built pins, gestures, split routing, roles, battery/sleep/Studio config")
    print("PASS: processor order, signed motion blocking, swipe/scroll separation, tab keys, sync")
    print("PASS: event model preserves left hold through movement and a right tap")
    print("Hardware/BLE/Deep Sleep acceptance tests remain manual.")


if __name__ == "__main__":
    main()
