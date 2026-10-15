"""Example background macro: sends a few chat messages to the window you pick
under TARGET WINDOW (e.g. a Discord channel), without taking over your
keyboard — keep using your PC while it runs; the window can stay behind
others (but not minimized, for Discord).

Because run() takes a `window` argument, AutoFlow hands it the target window.
"""
from macro_api import sleep


def run(stop_event, window):
    print(f"Typing into: {window.title or window.title_match}")
    for i in range(1, 4):
        if not window.send(f"Hello from AutoFlow ({i}/3)"):
            return  # stopped while waiting for the window to come back
        print(f"Sent message {i}/3")
        if sleep(5, stop_event):
            print("Stopped early.")
            return
    print("Background chat macro finished.")
