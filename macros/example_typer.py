"""Example macro: waits 3s (so you can click into a text field) then types
a message. Requires: pip install pyautogui
"""
from macro_api import sleep


def run(stop_event):
    try:
        import pyautogui
    except ImportError:
        print("pyautogui not installed. Run: pip install pyautogui")
        return

    print("Typer macro starts in 3 seconds — click into a text field now!")
    if sleep(3, stop_event):
        return

    message = "Hello from my macro manager!"
    for ch in message:
        if stop_event.is_set():
            print("Stopped early.")
            return
        pyautogui.typewrite(ch, interval=0.03)

    print("Typer macro finished.")
