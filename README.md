# AutoFlow

A desktop app for running your own Python macros — drop a file in, pick it
in the list, hit **Start** or press a hotkey, watch it go.

## Download

Grab `AutoFlow.zip` from [Releases](../../releases), extract it, and run
`AutoFlow.exe`. No Python needed. Windows only.

## Features

- Drop-in macros — any `.py` file in `macros/` shows up automatically
- Background macros — bind a macro to one window (e.g. a Discord channel) and
  it types there while you keep using your PC, with that window behind others
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

### Background macros (target window)

Give `run` a second argument and the macro gets the window you picked under
**Target window** on the Macros page (**Choose…** lists open windows):

```python
from macro_api import sleep

def run(stop_event, window):
    while not stop_event.is_set():
        window.send("owo hunt")      # types the text + Enter
        if sleep(15, stop_event):
            break
```

- `window.send(text)`: type `text` and press Enter
- `window.type(text)`: type without pressing Enter (`"\n"` presses Enter)
- `window.press(key, times=1)`: `"enter"`, `"tab"`, `"esc"`, `"backspace"`,
  `"space"`, arrows, `"home"`/`"end"`, `"pageup"`/`"pagedown"`, `"delete"`,
  `"f1"`–`"f24"`, or a single character
- `window.exists` / `window.title`: whether it's open, and its current title

Input is posted straight to that window instead of the real keyboard, so your
focus and your own typing are left alone. The window is found by its title
(case-insensitive "contains", so you can shorten it to e.g. `#owo`) and
process. If it goes away, for example when you switch Discord to another
channel, the macro waits until it's back. The window can sit behind other
windows, but Discord and other Chromium/Electron apps ignore input while
minimized, so the macro also pauses until you un-minimize it. Keyboard only:
key combos (Ctrl+…) and mouse clicks don't work in the background. Windows
only.

See `macros/example_background_chat.py`.

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
