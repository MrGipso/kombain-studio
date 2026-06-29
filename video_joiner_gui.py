import argparse
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    EXTENDED,
    HORIZONTAL,
    LEFT,
    RIGHT,
    VERTICAL,
    W,
    X,
    Y,
    BooleanVar,
    Button,
    Checkbutton,
    Entry,
    Frame,
    Label,
    Listbox,
    Scrollbar,
    StringVar,
    Tk,
    filedialog,
    messagebox,
)
from tkinter.scrolledtext import ScrolledText


def natural_key(path: Path):
    parts = re.split(r"(\d+)", path.stem)
    return [int(part) if part.isdigit() else part.casefold() for part in parts]


def quote_concat_path(path: Path) -> str:
    # FFmpeg concat files use single-quoted paths; internal quotes are escaped.
    text = path.resolve().as_posix().replace("'", r"'\''")
    return f"file '{text}'"


class VideoJoinerApp:
    def __init__(self, root: Tk, start_dir: Path):
        self.root = root
        self.root.title("Video Joiner - FFmpeg")
        self.root.geometry("920x640")
        self.root.minsize(760, 480)

        self.folder = start_dir
        self.files: list[Path] = []
        self.drag_index: int | None = None
        self.worker: threading.Thread | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()

        self.output_name = StringVar(value="combined.mp4")
        self.reencode_on_fail = BooleanVar(value=True)
        self.status = StringVar(value="")

        self.build_ui()
        self.load_folder(start_dir)
        self.poll_log_queue()

    def build_ui(self):
        top = Frame(self.root, padx=10, pady=10)
        top.pack(fill=X)

        Button(top, text="Open folder", command=self.choose_folder).pack(side=LEFT)
        self.folder_label = Label(top, text="", anchor=W)
        self.folder_label.pack(side=LEFT, fill=X, expand=True, padx=(10, 0))

        main = Frame(self.root, padx=10)
        main.pack(fill=BOTH, expand=True)

        list_frame = Frame(main)
        list_frame.pack(side=LEFT, fill=BOTH, expand=True)

        self.listbox = Listbox(list_frame, selectmode=EXTENDED, activestyle="dotbox")
        self.listbox.pack(side=LEFT, fill=BOTH, expand=True)
        self.listbox.bind("<Button-1>", self.on_drag_start)
        self.listbox.bind("<B1-Motion>", self.on_drag_motion)
        self.listbox.bind("<Delete>", lambda _event: self.remove_selected())

        yscroll = Scrollbar(list_frame, orient=VERTICAL, command=self.listbox.yview)
        yscroll.pack(side=RIGHT, fill=Y)
        self.listbox.configure(yscrollcommand=yscroll.set)

        controls = Frame(main, padx=10)
        controls.pack(side=LEFT, fill=Y)

        Button(controls, text="Add files", width=18, command=self.add_files).pack(pady=(0, 6))
        Button(controls, text="Remove", width=18, command=self.remove_selected).pack(pady=6)
        Button(controls, text="Move up", width=18, command=lambda: self.move_selected(-1)).pack(pady=6)
        Button(controls, text="Move down", width=18, command=lambda: self.move_selected(1)).pack(pady=6)
        Button(controls, text="Sort by number", width=18, command=self.sort_files).pack(pady=6)
        Button(controls, text="Reload folder", width=18, command=lambda: self.load_folder(self.folder)).pack(pady=6)

        output = Frame(self.root, padx=10)
        output.pack(fill=X)

        Label(output, text="Output file:").pack(side=LEFT)
        Entry(output, textvariable=self.output_name).pack(side=LEFT, fill=X, expand=True, padx=8)
        Checkbutton(output, text="Re-encode if direct join fails", variable=self.reencode_on_fail).pack(side=LEFT)

        actions = Frame(self.root, padx=10, pady=10)
        actions.pack(fill=X)

        self.join_button = Button(actions, text="Join videos", command=self.join_videos)
        self.join_button.pack(side=LEFT)
        Label(actions, textvariable=self.status, anchor=W).pack(side=LEFT, fill=X, expand=True, padx=10)

        log_frame = Frame(self.root, padx=10)
        log_frame.pack(fill=BOTH)
        self.log = ScrolledText(log_frame, height=9, wrap="word")
        self.log.pack(fill=BOTH, expand=True)

        xscroll = Scrollbar(self.root, orient=HORIZONTAL, command=self.listbox.xview)
        xscroll.pack(fill=X, padx=10, pady=(0, 8))
        self.listbox.configure(xscrollcommand=xscroll.set)

    def set_files(self, files: list[Path]):
        self.files = files
        self.refresh_listbox()
        self.status.set(f"{len(self.files)} video(s)")

    def refresh_listbox(self):
        selected = set(self.listbox.curselection())
        self.listbox.delete(0, END)
        for index, path in enumerate(self.files, start=1):
            self.listbox.insert(END, f"{index:03d}. {path.name}")
        for index in selected:
            if index < len(self.files):
                self.listbox.selection_set(index)

    def load_folder(self, folder: Path):
        self.folder = folder.resolve()
        self.folder_label.config(text=str(self.folder))
        files = sorted(self.folder.glob("*.mp4"), key=natural_key)
        files = [path for path in files if path.name.casefold() != self.output_name.get().casefold()]
        self.set_files(files)
        self.write_log(f"Loaded {len(files)} mp4 file(s) from {self.folder}")

    def choose_folder(self):
        folder = filedialog.askdirectory(initialdir=str(self.folder))
        if folder:
            self.load_folder(Path(folder))

    def add_files(self):
        filenames = filedialog.askopenfilenames(
            initialdir=str(self.folder),
            filetypes=[("MP4 video", "*.mp4"), ("All files", "*.*")],
        )
        for filename in filenames:
            path = Path(filename)
            if path not in self.files:
                self.files.append(path)
        self.refresh_listbox()
        self.status.set(f"{len(self.files)} video(s)")

    def remove_selected(self):
        for index in reversed(self.listbox.curselection()):
            del self.files[index]
        self.refresh_listbox()
        self.status.set(f"{len(self.files)} video(s)")

    def move_selected(self, direction: int):
        selected = list(self.listbox.curselection())
        if not selected:
            return

        if direction < 0:
            iterable = selected
        else:
            iterable = reversed(selected)

        moved = []
        for index in iterable:
            new_index = index + direction
            if 0 <= new_index < len(self.files):
                self.files[index], self.files[new_index] = self.files[new_index], self.files[index]
                moved.append(new_index)
            else:
                moved.append(index)

        self.refresh_listbox()
        self.listbox.selection_clear(0, END)
        for index in moved:
            self.listbox.selection_set(index)
        self.listbox.see(min(moved))

    def sort_files(self):
        self.set_files(sorted(self.files, key=natural_key))

    def on_drag_start(self, event):
        self.drag_index = self.listbox.nearest(event.y)

    def on_drag_motion(self, event):
        if self.drag_index is None or not self.files:
            return
        new_index = self.listbox.nearest(event.y)
        if new_index == self.drag_index or new_index < 0 or new_index >= len(self.files):
            return
        item = self.files.pop(self.drag_index)
        self.files.insert(new_index, item)
        self.drag_index = new_index
        self.refresh_listbox()
        self.listbox.selection_clear(0, END)
        self.listbox.selection_set(new_index)
        self.listbox.see(new_index)

    def join_videos(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.files:
            messagebox.showerror("No videos", "Add at least one mp4 file.")
            return

        output = self.folder / self.output_name.get().strip()
        if output.suffix.casefold() != ".mp4":
            output = output.with_suffix(".mp4")
        if output in self.files:
            messagebox.showerror("Output conflict", "Output file is also in the input list.")
            return

        ffmpeg = shutil.which("ffmpeg.exe") or shutil.which("ffmpeg")
        if not ffmpeg:
            messagebox.showerror("FFmpeg not found", "ffmpeg.exe was not found in PATH.")
            return

        self.join_button.config(state="disabled")
        self.status.set("Joining...")
        self.log.delete("1.0", END)
        files = list(self.files)
        reencode_on_fail = self.reencode_on_fail.get()
        self.worker = threading.Thread(
            target=self.run_ffmpeg,
            args=(ffmpeg, output, files, reencode_on_fail),
            daemon=True,
        )
        self.worker.start()

    def run_ffmpeg(self, ffmpeg: str, output: Path, files: list[Path], reencode_on_fail: bool):
        list_path = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
                list_path = Path(handle.name)
                for path in files:
                    handle.write(quote_concat_path(path) + "\n")

            self.log_queue.put(f"Output: {output}")
            self.log_queue.put("Trying direct join without re-encoding...")
            command = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(output)]
            code = self.run_command(command)

            if code != 0 and reencode_on_fail:
                self.log_queue.put("Direct join failed. Trying again with re-encoding...")
                command = [
                    ffmpeg,
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(list_path),
                    "-c:v",
                    "libx264",
                    "-crf",
                    "18",
                    "-preset",
                    "veryfast",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    str(output),
                ]
                code = self.run_command(command)

            if code == 0:
                self.log_queue.put("DONE")
                self.log_queue.put("__STATUS__Done")
            else:
                self.log_queue.put("ERROR: ffmpeg could not join these files.")
                self.log_queue.put("__STATUS__Error")
        except Exception as exc:
            self.log_queue.put(f"ERROR: {exc}")
            self.log_queue.put("__STATUS__Error")
        finally:
            if list_path:
                try:
                    list_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self.log_queue.put("__ENABLE__")

    def run_command(self, command: list[str]) -> int:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            self.log_queue.put(line.rstrip())
        return process.wait()

    def poll_log_queue(self):
        try:
            while True:
                message = self.log_queue.get_nowait()
                if message == "__ENABLE__":
                    self.join_button.config(state="normal")
                elif message.startswith("__STATUS__"):
                    self.status.set(message.replace("__STATUS__", "", 1))
                else:
                    self.write_log(message)
        except queue.Empty:
            pass
        self.root.after(100, self.poll_log_queue)

    def write_log(self, message: str):
        self.log.insert(END, message + "\n")
        self.log.see(END)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", nargs="?", default=os.getcwd())
    args = parser.parse_args()

    start_dir = Path(args.folder)
    if not start_dir.exists():
        start_dir = Path.cwd()

    root = Tk()
    VideoJoinerApp(root, start_dir)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
