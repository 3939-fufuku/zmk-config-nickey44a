"""Exercise the production C processor with a deterministic clock/work queue and key sink.

This is a state-machine regression test, not a BLE, TPS43, or macOS simulation.
"""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'modules/zmk-input-processor-touch-swipe/src/input_processor_touch_swipe.c'
STUB = r'''
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <assert.h>
#define DT_HAS_COMPAT_STATUS_OKAY(x) 1
#define IS_ENABLED(x) 0
#define DT_INST_FOREACH_STATUS_OKAY(x)
#define CLAMP(v,lo,hi) ((v)<(lo)?(lo):((v)>(hi)?(hi):(v)))
#define ARG_UNUSED(x) (void)(x)
#define CONTAINER_OF(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define K_FOREVER 0
#define K_MSEC(x) (x)
#define INPUT_EV_KEY 1
#define INPUT_EV_REL 2
#define ZMK_INPUT_PROC_CONTINUE 0
#define ZMK_INPUT_PROC_STOP 1
#define LOG_MODULE_DECLARE(...)
#define LOG_WRN(...)
#define LOG_DBG(...)
struct device { void *data; const void *config; };
struct input_event { int type; int code; int32_t value; bool sync; };
struct zmk_input_processor_state { int unused; };
struct zmk_input_processor_driver_api {
 int (*handle_event)(const struct device *,struct input_event *,uint32_t,uint32_t,
                     struct zmk_input_processor_state *);
};
struct k_work { int unused; };
struct k_work_delayable { struct k_work work; bool pending; int64_t due; };
struct k_mutex { bool held; };
#define K_MUTEX_DEFINE(name) static struct k_mutex name
static int64_t now_ms;
static bool layer1;
static uint32_t keys[128];
static bool pressed[128];
static int emitted;
static int64_t k_uptime_get(void) { return now_ms; }
static bool zmk_keymap_layer_active(int layer) { return layer==1 && layer1; }
static int raise_zmk_keycode_state_changed_from_encoded(uint32_t k,bool p,int64_t ts) {
 (void)ts; assert(emitted<128); keys[emitted]=k; pressed[emitted++]=p; return 0;
}
static void k_mutex_lock(struct k_mutex *m,int t) { (void)t; assert(!m->held);m->held=true; }
static void k_mutex_unlock(struct k_mutex *m) { assert(m->held);m->held=false; }
static struct k_work_delayable *k_work_delayable_from_work(struct k_work *w) {
 return CONTAINER_OF(w,struct k_work_delayable,work);
}
static void k_work_cancel_delayable(struct k_work_delayable *w) { w->pending=false; }
static void k_work_reschedule(struct k_work_delayable *w,int64_t dt) {
 w->pending=true; w->due=now_ms+dt;
}
static void k_work_init_delayable(struct k_work_delayable *w,void (*fn)(struct k_work *)) {
 (void)fn; w->pending=false;
}
'''
HARNESS = r'''
#include <stdio.h>
#include <string.h>
enum { LEFT=0, RIGHT=1, TOUCH=330, MISSION=0x01070052,
       L_IN=0x0107002b, L_OUT=0x0307002b, R_OUT=0x000c0224, R_IN=0x000c0225 };
static struct touch_swipe_config cfg[2];
static struct touch_swipe_data pads[2];
static struct device dev[2];
static void setup(void) {
 now_ms=1000; emitted=0; layer1=false; pair_latched=false;
 memset(pads,0,sizeof(pads)); memset(pair_slots,0,sizeof(pair_slots));
 for (int i=0;i<2;++i) {
  cfg[i]=(struct touch_swipe_config){
   .hwheel_code=i==LEFT?7:6, .wheel_code=i==LEFT?6:8, .touch_code=TOUCH,
   .threshold=8, .axis_ratio=2, .left_keycode=i==LEFT?L_IN:R_OUT,
   .right_keycode=i==LEFT?L_OUT:R_IN, .override_layer=i==LEFT?-1:1,
   .override_left_keycode=L_IN, .override_right_keycode=L_OUT,
   .pair_slot=i, .pair_direction=i==LEFT?1:-1, .pair_window_ms=250,
   .pair_keycode=MISSION,
  };
  dev[i]=(struct device){.data=&pads[i],.config=&cfg[i]};
  assert(initialize(&dev[i])==0);
 }
}
static void e(int pad,int type,int code,int value,int expected) {
 struct input_event event={type,code,value,true};
 assert(observe_input(&dev[pad],&event,0,0,NULL)==expected);
 assert(event.value==value && event.code==code && event.sync);
}
static void touch(int pad,int v) { e(pad,INPUT_EV_KEY,TOUCH,v,ZMK_INPUT_PROC_CONTINUE); }
static void h(int pad,int value) { e(pad,INPUT_EV_REL,cfg[pad].hwheel_code,value,ZMK_INPUT_PROC_STOP); }
static void v(int pad,int value) { e(pad,INPUT_EV_REL,cfg[pad].wheel_code,value,ZMK_INPUT_PROC_CONTINUE); }
static void outward(int pad) { h(pad,pad==LEFT?8:-8); }
static void inward(int pad) { h(pad,pad==LEFT?-8:8); }
static void pump(void) {
 for (int i=0;i<2;++i) if(pads[i].pending_work.pending && pads[i].pending_work.due<=now_ms) {
  pads[i].pending_work.pending=false;
  pending_timeout(&pads[i].pending_work.work);
 }
}
static void advance(int ms) { now_ms+=ms; pump(); }
static void tap(int index,uint32_t key) {
 assert(keys[2*index]==key && pressed[2*index]);
 assert(keys[2*index+1]==key && !pressed[2*index+1]);
}
static void both_down(void) { touch(LEFT,1); touch(RIGHT,1); }
int main(void) {
 /* Either arrival order, with BLE-like skew, must emit only Ctrl+Up. */
 for(int first=0;first<2;++first) for(int delay=0;delay<=249;delay+=83) {
  setup(); both_down(); outward(first); assert(emitted==0);
  advance(delay); outward(1-first); assert(emitted==2); tap(0,MISSION);
  advance(1000); outward(LEFT); outward(RIGHT); assert(emitted==2);
  /* A queued cancelled callback cannot emit the old normal shortcut. */
  pending_timeout(&pads[first].pending_work.work); assert(emitted==2);
  touch(LEFT,0); touch(LEFT,1); outward(LEFT); assert(emitted==2);
  touch(LEFT,0); touch(RIGHT,0); both_down();
  outward(RIGHT); outward(LEFT); assert(emitted==4); tap(1,MISSION);
 }
 /* Single outward: delay, then original behavior exactly once. */
 for(int pad=0;pad<2;++pad) {
  setup(); touch(pad,1); outward(pad); advance(249); assert(emitted==0);
  advance(1); assert(emitted==2); tap(0,pad==LEFT?L_OUT:R_OUT);
  touch(pad,0); advance(1000); assert(emitted==2);
  /* Lifting early commits single-pad fallback and prevents later pairing. */
  setup(); both_down(); outward(pad); touch(pad,0);
  assert(emitted==2); outward(1-pad); advance(250); assert(emitted==4);
  tap(0,pad==LEFT?L_OUT:R_OUT); tap(1,pad==LEFT?R_OUT:L_OUT);
 }
 /* Expiration is checked in the input path even if workqueue was stalled. */
 for(int delay=250;delay<=251;++delay) {
  setup(); both_down(); outward(LEFT); now_ms+=delay; outward(RIGHT);
  assert(emitted==2); tap(0,L_OUT); advance(250); assert(emitted==4); tap(1,R_OUT);
 }
 /* Closing, same physical direction, tiny motion, and vertical-heavy scroll don't pair. */
 setup(); both_down(); inward(LEFT); inward(RIGHT);
 assert(emitted==4); tap(0,L_IN); tap(1,R_IN);
 setup(); both_down(); outward(LEFT); inward(RIGHT); advance(250);
 assert(emitted==4); tap(0,R_IN); tap(1,L_OUT);
 setup(); both_down(); h(LEFT,7); h(RIGHT,-7); advance(1000); assert(emitted==0);
 setup(); both_down(); v(LEFT,5);v(LEFT,-5);v(RIGHT,5);v(RIGHT,-5);
 outward(LEFT);outward(RIGHT);advance(1000);assert(emitted==0);
 /* Layer choice is frozen when recognized, combo works in layer 1. */
 setup(); layer1=true; touch(RIGHT,1); outward(RIGHT); layer1=false;
 advance(250); assert(emitted==2);tap(0,L_IN);
 setup(); layer1=true; both_down();outward(LEFT);outward(RIGHT);tap(0,MISSION);
 /* Touch-only, pointer, button and normal scroll pass unchanged. */
 setup(); both_down();v(LEFT,10);v(RIGHT,-10);
 e(LEFT,INPUT_EV_KEY,272,1,0);e(RIGHT,INPUT_EV_REL,0,10,0);
 touch(LEFT,0);touch(RIGHT,0);h(LEFT,100);h(RIGHT,-100);assert(emitted==0);
 /* Duplicate touch down must neither reset distance nor restart a candidate. */
 setup();both_down();h(LEFT,4);touch(LEFT,1);h(LEFT,4);advance(100);
 touch(LEFT,1);outward(RIGHT);assert(emitted==2);tap(0,MISSION);
 /* Old queued callback during a new touch must honor the new deadline. */
 setup();touch(LEFT,1);outward(LEFT);touch(LEFT,0);advance(10);
 touch(LEFT,1);outward(LEFT);pending_timeout(&pads[LEFT].pending_work.work);
 assert(emitted==2 && pads[LEFT].pending);advance(250);assert(emitted==4);
 /* Disabled pairing preserves immediate, independent behavior. */
 setup();cfg[LEFT].pair_slot=-1;touch(LEFT,1);outward(LEFT);
 assert(emitted==2);tap(0,L_OUT);
 /* Extreme signed values are safe under UBSan, without negative magnitude overflow. */
 setup();both_down();v(LEFT,INT32_MIN);v(RIGHT,INT32_MAX);
 h(LEFT,INT32_MAX);h(RIGHT,INT32_MIN);assert(emitted==0);
 /* Reject duplicate slot configuration. */
 assert(initialize(&dev[LEFT])==-EINVAL);
 puts("PASS: paired/open-only, timing boundaries, fallback, once-per-touch, release/rearm, layer, passthrough, stale work, integer limits");
}
'''

