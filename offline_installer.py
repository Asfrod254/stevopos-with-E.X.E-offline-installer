from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Stevo POS"
APP_EXECUTABLE = "StevoPOS.exe"
DEFAULT_INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs" / "StevoPOS"


def resource_path(relative_path: str) -> Path:
    bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_dir / relative_path


def powershell_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def create_shortcut(link_path: Path, target_path: Path, working_dir: Path) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$shell = New-Object -ComObject WScript.Shell;"
        f"$shortcut = $shell.CreateShortcut({powershell_literal(link_path)});"
        f"$shortcut.TargetPath = {powershell_literal(target_path)};"
        f"$shortcut.WorkingDirectory = {powershell_literal(working_dir)};"
        f"$shortcut.Description = {powershell_literal('Launch Stevo POS offline')};"
        "$shortcut.Save()"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class Installer:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Install Stevo POS")
        self.root.geometry("560x420")
        self.root.resizable(False, False)
        self.root.configure(bg="#f2f4f7")
        self.install_dir = tk.StringVar(value=str(DEFAULT_INSTALL_DIR))
        self.desktop_shortcut = tk.BooleanVar(value=True)
        self.start_menu_shortcut = tk.BooleanVar(value=True)
        self.launch_after_install = tk.BooleanVar(value=True)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", padding=(10, 7), font=("Segoe UI", 10))
        style.configure("Primary.TButton", background="#087e70", foreground="white",
                        font=("Segoe UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", "#06695e")])

        panel = tk.Frame(root, bg="white", padx=28, pady=24,
                         highlightthickness=1, highlightbackground="#dce3e6")
        panel.pack(fill="both", expand=True, padx=20, pady=20)
        tk.Label(panel, text="Stevo POS", bg="white", fg="#163b40",
                 font=("Segoe UI", 23, "bold")).pack(anchor="w")
        tk.Label(panel, text="Offline setup", bg="white", fg="#087e70",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(1, 8))
        tk.Label(panel, text="Install the complete point-of-sale app on this computer. "
                 "No internet connection or separate Python installation is required.",
                 bg="white", fg="#53666b", justify="left", wraplength=470).pack(anchor="w", pady=(0, 18))

        tk.Label(panel, text="Install location", bg="white", fg="#163b40",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        path_row = tk.Frame(panel, bg="white")
        path_row.pack(fill="x", pady=(5, 12))
        ttk.Entry(path_row, textvariable=self.install_dir).pack(side="left", fill="x", expand=True, ipady=4)
        ttk.Button(path_row, text="Browse...", command=self.choose_directory).pack(side="left", padx=(8, 0))

        ttk.Checkbutton(panel, text="Create a desktop shortcut", variable=self.desktop_shortcut).pack(anchor="w", pady=2)
        ttk.Checkbutton(panel, text="Create a Start Menu shortcut", variable=self.start_menu_shortcut).pack(anchor="w", pady=2)
        ttk.Checkbutton(panel, text="Launch Stevo POS after installation", variable=self.launch_after_install).pack(anchor="w", pady=2)

        self.status = tk.StringVar(value="Your local sales database will be kept when updating.")
        tk.Label(panel, textvariable=self.status, bg="white", fg="#64777c",
                 justify="left", wraplength=470).pack(anchor="w", pady=(15, 8))
        self.install_button = ttk.Button(panel, text="Install Stevo POS", style="Primary.TButton",
                                         command=self.install)
        self.install_button.pack(anchor="e", pady=(2, 0))

    def choose_directory(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root,
            title="Choose install location",
            initialdir=self.install_dir.get(),
            mustexist=False,
        )
        if selected:
            self.install_dir.set(selected)

    def install(self) -> None:
        destination = Path(self.install_dir.get()).expanduser()
        if not destination.is_absolute():
            messagebox.showerror("Invalid location", "Choose an absolute install location.", parent=self.root)
            return
        source_app = resource_path(f"payload/{APP_EXECUTABLE}")
        if not source_app.is_file():
            messagebox.showerror("Installer damaged", "The Stevo POS application is missing from this installer.", parent=self.root)
            return

        self.install_button.configure(state="disabled")
        self.status.set("Installing Stevo POS...")
        self.root.update_idletasks()
        try:
            destination.mkdir(parents=True, exist_ok=True)
            installed_app = destination / APP_EXECUTABLE
            shutil.copy2(source_app, installed_app)

            database_dir = destination / "data"
            database_dir.mkdir(exist_ok=True)
            init_result = subprocess.run(
                [str(installed_app), "--init-db"],
                cwd=destination,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if init_result.returncode != 0:
                raise RuntimeError("The local SQLite database could not be initialized.")

            if self.desktop_shortcut.get():
                desktop = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
                create_shortcut(desktop / f"{APP_NAME}.lnk", installed_app, destination)
            if self.start_menu_shortcut.get():
                start_menu = Path(os.environ.get("APPDATA", Path.home())) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
                create_shortcut(start_menu / f"{APP_NAME}.lnk", installed_app, destination)

            self.status.set(f"Installed to {destination}\nYour existing sales database was preserved.")
            messagebox.showinfo("Installation complete", "Stevo POS is installed and ready to use.", parent=self.root)
            if self.launch_after_install.get():
                subprocess.Popen([str(installed_app)], cwd=destination)
            self.root.destroy()
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            self.status.set("Installation did not complete.")
            self.install_button.configure(state="normal")
            messagebox.showerror("Installation failed", str(error), parent=self.root)


def main() -> None:
    if os.name != "nt":
        raise SystemExit("The Stevo POS setup installer is for Windows.")
    root = tk.Tk()
    Installer(root)
    root.mainloop()


if __name__ == "__main__":
    main()
