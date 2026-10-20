/* SPDX-License-Identifier: MIT */
#define DT_DRV_COMPAT zmk_input_processor_touch_swipe
#include <zephyr/device.h>
#include <zephyr/kernel.h>

#if DT_HAS_COMPAT_STATUS_OKAY(DT_DRV_COMPAT)
#if IS_ENABLED(CONFIG_ZMK_SPLIT) && !IS_ENABLED(CONFIG_ZMK_SPLIT_ROLE_CENTRAL)
#error "touch-swipe requires a central-side devicetree instance"
#endif

#include <limits.h>
#include <errno.h>
#include <drivers/input_processor.h>
#include <zmk/events/keycode_state_changed.h>
#include <zmk/keymap.h>
#include <zephyr/logging/log.h>
LOG_MODULE_DECLARE(zmk, CONFIG_ZMK_LOG_LEVEL);

struct touch_swipe_config {
    uint16_t hwheel_code;
    uint16_t wheel_code;
    uint16_t touch_code;
    int32_t threshold;
    int32_t axis_ratio;
    uint32_t left_keycode;
    uint32_t right_keycode;
    int32_t override_layer;
    uint32_t override_left_keycode;
    uint32_t override_right_keycode;
    int32_t pair_slot;
    int32_t pair_direction;
    int32_t pair_window_ms;
    uint32_t pair_keycode;
};

struct touch_swipe_data {
    const struct device *dev;
    struct k_work_delayable pending_work;
    bool pending;
    int64_t pending_since;
    uint32_t pending_keycode;
    bool touching;
    bool swipe_fired;
    int32_t horizontal_accum;
    int32_t vertical_accum;
    /* Total vertical travel prevents opposite scroll samples cancelling out. */
    int32_t vertical_travel;
};

/* Input callbacks and timeout work may run on different threads. */
K_MUTEX_DEFINE(swipe_lock);
static struct touch_swipe_data *pair_slots[2];
static bool pair_latched;

static int64_t magnitude(int32_t value) {
    return value < 0 ? -(int64_t)value : (int64_t)value;
}

static int32_t bounded_add(int32_t accum, int64_t delta) {
    return CLAMP((int64_t)accum + delta, (int64_t)INT32_MIN, (int64_t)INT32_MAX);
}

static void reset_gesture(struct touch_swipe_data *data) {
    data->touching = false;
    data->swipe_fired = false;
    data->horizontal_accum = 0;
    data->vertical_accum = 0;
    data->vertical_travel = 0;
}

static void tap_keycode(uint32_t keycode) {
    int64_t timestamp = k_uptime_get();
    int press = raise_zmk_keycode_state_changed_from_encoded(keycode, true, timestamp);
    int release = raise_zmk_keycode_state_changed_from_encoded(keycode, false, timestamp);
    if (press < 0 || release < 0) {
        LOG_WRN("touch swipe key output failed: %d/%d", press, release);
    }
}

/* All pending/pair state changes and key emission hold swipe_lock. */
static void cancel_pending(struct touch_swipe_data *data) {
    data->pending = false;
    k_work_cancel_delayable(&data->pending_work);
}

static void flush_pending(struct touch_swipe_data *data) {
    if (!data->pending) {
        return;
    }
    uint32_t keycode = data->pending_keycode;
    cancel_pending(data);
    tap_keycode(keycode);
}

static void pending_timeout(struct k_work *work) {
    struct touch_swipe_data *data = CONTAINER_OF(
        k_work_delayable_from_work(work), struct touch_swipe_data, pending_work);
    const struct touch_swipe_config *cfg = data->dev->config;
    k_mutex_lock(&swipe_lock, K_FOREVER);
    if (data->pending) {
        int64_t remaining = data->pending_since + cfg->pair_window_ms - k_uptime_get();
        /* A cancelled old callback can run after another touch was scheduled. */
        if (remaining > 0) {
            k_work_reschedule(&data->pending_work, K_MSEC(remaining));
        } else {
            flush_pending(data);
        }
    }
    k_mutex_unlock(&swipe_lock);
}

