/* SPDX-License-Identifier: MIT */
#define DT_DRV_COMPAT zmk_input_processor_touch_inertia
#include <zephyr/device.h>
#include <zephyr/kernel.h>

#if DT_HAS_COMPAT_STATUS_OKAY(DT_DRV_COMPAT)
#if IS_ENABLED(CONFIG_ZMK_SPLIT) && !IS_ENABLED(CONFIG_ZMK_SPLIT_ROLE_CENTRAL)
#error "touch-inertia requires a central-side devicetree instance"
#endif

#include <limits.h>
#include <drivers/input_processor.h>
#include <dt-bindings/zmk/modifiers.h>
#include <zmk/hid.h>
#include <zmk/endpoints.h>

#define Q8_SHIFT 8
#define Q8_ONE (1 << Q8_SHIFT)

struct touch_inertia_config {
    uint16_t wheel_code;
    uint16_t touch_code;
    int32_t tick_ms;
    int32_t decay_permille;
    int32_t stop_threshold_q8;
    int32_t launch_threshold_q8;
    int32_t ema_new_permille;
    int32_t release_timeout_ms;
    bool cancel_scroll_inertia_on_ctrl;
};

struct touch_inertia_data {
    const struct device *dev;
    bool touching;
    bool scroll_seen;
    bool inertia_running;
    int32_t velocity_q8;
    int32_t remainder_q8;
    int64_t last_wheel_ms;
    struct k_work_delayable inertia_work;
    struct k_mutex lock;
};

static int32_t bounded_velocity(int64_t value) {
    return CLAMP(value, (int64_t)INT32_MIN, (int64_t)INT32_MAX);
}

static bool ctrl_mod_is_active(const struct touch_inertia_config *cfg) {
    return cfg->cancel_scroll_inertia_on_ctrl &&
           (zmk_hid_get_keyboard_report()->body.modifiers & (MOD_LCTL | MOD_RCTL)) != 0;
}

static bool below_threshold(const struct touch_inertia_data *data,
                            const struct touch_inertia_config *cfg) {
    return data->velocity_q8 > -cfg->stop_threshold_q8 &&
           data->velocity_q8 < cfg->stop_threshold_q8;
}

static bool above_launch_threshold(const struct touch_inertia_data *data,
                                   const struct touch_inertia_config *cfg) {
    return data->velocity_q8 >= cfg->launch_threshold_q8 ||
           data->velocity_q8 <= -cfg->launch_threshold_q8;
}

/* Called with the instance lock held, including from the work handler. */
static void clear_scroll(struct touch_inertia_data *data) {
    data->inertia_running = false;
    k_work_cancel_delayable(&data->inertia_work);
    data->velocity_q8 = 0;
    data->remainder_q8 = 0;
    data->last_wheel_ms = 0;
    data->scroll_seen = false;
}

static void inertia_tick(struct k_work *work) {
    struct touch_inertia_data *data = CONTAINER_OF(
        k_work_delayable_from_work(work), struct touch_inertia_data, inertia_work);
    const struct touch_inertia_config *cfg = data->dev->config;
    k_mutex_lock(&data->lock, K_FOREVER);
    if (!data->inertia_running) {
        goto done;
    }
    if (ctrl_mod_is_active(cfg) || data->touching || below_threshold(data, cfg)) {
        clear_scroll(data);
        goto done;
    }

    int64_t accumulated = (int64_t)data->remainder_q8 + data->velocity_q8;
    /* Keep reports within the signed 8-bit wheel range; retain excess. */
    int32_t wheel_delta = CLAMP(accumulated / Q8_ONE, -127, 127);
    data->remainder_q8 = bounded_velocity(accumulated - wheel_delta * Q8_ONE);
    if (wheel_delta != 0) {
        zmk_hid_mouse_movement_set(0, 0);
        zmk_hid_mouse_scroll_set(0, wheel_delta);
#ifdef ZMK_ENDPOINT_NONE_COUNT
        zmk_endpoint_send_mouse_report();
#else
        zmk_endpoints_send_mouse_report();
#endif
        zmk_hid_mouse_scroll_set(0, 0);
    }
    data->velocity_q8 = (int64_t)data->velocity_q8 * cfg->decay_permille / 1000;
    if (below_threshold(data, cfg)) {
        clear_scroll(data);
    } else {
        k_work_reschedule(&data->inertia_work, K_MSEC(cfg->tick_ms));
    }
done:
    k_mutex_unlock(&data->lock);
}