def main():
    # Both DTS roots must expose the new properties; inertia must remain disabled.
    binding = ROOT / 'modules/zmk-input-processor-touch-swipe/dts/bindings/input/processor/input-processor-touch-swipe.yaml'
    mirror = ROOT / 'boards/shields/nickey44a/dts/bindings/input/processor/input-processor-touch-swipe.yaml'
    assert mirror.read_text().split('\n', 1)[1] == binding.read_text()
    overlay = (ROOT / 'boards/shields/nickey44a/nickey44a_r.overlay').read_text()
    assert 'touch_inertia' not in overlay
    assert overlay.count('pair-keycode = <LC(UP)>;') == 2
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'stub.h').write_text(STUB)
        for header in ['zephyr/device.h','zephyr/kernel.h','drivers/input_processor.h',
                       'zmk/events/keycode_state_changed.h','zmk/keymap.h','zephyr/logging/log.h']:
            p = root / header
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('#include "stub.h"\n')
        source = root / 'test.c'
        source.write_text('#include "' + str(SOURCE) + '"\n' + HARNESS)
        exe = root / 'test'
        subprocess.run([os.environ.get('CC','cc'),'-std=c11','-Wall','-Wextra',
                        '-Werror','-Wno-unused-const-variable','-fsanitize=undefined',
                        '-I',str(root),str(source),'-o',str(exe)],check=True)
        subprocess.run([str(exe)],check=True)

if __name__ == '__main__':
    main()
