"""
macro_api.py — helper utilities for macros running inside Macro Manager.

HOW TO WRITE A MACRO
---------------------
Drop a .py file into the `macros/` folder. It must define:

    def run(stop_event):
        ...

`stop_event` is a threading.Event that gets set when the user hits Stop
(or presses the hotkey again, or closes the app). Your macro is responsible
for checking it periodically so it can stop cleanly — use the helpers below
instead of raw time.sleep() so your macro actually responds to Stop.

Anything you `print()` inside your macro shows up in the app's log panel.

MINIMAL EXAMPLE
----------------
    from macro_api import sleep

    def run(stop_event):
        print("Hi from my macro!")
        for i in range(5):
            if sleep(1, stop_event):   # sleeps 1s, returns True if stopped early
                return
            print(i)
"""

import time


def sleep(seconds, stop_event, step=0.05):
    """Sleep for `seconds`, checking stop_event roughly every `step` seconds.
    Returns True if it was interrupted by a stop request, False if it
    completed the full duration normally."""
    end = time.time() + seconds
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            return False
        if stop_event.is_set():
            return True
        time.sleep(min(step, remaining))


def is_stopped(stop_event):
    """Quick check — did the user ask this macro to stop?"""
    return stop_event.is_set()


def loop_while_running(stop_event, delay=0.1):
    """Iterate forever until stop is requested, sleeping `delay` between ticks.

        for _ in loop_while_running(stop_event):
            do_something()
    """
    while not stop_event.is_set():
        yield
        if sleep(delay, stop_event):
            return