static int observe_input(const struct device *dev, struct input_event *event,
                         uint32_t param1, uint32_t param2,
                         struct zmk_input_processor_state *state) {
    ARG_UNUSED(param1);
    ARG_UNUSED(param2);
    ARG_UNUSED(state);
    struct touch_inertia_data *data = dev->data;
    const struct touch_inertia_config *cfg = dev->config;
    if (!((event->type == INPUT_EV_KEY && event->code == cfg->touch_code) ||
          (event->type == INPUT_EV_REL && event->code == cfg->wheel_code))) {
        return ZMK_INPUT_PROC_CONTINUE;
    }

    /* Serialize reset and report emission: no old tick can run after touch-down. */
    k_mutex_lock(&data->lock, K_FOREVER);
    bool ctrl_active = ctrl_mod_is_active(cfg);
    if (ctrl_active) {
        /* Manual Ctrl+wheel still passes through; discard its launch velocity. */
        clear_scroll(data);
    }
    if (event->type == INPUT_EV_KEY) {
        if (event->value != 0) {
            clear_scroll(data);
            data->touching = true;
        } else if (data->touching) {
            data->touching = false;
            if (data->scroll_seen &&
                k_uptime_get() - data->last_wheel_ms <= cfg->release_timeout_ms &&
                above_launch_threshold(data, cfg)) {
                data->inertia_running = true;
                data->remainder_q8 = 0;
                k_work_reschedule(&data->inertia_work, K_MSEC(cfg->tick_ms));
            } else {
                clear_scroll(data);
            }
        }
    } else {
        if (data->inertia_running) {
            clear_scroll(data);
        }
        if (!ctrl_active && event->value != 0 && data->touching) {
            int64_t now = k_uptime_get();
            /* Multiplication, rather than a signed left shift, also handles negatives. */
            int64_t sample = (int64_t)event->value * Q8_ONE;
            /* A pause or reversal must not carry an old flick into this gesture. */
            if (data->scroll_seen &&
                (now - data->last_wheel_ms > cfg->release_timeout_ms ||
                 (event->value > 0 && data->velocity_q8 < 0) ||
                 (event->value < 0 && data->velocity_q8 > 0))) {
                clear_scroll(data);
            }
            if (data->scroll_seen) {
                /* Queued samples can arrive together; never amplify a delta
                 * because its delivery interval is shorter than one tick. */
                int64_t dt = MAX((int64_t)cfg->tick_ms, now - data->last_wheel_ms);
                sample = sample * cfg->tick_ms / dt;
            }
            int32_t sample_q8 = bounded_velocity(sample);
            data->velocity_q8 = bounded_velocity(
                ((int64_t)data->velocity_q8 * (1000 - cfg->ema_new_permille) +
                 (int64_t)sample_q8 * cfg->ema_new_permille) / 1000);
            data->last_wheel_ms = now;
            data->scroll_seen = true;
        }
    }
    k_mutex_unlock(&data->lock);
    return ZMK_INPUT_PROC_CONTINUE;
}

static int initialize(const struct device *dev) {
    struct touch_inertia_data *data = dev->data;
    data->dev = dev;
    k_mutex_init(&data->lock);
    k_work_init_delayable(&data->inertia_work, inertia_tick);
    return 0;
}

static const struct zmk_input_processor_driver_api api = {.handle_event = observe_input};

#define CREATE_INSTANCE(n)                                                                        \
    BUILD_ASSERT(DT_INST_PROP(n, release_timeout_ms) > 0 &&                                      \
                 DT_INST_PROP(n, release_timeout_ms) <= INT32_MAX,                              \
                 "release timeout must be positive and fit int32_t");                           \
    BUILD_ASSERT(DT_INST_PROP(n, tick_ms) >= 0 && DT_INST_PROP(n, tick_ms) <= INT32_MAX / Q8_ONE,    \
                 "tick-ms exceeds the safe sample arithmetic range");                            \
    BUILD_ASSERT(DT_INST_PROP(n, decay_permille) >= 0 && DT_INST_PROP(n, decay_permille) < 1000,     \
                 "decay-permille must be 0..999");                                                \
    BUILD_ASSERT(DT_INST_PROP(n, stop_threshold_q8) > 0 &&                                         \
                 DT_INST_PROP(n, stop_threshold_q8) <= INT32_MAX, "stop threshold must be positive"); \
    BUILD_ASSERT(DT_INST_PROP(n, ema_new_permille) >= 0 &&                                         \
                 DT_INST_PROP(n, ema_new_permille) <= 1000, "EMA weight must be 0..1000");         \
    BUILD_ASSERT(DT_INST_PROP(n, launch_threshold_q8) > 0 &&                                      \
                 DT_INST_PROP(n, launch_threshold_q8) <= INT32_MAX,                             \
                 "launch threshold must be positive and fit int32_t");                         \
    BUILD_ASSERT(DT_INST_PROP(n, launch_threshold_q8) >= DT_INST_PROP(n, stop_threshold_q8),      \
                 "launch threshold must be at least the stop threshold");                      \
    static struct touch_inertia_data data_##n;                                                    \
    static const struct touch_inertia_config config_##n = {                                       \
        .wheel_code = DT_INST_PROP(n, wheel_code),                                                \
        .touch_code = DT_INST_PROP(n, touch_code),                                                \
        .tick_ms = MAX(1, DT_INST_PROP(n, tick_ms)),                                               \
        .decay_permille = DT_INST_PROP(n, decay_permille),                                         \
        .stop_threshold_q8 = DT_INST_PROP(n, stop_threshold_q8),                                   \
        .launch_threshold_q8 = DT_INST_PROP(n, launch_threshold_q8),                               \
        .ema_new_permille = DT_INST_PROP(n, ema_new_permille),                                     \
        .release_timeout_ms = DT_INST_PROP(n, release_timeout_ms),                                \
        .cancel_scroll_inertia_on_ctrl = DT_INST_PROP(n, cancel_scroll_inertia_on_ctrl),           \
    };                                                                                           \
    DEVICE_DT_INST_DEFINE(n, initialize, NULL, &data_##n, &config_##n, POST_KERNEL,                 \
                          CONFIG_KERNEL_INIT_PRIORITY_DEFAULT, &api);

DT_INST_FOREACH_STATUS_OKAY(CREATE_INSTANCE)
#endif
