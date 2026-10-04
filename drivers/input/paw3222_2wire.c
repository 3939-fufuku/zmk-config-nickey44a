/*
 * Copyright 2025 sekigon-gonnoc
 * SPDX-License-Identifier: MIT
 *
 * Two-wire timing and initialization based on sekigon-gonnoc's MIT-licensed
 * small-mouse-sensor-module/sample_sketch/sample_sketch.ino.
 * CS is physically grounded; each transaction contains exactly 16 clocks.
 */

#define DT_DRV_COMPAT pixart_paw3222_2wire

#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/input/input.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/util.h>

LOG_MODULE_REGISTER(paw3222_2wire, CONFIG_ZMK_LOG_LEVEL);

#define REG_PRODUCT_ID 0x00
#define REG_MOTION 0x02
#define REG_DELTA_X 0x03
#define REG_DELTA_Y 0x04
#define REG_OPERATION 0x05
#define REG_CONFIG 0x06
#define REG_WRITE_PROTECT 0x09
#define REG_CPI_X 0x0d
#define REG_CPI_Y 0x0e
#define REG_DELTA_XY_HI 0x12
#define MOTION_PENDING BIT(7)

struct paw_config {
    struct gpio_dt_spec sclk;
    struct gpio_dt_spec sdio;
    struct gpio_dt_spec motion;
    uint16_t cpi;
};

struct paw_data {
    const struct device *dev;
    struct gpio_callback callback;
    struct k_work_delayable work;
};

/* Idle high, data sampled on the rising edge. At most 500 kHz; GPIO API
 * overhead lowers this further. Keep interrupts out of a single 16-bit
 * transfer so a long clock pause cannot reset the CS-less serial interface.
 * All three GPIOs are on the local nRF52840, never an I/O expander.
 */
static void paw_write_byte(const struct paw_config *cfg, uint8_t value) {
    for (int bit = 7; bit >= 0; bit--) {
        gpio_pin_set_dt(&cfg->sdio, (value >> bit) & 1);
        gpio_pin_set_dt(&cfg->sclk, 0);
        k_busy_wait(1);
        gpio_pin_set_dt(&cfg->sclk, 1);
        k_busy_wait(1);
    }
}

static int paw_read(const struct device *dev, uint8_t reg, uint8_t *value) {
    const struct paw_config *cfg = dev->config;
    unsigned int key = irq_lock();
    int ret = gpio_pin_configure_dt(&cfg->sdio, GPIO_OUTPUT_HIGH);
    uint8_t result = 0;

    if (ret == 0) {
        paw_write_byte(cfg, reg & 0x7f);
        ret = gpio_pin_configure_dt(&cfg->sdio, GPIO_INPUT);
        if (ret == 0) {
            k_busy_wait(1);
            for (int bit = 7; bit >= 0; bit--) {
                gpio_pin_set_dt(&cfg->sclk, 0);
                k_busy_wait(1);
                gpio_pin_set_dt(&cfg->sclk, 1);
                result |= gpio_pin_get_dt(&cfg->sdio) << bit;
                k_busy_wait(1);
            }
        }
        /* The sensor releases SDIO after its last data bit. */
        int restore = gpio_pin_configure_dt(&cfg->sdio, GPIO_OUTPUT_HIGH);
        if (ret == 0) {
            ret = restore;
        }
    }
    irq_unlock(key);
    *value = result;
    return ret;
}

static int paw_write(const struct device *dev, uint8_t reg, uint8_t value) {
    const struct paw_config *cfg = dev->config;
    unsigned int key = irq_lock();
    int ret = gpio_pin_configure_dt(&cfg->sdio, GPIO_OUTPUT_HIGH);

    if (ret == 0) {
        paw_write_byte(cfg, reg | 0x80);
        paw_write_byte(cfg, value);
        gpio_pin_set_dt(&cfg->sdio, 1);
    }
    irq_unlock(key);
    return ret;
}

static void paw_work(struct k_work *work) {
    struct paw_data *data = CONTAINER_OF(k_work_delayable_from_work(work), struct paw_data, work);
    const struct paw_config *cfg = data->dev->config;
    uint8_t motion, x, y;
    int ret = paw_read(data->dev, REG_MOTION, &motion);

    if (ret == 0 && (motion & MOTION_PENDING)) {
        ret = paw_read(data->dev, REG_DELTA_X, &x);
        if (ret == 0) {
            ret = paw_read(data->dev, REG_DELTA_Y, &y);
        }
        if (ret == 0) {
            input_report_rel(data->dev, INPUT_REL_X, (int8_t)x, false, K_FOREVER);
            input_report_rel(data->dev, INPUT_REL_Y, (int8_t)y, true, K_FOREVER);
        }
    }

    /* IRQ stays enabled. Poll while active because MOTION can remain low
     * across several reports; an edge alone would then miss movement.
     */
    if (ret < 0 || gpio_pin_get_dt(&cfg->motion) > 0 ||
        (ret == 0 && (motion & MOTION_PENDING))) {
        k_work_schedule(&data->work, K_MSEC(8));
    }
}

