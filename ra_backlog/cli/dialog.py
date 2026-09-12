"""Tkinter credential prompt, used on first run from the terminal.

The web UI has its own credentials dialog; this exists so the CLI path does
not depend on a browser.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from ..config import log
from ..credentials import Credentials

SETTINGS_URL = "https://retroachievements.org/settings"


class CredentialDialog:
    def __init__(self, existing_username: str = ""):
        self.result: Credentials | None = None
        self.existing_username = existing_username
        self.root: tk.Tk | None = None

    def show(self) -> Credentials | None:
        self.root = tk.Tk()
        self.root.title("RetroAchievements credentials")
        self.root.resizable(False, False)
        self._enable_dpi_awareness()

        frame = ttk.Frame(self.root, padding=20)
        frame.grid(sticky="nsew")

        ttk.Label(frame, text="RetroAchievements credentials",
                  font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=2,
                                                      sticky="w", pady=(0, 4))
        ttk.Label(frame, text="Stored securely; used only to call the RA API.",
                  foreground="#666").grid(row=1, column=0, columnspan=2,
                                          sticky="w", pady=(0, 14))

        ttk.Label(frame, text="Username").grid(row=2, column=0, sticky="w", pady=4)
        self.user_var = tk.StringVar(value=self.existing_username)
        user_entry = ttk.Entry(frame, textvariable=self.user_var, width=34)
        user_entry.grid(row=2, column=1, pady=4)

        ttk.Label(frame, text="API key").grid(row=3, column=0, sticky="w", pady=4)
        self.key_var = tk.StringVar()
        self.key_entry = ttk.Entry(frame, textvariable=self.key_var, width=34, show="*")
        self.key_entry.grid(row=3, column=1, pady=4)

        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Show key", variable=self.show_var,
                        command=self._toggle).grid(row=4, column=1, sticky="w")

        link = ttk.Label(frame, text="Get your API key", foreground="#2a78d6",
                         cursor="hand2")
        link.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        link.bind("<Button-1>", lambda _e: self._open_url())

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(18, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).grid(row=0, column=0, padx=4)
        ttk.Button(buttons, text="Save", command=self._submit).grid(row=0, column=1)

        self.root.bind("<Return>", lambda _e: self._submit())
        self.root.bind("<Escape>", lambda _e: self._cancel())
        (user_entry if not self.existing_username else self.key_entry).focus_set()

        self._centre()
        self.root.mainloop()
        return self.result

    def _toggle(self) -> None:
        self.key_entry.config(show="" if self.show_var.get() else "*")

    def _open_url(self) -> None:
        import webbrowser
        webbrowser.open(SETTINGS_URL)

    def _submit(self) -> None:
        username = self.user_var.get().strip()
        api_key = self.key_var.get().strip()
        if not username or not api_key:
            messagebox.showwarning("Missing details",
                                   "Both a username and an API key are required.")
            return
        self.result = Credentials(username, api_key)
        self.root.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.root.destroy()

    def _centre(self) -> None:
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry(f"+{x}+{y}")

    @staticmethod
    def _enable_dpi_awareness() -> None:
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass          # not Windows, or an older release


def prompt_for_credentials(existing_username: str = "") -> Credentials | None:
    """Show the dialog, falling back to the terminal if there is no display."""
    try:
        return CredentialDialog(existing_username).show()
    except tk.TclError as exc:
        log.debug("No display for the tkinter dialog (%s); falling back to stdin", exc)
        return _prompt_stdin(existing_username)


def _prompt_stdin(existing_username: str = "") -> Credentials | None:
    import getpass
    print("\nRetroAchievements credentials")
    print(f"  Get your API key at {SETTINGS_URL}")
    username = input(f"  Username [{existing_username}]: ").strip() or existing_username
    api_key = getpass.getpass("  API key: ").strip()
    if not username or not api_key:
        return None
    return Credentials(username, api_key)
