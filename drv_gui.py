#!/usr/bin/env python3
"""
Minimal tkinter GUI for drv_extract_v7.py on macOS.
Place this script in the same folder as drv_extract_v7.py (or adjust SCRIPT_PATH).
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import subprocess
import os
import sys
import threading
from pathlib import Path
import datetime as _dt
import traceback

# ============================================================
# Configuration
# ============================================================

def find_script():
    """Find drv_extract_v11.py in current dir, script dir, or bundled resources."""
    candidates = []
    
    # If running as a PyInstaller bundle
    if getattr(sys, 'frozen', False):
        # Running in a bundle
        bundle_dir = sys._MEIPASS
        candidates.append(os.path.join(bundle_dir, "drv_extract_v11.py"))
    
    # Current working directory
    candidates.append(os.path.join(os.getcwd(), "drv_extract_v11.py"))
    
    # Script directory
    candidates.append(os.path.join(os.path.dirname(__file__), "drv_extract_v11.py"))
    
    # App bundle resources (for .app structure)
    candidates.append(os.path.join(os.path.dirname(__file__), "..", "Resources", "drv_extract_v11.py"))
    
    for path in candidates:
        if os.path.exists(path):
            return path
    
    return None

def find_python():
    """Find the correct Python executable for subprocesses."""
    # If not frozen, use current Python
    if not getattr(sys, 'frozen', False):
        return sys.executable
    
    # Try to find python3 in common locations
    import shutil
    python = shutil.which('python3')
    if python:
        return python
    
    # Fallback to sys.executable
    return sys.executable

SCRIPT_PATH = find_script()
PYTHON_PATH = find_python()

# Log startup info to a file for debugging
LOG_FILE = os.path.expanduser("~/Desktop/drv_extractor.log")
def startup_log(msg):
    with open(LOG_FILE, "a") as f:
        f.write(msg + "\n")


class DrvGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Speco DRV Extractor")
        self.root.geometry("700x650")
        self.root.resizable(True, True)
        
        self.process = None
        self.processing = False
        
        # Style
        style = ttk.Style()
        style.theme_use('aqua')  # macOS native look
        
        # Build UI
        self._build_ui()
    
    def _build_ui(self):
        """Construct the GUI layout."""
        
        # ---- Input folder selection ----
        input_frame = ttk.LabelFrame(self.root, text="Input", padding=10)
        input_frame.pack(fill=tk.X, padx=10, pady=10)
        
        self.input_label = ttk.Label(input_frame, text="No folder selected")
        self.input_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        ttk.Button(input_frame, text="Browse", command=self._select_folder).pack(side=tk.LEFT, padx=5)
        
        # ---- Options ----
        options_frame = ttk.LabelFrame(self.root, text="Options", padding=10)
        options_frame.pack(fill=tk.X, padx=10, pady=10)
        
        # Start time
        ts_frame = ttk.Frame(options_frame)
        ts_frame.pack(fill=tk.X, pady=5)
        ttk.Label(ts_frame, text="Start Time:").pack(side=tk.LEFT)
        ttk.Label(ts_frame, text="(leave blank for auto from readme.txt)").pack(side=tk.LEFT, padx=5)
        
        self.start_time_var = tk.StringVar()
        self.start_time_entry = ttk.Entry(ts_frame, textvariable=self.start_time_var, width=25)
        self.start_time_entry.pack(side=tk.LEFT, padx=5)
        ttk.Button(ts_frame, text="Now", command=self._set_now_time).pack(side=tk.LEFT, padx=2)
        
        # Audio mode
        audio_frame = ttk.Frame(options_frame)
        audio_frame.pack(fill=tk.X, pady=5)
        ttk.Label(audio_frame, text="Audio Mode:").pack(side=tk.LEFT)
        self.audio_var = tk.StringVar(value="alaw")
        audio_combo = ttk.Combobox(audio_frame, textvariable=self.audio_var, 
                                   values=["alaw", "mulaw", "pcm8u", "pcm8s"], 
                                   state="readonly", width=12)
        audio_combo.pack(side=tk.LEFT, padx=5)
        
        # Checkboxes
        checkbox_frame = ttk.Frame(options_frame)
        checkbox_frame.pack(fill=tk.X, pady=5)
        
        self.no_timestamp_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(checkbox_frame, text="Skip timestamp overlay", 
                       variable=self.no_timestamp_var).pack(anchor=tk.W)
        
        self.stream_copy_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(checkbox_frame, text="Stream copy (faster, may drift)", 
                       variable=self.stream_copy_var).pack(anchor=tk.W)
        
        self.no_mp4_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(checkbox_frame, text="Extract .h264/.wav only (no MP4)", 
                       variable=self.no_mp4_var).pack(anchor=tk.W)
        
        # ---- Action buttons ----
        button_frame = ttk.Frame(self.root)
        button_frame.pack(fill=tk.X, padx=10, pady=10)
        
        self.process_btn = ttk.Button(button_frame, text="Process", command=self._process_files)
        self.process_btn.pack(side=tk.LEFT, padx=5)
        
        self.stop_btn = ttk.Button(button_frame, text="Stop", command=self._stop_process, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        
        ttk.Button(button_frame, text="Clear Log", command=self._clear_log).pack(side=tk.LEFT, padx=5)
        
        # ---- Progress bar ----
        self.progress = ttk.Progressbar(self.root, mode='indeterminate')
        self.progress.pack(fill=tk.X, padx=10, pady=5)
        
        # ---- Output / Log ----
        log_frame = ttk.LabelFrame(self.root, text="Output", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        self.log = scrolledtext.ScrolledText(log_frame, height=15, width=80, 
                                             font=("Menlo", 10), wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True)
        
        # Make log read-only
        self.log.config(state=tk.DISABLED)
        
        self._log("Ready. Select a folder containing .drv files.")
    
    def _select_folder(self):
        """Open folder browser."""
        folder = filedialog.askdirectory(title="Select folder with .drv files")
        if folder:
            self.input_folder = folder
            self.input_label.config(text=folder)
            self._log(f"Selected: {folder}")
    
    def _set_now_time(self):
        """Set start time to now."""
        now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.start_time_var.set(now)
    
    def _log(self, message):
        """Append message to log."""
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, message + "\n")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)
        self.root.update()
    
    def _clear_log(self):
        """Clear the log."""
        self.log.config(state=tk.NORMAL)
        self.log.delete(1.0, tk.END)
        self.log.config(state=tk.DISABLED)
    
    def _process_files(self):
        """Validate and start processing."""
        if not hasattr(self, 'input_folder') or not self.input_folder:
            messagebox.showerror("Error", "Please select a folder")
            return
        
        if not SCRIPT_PATH or not os.path.exists(SCRIPT_PATH):
            messagebox.showerror(
                "Error", 
                "Cannot find drv_extract_v11.py\n\n"
                "Make sure both files are in the same folder:\n"
                "  • drv_gui.py\n"
                "  • drv_extract_v11.py"
            )
            return
        
        # Find all .drv files
        drv_files = list(Path(self.input_folder).glob("*.drv"))
        if not drv_files:
            messagebox.showwarning("No files", f"No .drv files found in {self.input_folder}")
            return
        
        self._log(f"\n{'='*60}")
        self._log(f"Processing {len(drv_files)} file(s)...")
        self._log(f"{'='*60}\n")
        
        # Disable process button, enable stop button
        self.process_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.progress.start()
        self.processing = True
        
        # Run in background thread
        thread = threading.Thread(target=self._run_extraction, args=(drv_files,))
        thread.daemon = True
        thread.start()
    
    def _run_extraction(self, drv_files):
        """Execute the extraction script for each file."""
        try:
            # Build command — use unbuffered Python output for real-time streaming
            cmd = [PYTHON_PATH, "-u", SCRIPT_PATH]
            
            # Add files
            cmd.extend(str(f) for f in sorted(drv_files))
            
            # Add options
            start_time = self.start_time_var.get().strip()
            if start_time:
                cmd.extend(["--start-time", start_time])
            
            audio_mode = self.audio_var.get()
            if audio_mode != "alaw":
                cmd.extend(["--audio-mode", audio_mode])
            
            if self.no_timestamp_var.get():
                cmd.append("--no-timestamp")
            
            if self.stream_copy_var.get():
                cmd.append("--stream-copy")
            
            if self.no_mp4_var.get():
                cmd.append("--no-mp4")
            
            # Log the command
            self._log(f"Command: {' '.join(cmd)}\n")
            
            # Run process — ensure it's not running in the context of the GUI app
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=self.input_folder,
                env={**os.environ, "PYTHONUNBUFFERED": "1"}
            )
            
            # Stream output
            for line in iter(self.process.stdout.readline, ''):
                if not self.processing:
                    self.process.terminate()
                    break
                if line:
                    self._log(line.rstrip())
            
            self.process.wait()
            
            if self.processing:
                if self.process.returncode == 0:
                    self._log("\n✓ Done!")
                    messagebox.showinfo("Success", "Processing complete")
                else:
                    self._log(f"\n✗ Process exited with code {self.process.returncode}")
                    messagebox.showerror("Error", f"Process failed (exit code {self.process.returncode})")
            else:
                self._log("\n• Stopped by user")
        
        except Exception as e:
            self._log(f"\n✗ Error: {e}")
            messagebox.showerror("Error", str(e))
        
        finally:
            self.processing = False
            self.process = None
            self.progress.stop()
            self.process_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
    
    def _stop_process(self):
        """Stop the running process."""
        if self.process:
            self._log("\nStopping...")
            self.processing = False
            self.process.terminate()


def main():
    try:
        startup_log(f"Starting DRV Extractor GUI")
        startup_log(f"Python: {sys.executable}")
        startup_log(f"Frozen: {getattr(sys, 'frozen', False)}")
        if getattr(sys, 'frozen', False):
            startup_log(f"Bundle dir (_MEIPASS): {sys._MEIPASS}")
        startup_log(f"Working dir: {os.getcwd()}")
        startup_log(f"Script path found: {SCRIPT_PATH}")
        
        root = tk.Tk()
        app = DrvGUI(root)
        root.mainloop()
        
        startup_log("App closed normally")
    except Exception as e:
        startup_log(f"ERROR: {e}")
        startup_log(traceback.format_exc())
        # Try to show error in a message box too
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Startup Error",
                f"Failed to start:\n\n{e}\n\nCheck ~/Desktop/drv_extractor.log for details"
            )
        except:
            pass


if __name__ == "__main__":
    main()
