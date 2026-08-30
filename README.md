# AutoFlow

A desktop app for running your own Python macros — drop a file in, pick it
in the list, hit **Start** or press a hotkey, watch it go.

## Download

Grab `AutoFlow.zip` from [Releases](../../releases), extract it, and run
`AutoFlow.exe`. No Python needed. Windows only.

## Features

- Drop-in macros — any `.py` file in `macros/` shows up automatically
- Global hotkey (click a button, press a key — no typing required)
- Dashboard with live stats and a recent-activity feed
- Full run history (name, duration, timestamp, outcome)
- Minimizes to system tray instead of quitting
- Only one instance can run at a time
- "Copy AI Prompt" button — paste it into any AI chatbot to get a working macro written for you
- Force Stop for macros that hang

## Running from source

```bash
pip install -r requirements.txt
python main.py
```

`customtkinter` is required. `keyboard` (global hotkey), `pyautogui`
(click/type macros), and `pystray` + `Pillow` (system tray) are optional —
the app just degrades gracefully without them.

## Writing a macro

Every macro is a `.py` file in `macros/` defining one function:

```python
from macro_api import sleep

def run(stop_event):
    while not stop_event.is_set():
        do_one_step()
        if sleep(0.5, stop_event):   # returns True if Stop was requested
            break
```

Check `stop_event` periodically so AutoFlow can stop your macro cleanly.
Anything you `print()` shows up in the app's log. See `macros/example_typer.py`
for a working example, or use **📋 Copy AI Prompt** in the app to have an AI
write one for you.

## Building the .exe

```bash
pip install pyinstaller
pyinstaller --onedir --windowed --name AutoFlow --icon assets/icon.ico --collect-all customtkinter --collect-all pyautogui --collect-all pystray main.py
```

Copy `macro_api.py`, `macros/`, and `assets/` into the new `dist/AutoFlow/`
folder next to `AutoFlow.exe`.

## License

Source-available, not permissively open source — see [`LICENSE`](LICENSE).
You can view, run, and modify it for personal use, and submit pull
requests — redistributing, rebranding, or commercial use needs permission.
