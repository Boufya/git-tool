"""
Git Workflow Tool — GUI edition (tkinter, stdlib only)

Architecture:
- GitRunner         -> Facade over the git CLI (SSOT for execution); UI-agnostic
- CredentialManager -> Identity config + forces GitHub re-auth popup on change
- TemplateManager   -> Discovers/copies .gitignore + .gitattributes template sets
- AppConfig         -> SSOT for persisted settings (remembers your templates folder)
- Command classes   -> Command pattern: each workflow is a self-contained unit
- WorkflowApp       -> Tkinter view/controller; talks to commands only (MVC-ish)
- Log flow          -> Observer pattern: core emits lines, GUI subscribes via queue
"""

import json
import os
import queue
import shutil
import stat
import subprocess
import threading
import tkinter as tk
from abc import ABC, abstractmethod
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable

Logger = Callable[[str], None]

class GitRunner:
    def __init__(self, cwd: Path, log: Logger):
        self.cwd = cwd
        self.log = log

    def run(self, *args: str, input_text: str | None = None,
            allow_fail: bool = False) -> subprocess.CompletedProcess:
        self.log(f"$ git {' '.join(args)}")
        result = subprocess.run(
            ["git", *args],
            cwd=self.cwd,
            text=True,
            input=input_text,
            capture_output=True,
        )
        for stream in (result.stdout, result.stderr):
            if stream.strip():
                self.log(stream.strip())
        if result.returncode != 0 and not allow_fail:
            raise RuntimeError(f"git {args[0]} failed (exit {result.returncode})")
        return result

    def is_repo(self) -> bool:
        return self.run("rev-parse", "--is-inside-work-tree", allow_fail=True).returncode == 0

    def current_branch(self) -> str:
        result = self.run("branch", "--show-current", allow_fail=True)
        return result.stdout.strip() or "main"

    def ensure_branch(self, name: str) -> str:
        if name:
            self.run("branch", "-M", name, allow_fail=True)
        return self.current_branch()

    def set_remote(self, url: str, name: str = "origin") -> None:
        exists = self.run("remote", "get-url", name, allow_fail=True).returncode == 0
        self.run("remote", "set-url" if exists else "add", name, url)
        if exists:
            self.log(f"[i] Remote '{name}' updated to point at the new URL.")

    def wipe_history(self) -> None:
        git_dir = self.cwd / ".git"
        if not git_dir.is_dir():
            self.log("[i] No existing history to wipe.")
            return

        def _unlock(func, path, _exc):
            os.chmod(path, stat.S_IWRITE)
            func(path)

        try:
            shutil.rmtree(git_dir, onexc=_unlock)
        except TypeError:
            shutil.rmtree(git_dir, onerror=_unlock)
        self.log("[i] Local git history wiped — starting fresh.")


class CredentialManager:
    GITHUB_HOST = "github.com"

    def __init__(self, git: GitRunner):
        self.git = git

    def current_identity(self) -> tuple[str, str]:
        name = self.git.run("config", "--global", "user.name", allow_fail=True).stdout.strip()
        email = self.git.run("config", "--global", "user.email", allow_fail=True).stdout.strip()
        return name, email

    def update_identity(self, name: str, email: str) -> None:
        old_name, old_email = self.current_identity()
        name, email = name or old_name, email or old_email
        self.git.run("config", "--global", "user.name", name)
        self.git.run("config", "--global", "user.email", email)
        if (name, email) != (old_name, old_email):
            self._clear_cached_auth()
            self.git.log("[i] Credentials changed! GitHub sign-in will pop up on next push/pull.")

    def _clear_cached_auth(self) -> None:
        self.git.run(
            "credential", "reject",
            input_text=f"protocol=https\nhost={self.GITHUB_HOST}\n\n",
            allow_fail=True,
        )


class TemplateManager:
    TEMPLATE_FILES = (".gitignore", ".gitattributes")

    def __init__(self, templates_root: Path, log: Logger):
        self.templates_root = templates_root
        self.log = log

    def available(self) -> list[str]:
        if not self.templates_root.is_dir():
            return []
        return sorted(
            folder.name for folder in self.templates_root.iterdir()
            if folder.is_dir() and any((folder / f).is_file() for f in self.TEMPLATE_FILES)
        )

    def existing_conflicts(self, template: str, dest: Path) -> list[str]:
        source = self.templates_root / template
        return [f for f in self.TEMPLATE_FILES
                if (source / f).is_file() and (dest / f).is_file()]

    def copy(self, template: str, dest: Path) -> None:
        source = self.templates_root / template
        copied = 0
        for filename in self.TEMPLATE_FILES:
            src_file = source / filename
            if src_file.is_file():
                shutil.copy2(src_file, dest / filename)
                self.log(f"[+] Copied {template}/{filename} -> {dest / filename}")
                copied += 1
        if not copied:
            raise RuntimeError(f"No template files found in {source}")
        self.log(f"[✓] '{template}' template applied to project root.")