static void paw_motion(const struct device *port, struct gpio_callback *cb, uint32_t pins) {
    struct paw_data *data = CONTAINER_OF(cb, struct paw_data, callback);
    ARG_UNUSED(port);
    ARG_UNUSED(pins);
    k_work_schedule(&data->work, K_NO_WAIT);
}

static int paw_init(const struct device *dev) {
    const struct paw_config *cfg = dev->config;
    struct paw_data *data = dev->data;
    uint8_t value;
    int ret;

    if (!gpio_is_ready_dt(&cfg->sclk) || !gpio_is_ready_dt(&cfg->sdio) ||
        !gpio_is_ready_dt(&cfg->motion)) {
        return -ENODEV;
    }
    ret = gpio_pin_configure_dt(&cfg->sclk, GPIO_OUTPUT_HIGH);
    if (ret < 0) { return ret; }
    ret = gpio_pin_configure_dt(&cfg->sdio, GPIO_OUTPUT_HIGH);
    if (ret < 0) { return ret; }
    ret = gpio_pin_configure_dt(&cfg->motion, GPIO_INPUT);
    if (ret < 0) { return ret; }

    /* Recover framing even after an MCU-only reset with the sensor powered.
     * This clock-low reset sequence is from the manufacturer's sample.
     */
    gpio_pin_set_dt(&cfg->sclk, 0);
    k_sleep(K_MSEC(1));
    gpio_pin_set_dt(&cfg->sclk, 1);
    k_sleep(K_MSEC(35));

    for (int attempt = 0; attempt < 10; attempt++) {
        ret = paw_read(dev, REG_PRODUCT_ID, &value);
        if (ret == 0 && value == 0x30) { break; }
        if (attempt == 9) {
            LOG_ERR("PAW3222 not found (id=0x%02x, err=%d); check power and wiring", value, ret);
            return ret < 0 ? ret : -ENODEV;
        }
        k_sleep(K_MSEC(35));
    }

    /* Manufacturer's reset / wake sequence, preserving the default 8-bit mode. */
    ret = paw_write(dev, REG_CONFIG, 0xd1);
    if (ret < 0) { return ret; }
    k_sleep(K_MSEC(35));
    ret = paw_write(dev, REG_CONFIG, 0x11);
    if (ret < 0) { return ret; }
    k_sleep(K_MSEC(35));
    ret = paw_write(dev, REG_OPERATION, 0xb9);
    if (ret < 0) { return ret; }
    k_sleep(K_MSEC(35));

    ret = paw_write(dev, REG_WRITE_PROTECT, 0x5a);
    if (ret < 0) { return ret; }
    ret = paw_write(dev, REG_CPI_X, cfg->cpi / 38);
    if (ret < 0) { return ret; }
    ret = paw_write(dev, REG_CPI_Y, cfg->cpi / 38);
    if (ret < 0) { return ret; }
    ret = paw_write(dev, REG_WRITE_PROTECT, 0x00);
    if (ret < 0) { return ret; }

    const uint8_t clear_regs[] = {REG_MOTION, REG_DELTA_X, REG_DELTA_Y, REG_DELTA_XY_HI};
    for (size_t i = 0; i < ARRAY_SIZE(clear_regs); i++) {
        ret = paw_read(dev, clear_regs[i], &value);
        if (ret < 0) { return ret; }
    }

    data->dev = dev;
    k_work_init_delayable(&data->work, paw_work);
    gpio_init_callback(&data->callback, paw_motion, BIT(cfg->motion.pin));
    ret = gpio_add_callback_dt(&cfg->motion, &data->callback);
    if (ret < 0) { return ret; }
    ret = gpio_pin_interrupt_configure_dt(&cfg->motion, GPIO_INT_EDGE_TO_ACTIVE);
    if (ret < 0) {
        gpio_remove_callback(cfg->motion.port, &data->callback);
        return ret;
    }
    /* Also service MOTION if it was already low before enabling the IRQ. */
    k_work_schedule(&data->work, K_NO_WAIT);
    LOG_INF("PAW3222 ready: %u CPI, CS grounded", cfg->cpi);
    return 0;
}

#define PAW_DEFINE(n)                                                                               \
    BUILD_ASSERT(DT_INST_PROP(n, res_cpi) >= 608 && DT_INST_PROP(n, res_cpi) <= 4826 &&                \
                     DT_INST_PROP(n, res_cpi) % 38 == 0, "CPI must be 608..4826 in steps of 38");     \
    static const struct paw_config config_##n = {                                                   \
        .sclk = GPIO_DT_SPEC_INST_GET(n, sclk_gpios),                                                \
        .sdio = GPIO_DT_SPEC_INST_GET(n, sdio_gpios),                                                \
        .motion = GPIO_DT_SPEC_INST_GET(n, motion_gpios),                                            \
        .cpi = DT_INST_PROP(n, res_cpi),                                                            \
    };                                                                                             \
    static struct paw_data data_##n;                                                               \
    DEVICE_DT_INST_DEFINE(n, paw_init, NULL, &data_##n, &config_##n, POST_KERNEL,                     \
                          CONFIG_INPUT_INIT_PRIORITY, NULL);

DT_INST_FOREACH_STATUS_OKAY(PAW_DEFINE)
