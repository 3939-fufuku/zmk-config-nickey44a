"""Compile the actual GPIO transport against a simulated sensor (Python 3 + cc).

Checks framing, bit order, SDIO release during reads, idle levels and IRQ locks.
This is a logic test, not a substitute for measurements on the physical sensor.
"""
import os
from pathlib import Path
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / 'drivers/input/paw3222_2wire.c').read_text()
transport = source[source.index('static void paw_write_byte('):source.index('static void paw_work(')]
prelude = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
struct gpio_dt_spec { int pin; };
struct paw_config { struct gpio_dt_spec sclk, sdio, motion; uint16_t cpi; };
struct device { const void *config; };
#define GPIO_OUTPUT_HIGH 1
#define GPIO_INPUT 2
static int locked, edges, sclk, sdio, direction, sensor_value, is_read, fail_config;
static int sent;
static unsigned int irq_lock(void) { assert(!locked); locked = 1; return 42; }
static void irq_unlock(unsigned int key) { assert(locked && key == 42); locked = 0; }
static void k_busy_wait(int us) { assert(locked && us >= 1); }
static int gpio_pin_configure_dt(const struct gpio_dt_spec *pin, int flags) {
    assert(locked && pin->pin == 1);
    if (fail_config) { return -5; }
    if (flags == GPIO_INPUT) { assert(is_read && edges == 8); }
    else { assert(edges == 0 || edges == 16); sdio = 1; }
    direction = flags;
    return 0;
}
static int gpio_pin_set_dt(const struct gpio_dt_spec *pin, int value) {
    assert(locked && (value == 0 || value == 1));
    if (pin->pin == 1) {
        assert(direction == GPIO_OUTPUT_HIGH);
        sdio = value;
    } else {
        assert(pin->pin == 0 && sclk != value);
        sclk = value;
        if (value) {
            if (edges < 8 || !is_read) {
                assert(direction == GPIO_OUTPUT_HIGH);
                sent = (sent << 1) | sdio;
            } else { assert(direction == GPIO_INPUT); }
            edges++;
            assert(edges <= 16);
        }
    }
    return 0;
}
static int gpio_pin_get_dt(const struct gpio_dt_spec *pin) {
    assert(locked && pin->pin == 1 && direction == GPIO_INPUT);
    assert(sclk == 1 && edges >= 9 && edges <= 16);
    return (sensor_value >> (16 - edges)) & 1;
}
static void reset(int read, int value) {
    locked = edges = sent = fail_config = 0;
    sclk = sdio = 1;
    direction = GPIO_OUTPUT_HIGH;
    is_read = read;
    sensor_value = value;
}
'''
main = r'''
int main(void) {
    const struct paw_config cfg = {.sclk = {0}, .sdio = {1}, .motion = {2}};
    const struct device dev = {.config = &cfg};
    uint8_t value;
    for (int reg = 0; reg < 128; reg++) {
        for (int v = 0; v < 256; v++) {
            reset(1, v);
            assert(paw_read(&dev, reg, &value) == 0);
            assert(value == v && sent == reg && edges == 16);
            assert(!locked && sclk == 1 && sdio == 1 && direction == GPIO_OUTPUT_HIGH);
            reset(0, 0);
            assert(paw_write(&dev, reg, v) == 0);
            assert(sent == (((reg | 0x80) << 8) | v) && edges == 16);
            assert(!locked && sclk == 1 && sdio == 1 && direction == GPIO_OUTPUT_HIGH);
        }
    }
    reset(1, 0); fail_config = 1;
    assert(paw_read(&dev, 0, &value) == -5 && !locked && edges == 0);
    reset(0, 0); fail_config = 1;
    assert(paw_write(&dev, 0, 0) == -5 && !locked && edges == 0);
    puts("PASS: 65,536 read/write transactions; framing, SDIO direction, bit order, idle and error unlock");
}
'''
with tempfile.TemporaryDirectory() as tmp:
    c = Path(tmp) / 'transport.c'
    exe = Path(tmp) / 'transport'
    c.write_text(prelude + transport + main)
    subprocess.run([os.environ.get('CC', 'cc'), '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=undefined,address', str(c), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
