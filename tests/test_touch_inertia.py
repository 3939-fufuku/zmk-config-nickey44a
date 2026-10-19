"""Run the production C processor with fake clock/work/HID APIs (no BLE simulation)."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'modules/zmk-input-processor-touch-inertia/src/input_processor_touch_inertia.c'
STUB = r'''
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#define DT_HAS_COMPAT_STATUS_OKAY(x) 1
#define IS_ENABLED(x) 0
#define DT_INST_FOREACH_STATUS_OKAY(x)
#define MAX(a,b) ((a) > (b) ? (a) : (b))
#define CLAMP(v,lo,hi) ((v) < (lo) ? (lo) : ((v) > (hi) ? (hi) : (v)))
#define ARG_UNUSED(x) (void)(x)
#define CONTAINER_OF(p,t,m) ((t *)((char *)(p) - offsetof(t,m)))
#define K_FOREVER 0
#define K_MSEC(x) (x)
#define INPUT_EV_KEY 1
#define INPUT_EV_REL 2
#define ZMK_INPUT_PROC_CONTINUE 0
#define MOD_LCTL 1
#define MOD_RCTL 16
struct device { void *data; const void *config; };
struct input_event { int type; int code; int32_t value; };
struct zmk_input_processor_state { int unused; };
struct zmk_input_processor_driver_api {
 int (*handle_event)(const struct device *, struct input_event *, uint32_t,
                     uint32_t, struct zmk_input_processor_state *);
};
struct k_work { int unused; };
struct k_work_delayable { struct k_work work; bool pending; };
struct k_mutex { int unused; };
static int64_t now_ms;
static int wheel, reports, deltas[2048];
static struct { struct { uint8_t modifiers; } body; } keyboard;
#define zmk_hid_get_keyboard_report() (&keyboard)
static int64_t k_uptime_get(void) { return now_ms; }
static void k_mutex_init(struct k_mutex *m) { (void)m; }
static void k_mutex_lock(struct k_mutex *m, int t) { (void)m; (void)t; }
static void k_mutex_unlock(struct k_mutex *m) { (void)m; }
static struct k_work_delayable *k_work_delayable_from_work(struct k_work *w) {
 return CONTAINER_OF(w, struct k_work_delayable, work);
}
static void k_work_cancel_delayable(struct k_work_delayable *w) { w->pending = false; }
static void k_work_reschedule(struct k_work_delayable *w, int t) {
 (void)t; w->pending = true;
}
static void k_work_init_delayable(struct k_work_delayable *w, void (*fn)(struct k_work *)) {
 (void)fn; w->pending = false;
}
static void zmk_hid_mouse_movement_set(int x, int y) { (void)x; (void)y; }
static void zmk_hid_mouse_scroll_set(int x, int y) { (void)x; wheel = y; }
static void zmk_endpoints_send_mouse_report(void) { deltas[reports++] = wheel; }
'''
HARNESS = r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
static struct touch_inertia_config cfg = {
 .wheel_code = 8, .touch_code = 330, .tick_ms = 10, .decay_permille = 920,
 .stop_threshold_q8 = 256, .launch_threshold_q8 = 384, .ema_new_permille = 250,
 .release_timeout_ms = 100, .cancel_scroll_inertia_on_ctrl = true,
};
static struct touch_inertia_data data;
static struct device dev = { .data = &data, .config = &cfg };
static void reset(void) {
 memset(&data, 0, sizeof(data)); now_ms = 1000; reports = 0; keyboard.body.modifiers = 0;
 initialize(&dev);
}
static void event(int type, int code, int value, int dt) {
 now_ms += dt;
 struct input_event e = {type, code, value};
 assert(observe_input(&dev, &e, 0, 0, NULL) == ZMK_INPUT_PROC_CONTINUE);
 assert(e.value == value); /* Manual scrolling is untouched. */
}
static void touch(int v, int dt) { event(INPUT_EV_KEY, 330, v, dt); }
static void scroll(int v, int dt) { event(INPUT_EV_REL, 8, v, dt); }
static void flick(int sign, int dt) {
 touch(1, 0);
 for (int i=0; i<8; ++i) scroll(sign * 8, dt);
}
static void tick(void) { now_ms += 10; inertia_tick(&data.inertia_work.work); }
int main(void) {
 for (int sign=-1; sign<=1; sign+=2) {
  reset(); flick(sign, 10); int32_t paced = data.velocity_q8;
  reset(); flick(sign, 0); assert(data.velocity_q8 == paced);
  reset(); flick(sign, 1); assert(data.velocity_q8 == paced);
  touch(0, 1); assert(data.inertia_running);
  int count=0;
  while (data.inertia_running) {
   int before=reports; tick();
   assert(reports == before+1); /* No empty ticks / spaced tail pulses. */
   assert(deltas[before] * sign > 0 && deltas[before] * sign <= 8);
   assert(++count < 100);
  }
  int done=reports; tick(); assert(reports == done);
  reset(); flick(sign, 10); touch(0, 101); assert(!data.inertia_running);
  reset(); flick(sign, 10); scroll(sign, 101); touch(0, 0);
  assert(!data.inertia_running); /* Old speed cannot survive a pause. */
  reset(); flick(sign, 10); scroll(-sign, 10);
  assert(data.velocity_q8 * sign < 0); touch(0, 0); assert(!data.inertia_running);
  reset(); flick(sign, 10); touch(0, 1); tick(); touch(1, 0);
  int stopped=reports; tick(); assert(reports == stopped && !data.inertia_running);
  for (int ctrl=MOD_LCTL; ctrl<=MOD_RCTL; ctrl+=MOD_RCTL-MOD_LCTL) {
   reset(); flick(sign, 10); touch(0, 1); keyboard.body.modifiers=ctrl;
   tick(); assert(!data.inertia_running && reports == 0);
   keyboard.body.modifiers=0; tick(); assert(reports == 0);
  }
 }
 reset(); touch(1, 0); touch(0, 10); assert(!data.inertia_running);
 reset(); touch(1, 0); event(INPUT_EV_REL, 6, 100, 10); touch(0, 0);
 assert(!data.inertia_running); /* Horizontal/swipe axis is ignored. */
 puts("PASS: burst timing, both directions, pause, reversal, continuous tail, re-touch, Ctrl, no-scroll");
}
'''

def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        headers = ['zephyr/device.h', 'zephyr/kernel.h', 'drivers/input_processor.h',
                   'dt-bindings/zmk/modifiers.h', 'zmk/hid.h', 'zmk/endpoints.h']
        (root / 'stub.h').write_text(STUB)
        for header in headers:
            dest = root / header
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text('#include "stub.h"\n')
        source = root / 'test.c'
        source.write_text('#include "' + str(SOURCE) + '"\n' + HARNESS)
        exe = root / 'test'
        subprocess.run([os.environ.get('CC', 'cc'), '-std=c11', '-Wall', '-Wextra',
                        '-Wno-unused-const-variable', '-fsanitize=undefined', '-I', str(root),
                        str(source), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)

if __name__ == '__main__':
    main()