class AppConfig:
    PATH = Path.home() / ".git_workflow_tool.json"

    def __init__(self):
        self.data: dict = {}
        try:
            self.data = json.loads(self.PATH.read_text())
        except (OSError, json.JSONDecodeError):
            pass

    def get(self, key: str, default: str = "") -> str:
        return self.data.get(key, default)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value
        try:
            self.PATH.write_text(json.dumps(self.data, indent=2))
        except OSError:
            pass


class WorkflowCommand(ABC):
    def __init__(self, git: GitRunner, creds: CredentialManager):
        self.git = git
        self.creds = creds

    @abstractmethod
    def execute(self, **params) -> None: ...


class SetupCommand(WorkflowCommand):
    def execute(self, remote_url: str = "", branch: str = "", fresh_start: bool = False, **_) -> None:
        if fresh_start:
            self.git.wipe_history()
        if not self.git.is_repo():
            self.git.run("init")
        self.git.run("lfs", "install", allow_fail=True)
        if remote_url:
            self.git.set_remote(remote_url) 
        self.git.run("add", ".")
        self.git.run("commit", "-m", "Initial commit", allow_fail=True)
        branch = self.git.ensure_branch(branch)
        pull = self.git.run("pull", "origin", branch, "--no-rebase",
                            "--allow-unrelated-histories", "--no-edit", allow_fail=True)
        if "CONFLICT" in pull.stdout + pull.stderr:
            raise RuntimeError(
                "Merge conflict with the remote's starter files. "
                "Resolve the conflicted files, then run Submit.")
        self.git.run("push", "-u", "origin", branch)
        self.git.log("[✓] Project set up and pushed.")


class RefreshCommand(WorkflowCommand):
    def execute(self, **_) -> None:
        self.git.run("pull")
        self.git.log("[✓] Up to date with remote.")


class SubmitCommand(WorkflowCommand):
    def execute(self, message: str = "Update project", branch: str = "", **_) -> None:
        self.git.run("pull", "--no-rebase", "--no-edit", allow_fail=True)
        self.git.run("add", ".")
        commit = self.git.run("commit", "-m", message or "Update project", allow_fail=True)
        if "nothing to commit" in commit.stdout + commit.stderr:
            self.git.log("[i] Nothing new to commit.")
            return
        self.git.run("push", "-u", "origin", self.git.ensure_branch(branch))
        self.git.log("[✓] Updates submitted.")


class CredentialsCommand(WorkflowCommand):
    def execute(self, name: str = "", email: str = "", **_) -> None:
        self.creds.update_identity(name, email)


class FixAuthorCommand(WorkflowCommand):
    def execute(self, branch: str = "", **_) -> None:
        self.git.run("commit", "--amend", "--reset-author", "--no-edit")
        self.git.run("push", "--force-with-lease", "-u", "origin",
                     self.git.ensure_branch(branch))
        self.git.log("[✓] Latest commit re-attributed to your current identity and pushed.")


class ApplyTemplateCommand(WorkflowCommand):
    def execute(self, templates_root: str = "", template: str = "", **_) -> None:
        if not template:
            raise RuntimeError("No template selected.")
        manager = TemplateManager(Path(templates_root), self.git.log)
        manager.copy(template, self.git.cwd)