static void recognize_swipe(struct touch_swipe_data *data, uint32_t keycode) {
    const struct touch_swipe_config *cfg = data->dev->config;
    bool outward = cfg->pair_direction > 0 ? data->horizontal_accum > 0
                                          : data->horizontal_accum < 0;
    if (cfg->pair_slot < 0 || !outward) {
        tap_keycode(keycode);
        return;
    }
    struct touch_swipe_data *peer = pair_slots[1 - cfg->pair_slot];
    int64_t now = k_uptime_get();
    if (peer && peer->pending && now - peer->pending_since >= cfg->pair_window_ms) {
        /* Enforce the deadline even if the system workqueue is delayed. */
        flush_pending(peer);
    }
    if (peer && peer->pending && peer->touching) {
        cancel_pending(peer);
        pair_latched = true;
        /* Both candidates are consumed; neither emits its ordinary shortcut. */
        tap_keycode(cfg->pair_keycode);
        return;
    }
    data->pending = true;
    data->pending_since = now;
    data->pending_keycode = keycode; /* Preserve the layer at recognition. */
    k_work_reschedule(&data->pending_work, K_MSEC(cfg->pair_window_ms));
}

static int observe_locked(const struct device *dev, struct input_event *event,
                         uint32_t param1, uint32_t param2,
                         struct zmk_input_processor_state *state) {
    ARG_UNUSED(param1);
    ARG_UNUSED(param2);
    ARG_UNUSED(state);
    struct touch_swipe_data *data = dev->data;
    const struct touch_swipe_config *cfg = dev->config;

    if (event->type == INPUT_EV_KEY && event->code == cfg->touch_code) {
        if (!event->value) {
            flush_pending(data);
            reset_gesture(data);
            if (cfg->pair_slot >= 0 && pair_slots[0] && pair_slots[1] &&
                !pair_slots[0]->touching && !pair_slots[1]->touching) {
                pair_latched = false;
            }
            LOG_DBG("touch swipe release/reset");
        } else if (!data->touching) {
            reset_gesture(data);
            data->touching = true;
            LOG_DBG("touch swipe start");
        }
        return ZMK_INPUT_PROC_CONTINUE;
    }
    if (cfg->pair_slot >= 0 && pair_latched) {
        /* Re-arm only after both pads have released, including re-touch attempts. */
        return event->type == INPUT_EV_REL && event->code == cfg->hwheel_code
                   ? ZMK_INPUT_PROC_STOP : ZMK_INPUT_PROC_CONTINUE;
    }
    if (event->type != INPUT_EV_REL) {
        return ZMK_INPUT_PROC_CONTINUE;
    }
    if (event->code == cfg->wheel_code) {
        if (data->touching && !data->swipe_fired) {
            data->vertical_accum = bounded_add(data->vertical_accum, event->value);
            data->vertical_travel = bounded_add(data->vertical_travel, magnitude(event->value));
        }
        return ZMK_INPUT_PROC_CONTINUE;
    }
    if (event->code != cfg->hwheel_code) {
        return ZMK_INPUT_PROC_CONTINUE;
    }

    if (data->touching && !data->swipe_fired) {
        data->horizontal_accum = bounded_add(data->horizontal_accum, event->value);
        int64_t horizontal = magnitude(data->horizontal_accum);
        if (horizontal >= cfg->threshold &&
            horizontal >= (int64_t)data->vertical_travel * cfg->axis_ratio) {
            /* Latch before output: neither reversal nor output failure may retrigger. */
            data->swipe_fired = true;
            bool left = data->horizontal_accum < 0;
            uint32_t keycode = left ? cfg->left_keycode : cfg->right_keycode;
            if (cfg->override_layer >= 0 && zmk_keymap_layer_active(cfg->override_layer)) {
                keycode = left ? cfg->override_left_keycode : cfg->override_right_keycode;
            }
            recognize_swipe(data, keycode);
            LOG_DBG("touch swipe %s fired", left ? "left" : "right");
        }
    }
    /* Always suppress horizontal scrolling, including outside a tracked touch. */
    return ZMK_INPUT_PROC_STOP;
}

static int observe_input(const struct device *dev, struct input_event *event,
                         uint32_t param1, uint32_t param2,
                         struct zmk_input_processor_state *state) {
    k_mutex_lock(&swipe_lock, K_FOREVER);
    int result = observe_locked(dev, event, param1, param2, state);
    k_mutex_unlock(&swipe_lock);
    return result;
}

