"""SnapFrame per-user installer / uninstaller (no admin rights needed).

  SnapFrame-Setup-x.y.z.exe                 GUI install
  SnapFrame-Setup-x.y.z.exe --silent        silent install with defaults (IT deployment)
         [--dir "C:\\path"] [--no-desktop] [--no-autostart]
  <install dir>\\Uninstall.exe             GUI uninstall      (--silent for no prompt)

The setup exe embeds SnapFrame.exe. It installs under %LOCALAPPDATA%\\Programs\\SnapFrame, adds a Start
menu shortcut (+ optional Desktop shortcut and start-with-Windows), and registers an entry in
Settings > Apps so it can be removed normally.
"""
import ctypes
import os
import shutil
import subprocess
import sys
import tkinter as tk
import winreg
from tkinter import filedialog, messagebox, ttk

from branding import APP, VERSION

EXE = f"{APP}.exe"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP}"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Programs", APP)
START_MENU = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs")
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")
NO_WINDOW = 0x08000000


def resource(name):
    return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), name)


def kill_running():
    subprocess.run(["taskkill", "/IM", EXE, "/F"], capture_output=True, creationflags=NO_WINDOW)


def make_shortcut(lnk, target, workdir):
    # values go through environment variables: extra args after -Command do NOT populate $args
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SL_LNK);"
          "$s.TargetPath=$env:SL_TARGET;$s.WorkingDirectory=$env:SL_WORKDIR;$s.IconLocation=$env:SL_TARGET;"
          f"$s.Description='{APP}';$s.Save()")
    env = dict(os.environ, SL_LNK=lnk, SL_TARGET=target, SL_WORKDIR=workdir)
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       capture_output=True, text=True, env=env, creationflags=NO_WINDOW)
    if r.returncode or not os.path.exists(lnk):
        raise RuntimeError(f"could not create shortcut {lnk}: {r.stderr.strip() or r.stdout.strip()}")


def install(install_dir, desktop=False, autostart=True, launch=True, log=lambda m: None):
    exe = os.path.join(install_dir, EXE)
    log("Stopping running instance...")
    kill_running()
    os.makedirs(install_dir, exist_ok=True)
    log("Copying files...")
    shutil.copyfile(resource(EXE), exe)
    unin = os.path.join(install_dir, "Uninstall.exe")
    if getattr(sys, "frozen", False):
        shutil.copyfile(sys.executable, unin)
    log("Creating shortcuts...")
    os.makedirs(START_MENU, exist_ok=True)
    make_shortcut(os.path.join(START_MENU, f"{APP}.lnk"), exe, install_dir)
    if desktop:
        make_shortcut(os.path.join(DESKTOP, f"{APP}.lnk"), exe, install_dir)
    log("Registering...")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        for name, val in (("DisplayName", APP), ("DisplayVersion", VERSION), ("Publisher", "YaoJie2099"),
                          ("InstallLocation", install_dir), ("DisplayIcon", exe),
                          ("UninstallString", f'"{unin}" --uninstall'),
                          ("QuietUninstallString", f'"{unin}" --uninstall --silent')):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, os.path.getsize(exe) // 1024 * 2)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if autostart:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, f'"{exe}"')
        else:
            try:
                winreg.DeleteValue(k, APP)
            except OSError:
                pass
    if launch:
        log("Starting SnapFrame...")
        subprocess.Popen([exe], cwd=install_dir, creationflags=NO_WINDOW)
    log("Done.")


def installed_dir():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            return winreg.QueryValueEx(k, "InstallLocation")[0]
    except OSError:
        return os.path.dirname(os.path.abspath(sys.executable))