class WorkflowApp(tk.Tk):
    POLL_MS = 100  

    def __init__(self):
        super().__init__()
        self.title("Git Workflow Tool")
        self.geometry("740x600")
        self.minsize(620, 500)

        self.config_store = AppConfig()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.busy = False 

        self._build_widgets()
        self._refresh_templates()
        self._poll_log_queue()


    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 4}

        folder_frame = ttk.LabelFrame(self, text="Project")
        folder_frame.pack(fill="x", **pad)
        ttk.Label(folder_frame, text="Folder:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.folder_var = tk.StringVar(value=str(Path.cwd()))
        ttk.Entry(folder_frame, textvariable=self.folder_var).grid(
            row=0, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(folder_frame, text="Browse…",
                   command=lambda: self._browse_into(self.folder_var)).grid(
            row=0, column=2, padx=6, pady=4)
        ttk.Label(folder_frame, text="Branch:").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.branch_var = tk.StringVar()
        ttk.Entry(folder_frame, textvariable=self.branch_var, width=24).grid(
            row=1, column=1, sticky="w", padx=6, pady=4)
        ttk.Label(folder_frame, text="(leave blank to keep the current branch)").grid(
            row=1, column=1, sticky="w", padx=(200, 6), pady=4)
        folder_frame.columnconfigure(1, weight=1)

        tabs = ttk.Notebook(self)
        tabs.pack(fill="x", **pad)
        self._build_setup_tab(tabs)
        self._build_refresh_tab(tabs)
        self._build_submit_tab(tabs)
        self._build_templates_tab(tabs)
        self._build_credentials_tab(tabs)

        log_frame = ttk.LabelFrame(self, text="Output")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_view = scrolledtext.ScrolledText(
            log_frame, height=12, state="disabled", font=("Consolas", 9))
        self.log_view.pack(fill="both", expand=True, padx=6, pady=6)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status_var, anchor="w").pack(fill="x", padx=8, pady=(0, 6))

    def _build_setup_tab(self, tabs: ttk.Notebook) -> None:
        frame = ttk.Frame(tabs)
        tabs.add(frame, text="Setup")
        ttk.Label(frame, text="Repository URL:").grid(
            row=0, column=0, sticky="w", padx=6, pady=6)
        self.remote_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.remote_var, width=60).grid(
            row=0, column=1, sticky="ew", padx=6, pady=6)
        self.fresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, variable=self.fresh_var,
                        text="Toggle this to wipe local git history first "
                             "(e.g. new repo begins at commit #1)").grid(
            row=1, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(frame, text="Tip: create the GitHub repo WITHOUT a README.\n"
                              "The templates cover it, and you'll avoid merge commits.").grid(
            row=2, column=1, sticky="w", padx=6, pady=2)
        ttk.Button(frame, text="Run Setup", command=self._run_setup).grid(
            row=3, column=1, sticky="e", padx=6, pady=6)
        frame.columnconfigure(1, weight=1)

    def _run_setup(self) -> None:
        if self.fresh_var.get() and not messagebox.askyesno(
                "Wipe local history?",
                "Fresh start will PERMANENTLY delete this project's local git\n"
                "history (the .git folder). Your project files are untouched,\n"
                "but old commits are gone for good.\n\nContinue?"):
            return
        self._dispatch(SetupCommand,
                       remote_url=self.remote_var.get().strip(),
                       branch=self.branch_var.get().strip(),
                       fresh_start=self.fresh_var.get())

    def _build_refresh_tab(self, tabs: ttk.Notebook) -> None:
        frame = ttk.Frame(tabs)
        tabs.add(frame, text="Refresh")
        ttk.Label(frame, text="Pull the latest changes from the remote.").pack(
            anchor="w", padx=6, pady=6)
        ttk.Button(frame, text="Refresh (pull)",
                   command=lambda: self._dispatch(RefreshCommand)).pack(anchor="e", padx=6, pady=6)

    def _build_submit_tab(self, tabs: ttk.Notebook) -> None:
        frame = ttk.Frame(tabs)
        tabs.add(frame, text="Submit")
        ttk.Label(frame, text="Commit message:").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.message_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.message_var, width=60).grid(
            row=0, column=1, sticky="ew", padx=6, pady=6)
        ttk.Button(frame, text="Submit (pull -> add -> commit -> push)",
                   command=lambda: self._dispatch(SubmitCommand,
                                                  message=self.message_var.get().strip(),
                                                  branch=self.branch_var.get().strip())
                   ).grid(row=1, column=1, sticky="e", padx=6, pady=6)
        frame.columnconfigure(1, weight=1)

    def _build_templates_tab(self, tabs: ttk.Notebook) -> None:
        frame = ttk.Frame(tabs)
        tabs.add(frame, text="Templates")

        ttk.Label(frame, text="Content folder (holds Unity/Godot/etc. subfolders):").grid(
            row=0, column=0, sticky="w", padx=6, pady=6)
        self.templates_root_var = tk.StringVar(value=self.config_store.get("templates_root"))
        ttk.Entry(frame, textvariable=self.templates_root_var, width=48).grid(
            row=0, column=1, sticky="ew", padx=6, pady=6)
        ttk.Button(frame, text="Browse…", command=self._browse_templates_root).grid(
            row=0, column=2, padx=6, pady=6)

        ttk.Label(frame, text="Template:").grid(row=1, column=0, sticky="w", padx=6, pady=6)
        self.template_var = tk.StringVar()
        self.template_box = ttk.Combobox(frame, textvariable=self.template_var, state="readonly")
        self.template_box.grid(row=1, column=1, sticky="ew", padx=6, pady=6)
        ttk.Button(frame, text="Rescan", command=self._refresh_templates).grid(
            row=1, column=2, padx=6, pady=6)

        ttk.Label(frame, text="Copies that folder's .gitignore + .gitattributes\n"
                              "into the project root above.").grid(
            row=2, column=1, sticky="w", padx=6, pady=2)
        ttk.Button(frame, text="Copy to project root",
                   command=self._apply_template).grid(row=3, column=1, sticky="e", padx=6, pady=6)
        frame.columnconfigure(1, weight=1)

    def _build_credentials_tab(self, tabs: ttk.Notebook) -> None:
        frame = ttk.Frame(tabs)
        tabs.add(frame, text="Credentials")
        self.name_var, self.email_var = tk.StringVar(), tk.StringVar()
        ttk.Label(frame, text="Name:").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        ttk.Entry(frame, textvariable=self.name_var, width=40).grid(
            row=0, column=1, sticky="ew", padx=6, pady=6)
        ttk.Label(frame, text="Email:").grid(row=1, column=0, sticky="w", padx=6, pady=6)
        ttk.Entry(frame, textvariable=self.email_var, width=40).grid(
            row=1, column=1, sticky="ew", padx=6, pady=6)
        ttk.Label(frame, text="Changing these clears cached GitHub auth ->\n"
                              "the sign-in popup appears on your next push.").grid(
            row=2, column=1, sticky="w", padx=6, pady=2)
        ttk.Button(frame, text="Save credentials",
                   command=lambda: self._dispatch(CredentialsCommand,
                                                  name=self.name_var.get().strip(),
                                                  email=self.email_var.get().strip())
                   ).grid(row=3, column=1, sticky="e", padx=6, pady=6)
        ttk.Separator(frame, orient="horizontal").grid(
            row=4, column=0, columnspan=2, sticky="ew", padx=6, pady=8)
        ttk.Label(frame, text="Pushed a commit under the wrong account?\n"
                              "This re-stamps the LATEST commit with the author specified in the config.").grid(
            row=5, column=1, sticky="w", padx=6, pady=2)
        ttk.Button(frame, text="Fix last commit's author",
                   command=self._fix_author).grid(row=6, column=1, sticky="e", padx=6, pady=6)
        frame.columnconfigure(1, weight=1)

    def _fix_author(self) -> None:
        if messagebox.askyesno(
                "Rewrite last commit?",
                "This re-stamps the LATEST commit with your current name/email,\n"
                "then force-pushes it (Safely! It won't overwrite work you\n"
                "haven't pulled).\n\nTip: save the correct credentials first. Continue?"):
            self._dispatch(FixAuthorCommand, branch=self.branch_var.get().strip())

    def _browse_templates_root(self) -> None:
        self._browse_into(self.templates_root_var)
        self.config_store.set("templates_root", self.templates_root_var.get())
        self._refresh_templates()

    def _refresh_templates(self) -> None:
        manager = TemplateManager(Path(self.templates_root_var.get() or "."), log=lambda _: None)
        options = manager.available()
        self.template_box["values"] = options
        if options and self.template_var.get() not in options:
            self.template_var.set(options[0])
        elif not options:
            self.template_var.set("")

    def _apply_template(self) -> None:
        templates_root = self.templates_root_var.get().strip()
        template = self.template_var.get()
        project_dir = Path(self.folder_var.get())
        if not template:
            messagebox.showinfo("No template", "Pick your content folder and a template first.")
            return

        manager = TemplateManager(Path(templates_root), log=lambda _: None)
        conflicts = manager.existing_conflicts(template, project_dir)
        if conflicts and not messagebox.askyesno(
                "Overwrite?", f"Project root already has: {', '.join(conflicts)}.\nReplace them?"):
            return

        self.config_store.set("templates_root", templates_root)
        self._dispatch(ApplyTemplateCommand, templates_root=templates_root, template=template)

    def _dispatch(self, command_cls: type[WorkflowCommand], **params) -> None:
        if self.busy:
            messagebox.showinfo("Busy", "Wait for the current operation to finish.")
            return
        project_dir = Path(self.folder_var.get())
        if not project_dir.is_dir():
            messagebox.showerror("Invalid folder", f"Path not found:\n{project_dir}")
            return

        git = GitRunner(project_dir, log=self.log_queue.put)
        command = command_cls(git, CredentialManager(git))

        self._set_busy(True)
        threading.Thread(target=self._worker, args=(command, params), daemon=True).start()

    def _worker(self, command: WorkflowCommand, params: dict) -> None:
        try:
            command.execute(**params)
        except RuntimeError as err:
            self.log_queue.put(f"[!] {err}")
        finally:
            self.log_queue.put("__DONE__")

    def _poll_log_queue(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line == "__DONE__":
                    self._set_busy(False)
                    continue
                self._append_log(line)
        except queue.Empty:
            pass
        self.after(self.POLL_MS, self._poll_log_queue)

    def _append_log(self, line: str) -> None:
        self.log_view.configure(state="normal")
        self.log_view.insert("end", line + "\n")
        self.log_view.see("end")
        self.log_view.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.status_var.set("Working…" if busy else "Ready.")

    def _browse_into(self, var: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(initialdir=var.get() or str(Path.home()))
        if chosen:
            var.set(chosen)


if __name__ == "__main__":
    WorkflowApp().mainloop()