static int initialize(const struct device *dev) {
    struct touch_swipe_data *data = dev->data;
    const struct touch_swipe_config *cfg = dev->config;
    data->dev = dev;
    k_work_init_delayable(&data->pending_work, pending_timeout);
    if (cfg->pair_slot >= 0) {
        if (pair_slots[cfg->pair_slot]) {
            return -EINVAL;
        }
        struct touch_swipe_data *peer = pair_slots[1 - cfg->pair_slot];
        if (peer) {
            const struct touch_swipe_config *other = peer->dev->config;
            if (other->pair_window_ms != cfg->pair_window_ms ||
                other->pair_keycode != cfg->pair_keycode) {
                return -EINVAL;
            }
        }
        pair_slots[cfg->pair_slot] = data;
    }
    return 0;
}

static const struct zmk_input_processor_driver_api api = {.handle_event = observe_input};

#define CREATE_INSTANCE(n)                                                                         \
    BUILD_ASSERT(DT_INST_PROP(n, pair_slot) >= -1 && DT_INST_PROP(n, pair_slot) <= 1,             \
                 "pair-slot must be -1, 0 or 1");                                               \
    BUILD_ASSERT(DT_INST_PROP(n, pair_direction) == -1 || DT_INST_PROP(n, pair_direction) == 1,   \
                 "pair-direction must be -1 or 1");                                            \
    BUILD_ASSERT(DT_INST_PROP(n, pair_window_ms) > 0 &&                                         \
                 DT_INST_PROP(n, pair_window_ms) <= INT32_MAX, "invalid pair window");            \
    BUILD_ASSERT(DT_INST_PROP(n, pair_slot) < 0 || DT_INST_PROP(n, pair_keycode) != 0,            \
                 "paired swipe needs a keycode");                                               \
    BUILD_ASSERT(DT_INST_PROP(n, override_layer) >= -1 &&                                        \
                 DT_INST_PROP(n, override_layer) < ZMK_KEYMAP_LAYERS_LEN, "invalid override layer"); \
    BUILD_ASSERT(DT_INST_PROP(n, override_layer) < 0 ||                                          \
                 (DT_INST_PROP(n, override_left_keycode) && DT_INST_PROP(n, override_right_keycode)), \
                 "override layer requires both keycodes");                                     \
    BUILD_ASSERT(DT_INST_PROP(n, threshold) > 0 &&                                                 \
                 DT_INST_PROP(n, threshold) <= INT32_MAX, "threshold must be positive int32");   \
    BUILD_ASSERT(DT_INST_PROP(n, axis_ratio) > 0 &&                                                \
                 DT_INST_PROP(n, axis_ratio) <= INT32_MAX, "axis-ratio must be positive int32"); \
    BUILD_ASSERT(DT_INST_PROP(n, hwheel_code) != DT_INST_PROP(n, wheel_code),                      \
                 "horizontal and vertical codes must differ");                                   \
    static struct touch_swipe_data data_##n;                                                       \
    static const struct touch_swipe_config config_##n = {                                          \
        .hwheel_code = DT_INST_PROP(n, hwheel_code),                                               \
        .wheel_code = DT_INST_PROP(n, wheel_code),                                                 \
        .touch_code = DT_INST_PROP(n, touch_code),                                                 \
        .threshold = DT_INST_PROP(n, threshold),                                                   \
        .axis_ratio = DT_INST_PROP(n, axis_ratio),                                                 \
        .left_keycode = DT_INST_PROP(n, left_keycode),                                             \
        .right_keycode = DT_INST_PROP(n, right_keycode),                                           \
        .override_layer = DT_INST_PROP(n, override_layer),                                       \
        .override_left_keycode = DT_INST_PROP(n, override_left_keycode),                         \
        .pair_slot = DT_INST_PROP(n, pair_slot),                                               \
        .pair_direction = DT_INST_PROP(n, pair_direction),                                     \
        .pair_window_ms = DT_INST_PROP(n, pair_window_ms),                                     \
        .pair_keycode = DT_INST_PROP(n, pair_keycode),                                         \
        .override_right_keycode = DT_INST_PROP(n, override_right_keycode),                       \
    };                                                                                            \
    DEVICE_DT_INST_DEFINE(n, initialize, NULL, &data_##n, &config_##n, POST_KERNEL,                       \
                          CONFIG_KERNEL_INIT_PRIORITY_DEFAULT, &api);

DT_INST_FOREACH_STATUS_OKAY(CREATE_INSTANCE)
#endif