def uninstall(silent=False):
    d = installed_dir()
    if not silent and not messagebox.askyesno(f"Uninstall {APP}", f"Remove {APP} from\n{d}?\n\n"
                                              "Your screenshots and settings are kept."):
        return
    kill_running()
    for lnk in (os.path.join(START_MENU, f"{APP}.lnk"), os.path.join(DESKTOP, f"{APP}.lnk")):
        try:
            os.remove(lnk)
        except OSError:
            pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP)
    except OSError:
        pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    # this process is Uninstall.exe itself, so the folder is removed by a helper after we exit
    subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 4 >nul & rmdir /s /q "{d}"', shell=True, creationflags=NO_WINDOW)
    if not silent:
        messagebox.showinfo(f"Uninstall {APP}", f"{APP} was removed.")


class Wizard:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"{APP} Setup")
        self.root.resizable(False, False)
        try:
            self.root.iconbitmap(resource("snapframe.ico"))
        except Exception:
            pass
        f = ttk.Frame(self.root, padding=16)
        f.pack()
        ttk.Label(f, text=f"{APP} {VERSION}", font=("Segoe UI", 14, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="Lightshot-style screenshot + annotation tool.\nPress PrintScreen or Ctrl+Alt+A to capture. "
                          "Nothing is uploaded anywhere.", justify="left").grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 12))
        ttk.Label(f, text="Install to:").grid(row=2, column=0, sticky="w")
        self.dir = tk.StringVar(value=DEFAULT_DIR)
        ttk.Entry(f, textvariable=self.dir, width=52).grid(row=3, column=0, columnspan=2, sticky="we")
        ttk.Button(f, text="Browse...", command=self.browse).grid(row=3, column=2, padx=(6, 0))
        self.desktop = tk.BooleanVar(value=False)
        self.autostart = tk.BooleanVar(value=True)
        self.launch = tk.BooleanVar(value=True)
        for i, (txt, var) in enumerate((("Create a Desktop shortcut", self.desktop),
                                        ("Start SnapFrame with Windows", self.autostart),
                                        ("Launch SnapFrame when finished", self.launch))):
            ttk.Checkbutton(f, text=txt, variable=var).grid(row=4 + i, column=0, columnspan=3, sticky="w", pady=(8 if i == 0 else 0, 0))
        self.status = ttk.Label(f, text="No administrator rights needed.", foreground="#555")
        self.status.grid(row=7, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.btn = ttk.Button(f, text="Install", command=self.go)
        self.btn.grid(row=7, column=2, sticky="e", pady=(12, 0))

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.dir.get())
        if d:
            self.dir.set(os.path.join(os.path.normpath(d), APP) if os.path.basename(d).lower() != APP.lower() else os.path.normpath(d))

    def go(self):
        self.btn.configure(state="disabled")
        try:
            install(self.dir.get(), self.desktop.get(), self.autostart.get(), self.launch.get(),
                    log=lambda m: (self.status.configure(text=m), self.root.update()))
        except Exception as e:
            messagebox.showerror(f"{APP} Setup", f"Install failed:\n{e}")
            self.btn.configure(state="normal")
            return
        messagebox.showinfo(f"{APP} Setup", f"{APP} is installed.\nLook for the icon in the system tray.")
        self.root.destroy()


def main():
    args = sys.argv[1:]
    silent = "--silent" in args
    ctypes.windll.shcore.SetProcessDpiAwareness(1) if hasattr(ctypes.windll, "shcore") else None
    if "--uninstall" in args:
        if not silent:
            tk.Tk().withdraw()
        return uninstall(silent)
    if silent:
        d = args[args.index("--dir") + 1] if "--dir" in args else DEFAULT_DIR
        install(d, desktop="--no-desktop" not in args and "--desktop" in args,
                autostart="--no-autostart" not in args, launch="--no-launch" not in args)
        return
    Wizard().root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        path = os.path.join(os.environ.get("TEMP", "."), "snapframe-setup.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        if "--silent" not in sys.argv:
            messagebox.showerror(f"{APP} Setup", f"Setup failed. Details: {path}\n\n{traceback.format_exc()[-600:]}")
        sys.exit(1)
