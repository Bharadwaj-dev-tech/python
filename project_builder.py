import os
import sys
import threading
import subprocess
import shutil
import time
import queue
import json
import math
import re
import functools
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext

# -----------------------------
# Utility helpers & Decorators
# -----------------------------
def resource_path(rel_path: str) -> str:
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, rel_path)

def ui_thread_safe(func):
    """Decorator to ensure function runs on main thread."""
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        self.after(0, lambda: func(self, *args, **kwargs))
    return wrapper

def which_pip_in_venv(venv_path: Path) -> str:
    if sys.platform.startswith("win"):
        pip_path = venv_path / "Scripts" / "pip.exe"
    else:
        pip_path = venv_path / "bin" / "pip"
    return str(pip_path)

def which_python_in_venv(venv_path: Path) -> str:
    if sys.platform.startswith("win"):
        py = venv_path / "Scripts" / "python.exe"
    else:
        py = venv_path / "bin" / "python3"
    return str(py)

def validate_package_name(pkg: str) -> tuple[bool, str]:
    """Validate package name and return (is_valid, error_message)."""
    if not pkg:
        return False, "Package name cannot be empty"
    
    # Extract package name without version specifiers
    base_name = pkg
    for sep in ['==', '>=', '<=', '>', '<', '~=', '!=']:
        if sep in pkg:
            base_name = pkg.split(sep)[0]
            break
    
    # Check for basic security issues
    if any(char in base_name for char in [';', '|', '&', '$', '`']):
        return False, f"Invalid characters in package: {base_name}"
    
    # Check for URL-like patterns (could be dangerous)
    if base_name.startswith(('http://', 'https://', 'git+', 'svn+')):
        return False, "Direct URLs not supported. Use package names only."
    
    # Basic regex for valid package names
    pattern = r'^[a-zA-Z0-9][a-zA-Z0-9._-]*[a-zA-Z0-9]$'
    if not re.match(pattern, base_name):
        return False, f"Invalid package name format: {base_name}"
    
    return True, ""

# -----------------------------
# Configuration Manager
# -----------------------------
class ConfigManager:
    """Manage persistent configuration."""
    
    CONFIG_FILE = Path.home() / ".project_builder_config.json"
    DEFAULT_CONFIG = {
        "last_dir": str(Path.cwd()),
        "recent_projects": [],
        "packages": [],
        "theme": "dark",
        "typing_effect": True,
        "create_readme": True,
        "create_git": False,
        "selected_preset": ""
    }
    
    @classmethod
    def load(cls) -> dict:
        """Load configuration from file."""
        try:
            if cls.CONFIG_FILE.exists():
                with open(cls.CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    # Merge with defaults to ensure all keys exist
                    return {**cls.DEFAULT_CONFIG, **config}
        except Exception as e:
            print(f"Failed to load config: {e}")
        return cls.DEFAULT_CONFIG.copy()
    
    @classmethod
    def save(cls, config: dict) -> bool:
        """Save configuration to file."""
        try:
            cls.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(cls.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"Failed to save config: {e}")
            return False

# -----------------------------
# Project Presets/Templates
# -----------------------------
class ProjectPresets:
    """Predefined project templates."""
    
    PRESETS = {
        "Data Science": [
            "numpy", "pandas", "matplotlib", "seaborn",
            "scikit-learn", "jupyter", "scipy"
        ],
        "Web Development (Flask)": [
            "flask", "flask-sqlalchemy", "flask-wtf",
            "flask-login", "flask-migrate", "requests"
        ],
        "Web Development (Django)": [
            "django", "django-rest-framework", "django-cors-headers",
            "psycopg2-binary", "gunicorn"
        ],
        "Automation & Scripting": [
            "requests", "beautifulsoup4", "selenium",
            "pyautogui", "pandas", "openpyxl"
        ],
        "GUI Development": [
            "pyqt5", "pyside6", "pyautogui",
            "pillow", "pyinstaller"
        ],
        "Machine Learning": [
            "torch", "torchvision", "tensorflow",
            "keras", "scikit-learn", "xgboost"
        ],
        "API Development": [
            "fastapi", "uvicorn", "pydantic",
            "sqlalchemy", "alembic", "python-jose"
        ],
        "Testing": [
            "pytest", "pytest-cov", "pytest-mock",
            "tox", "hypothesis", "factory-boy"
        ],
        "DevOps": [
            "docker", "boto3", "paramiko",
            "fabric", "ansible", "jinja2"
        ],
        "Minimal": []
    }
    
    @classmethod
    def get_preset(cls, name: str) -> list:
        """Get packages for a preset."""
        return cls.PRESETS.get(name, []).copy()
    
    @classmethod
    def get_preset_names(cls) -> list:
        """Get all preset names."""
        return list(cls.PRESETS.keys())

# -----------------------------
# Worker Thread
# -----------------------------
class ProjectWorker(threading.Thread):
    """Background worker for project creation tasks."""
    
    def __init__(self, project_dir: Path, packages: list, msg_queue: queue.Queue,
                 stop_event: threading.Event, options: dict):
        super().__init__(daemon=True)
        self.project_dir = project_dir
        self.packages = packages
        self.msgq = msg_queue
        self.stop_event = stop_event
        self.options = options
        self.success = False
        self.venv_path = None
        self._cleanup_on_cancel = options.get('cleanup_on_fail', False)
        self.project_size = 0
        
    def post(self, kind: str, text: str):
        """Put a message object onto queue for UI to consume."""
        self.msgq.put((kind, text))
    
    def calculate_project_size(self) -> int:
        """Calculate total size of project in bytes."""
        total_size = 0
        try:
            for path in self.project_dir.rglob('*'):
                if path.is_file():
                    total_size += path.stat().st_size
        except Exception:
            pass
        return total_size
    
    def format_size(self, size_bytes: int) -> str:
        """Format bytes to human readable size."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} TB"
    
    def run_command_stream(self, cmd, cwd=None, env=None):
        """Run a command and stream stdout/stderr line by line to UI."""
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                universal_newlines=True,
                bufsize=1,
                errors='replace'  # Handle encoding errors gracefully
            )
        except Exception as e:
            self.post("error", f"Failed to run command: {cmd}\nError: {e}")
            return 1
        
        # Stream lines
        while True:
            if self.stop_event.is_set():
                try:
                    proc.kill()
                except Exception:
                    pass
                self.post("warn", "Operation cancelled by user.")
                return 1
            
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            
            if line:
                self.post("log", line.rstrip())
        
        return proc.returncode
    
    def create_directory_structure(self):
        """Create standard directory structure."""
        directories = [
            "src",
            "tests",
            "docs",
            "data",
            "notebooks",
            "config",
            "logs"
        ]
        
        for directory in directories:
            dir_path = self.project_dir / directory
            dir_path.mkdir(exist_ok=True)
            self.post("log", f"Created directory: {directory}/")
            
            # Create __init__.py in src and tests
            if directory in ["src", "tests"]:
                init_file = dir_path / "__init__.py"
                init_file.touch()
    
    def create_venv(self, venv_path: Path) -> bool:
        """Create virtual environment with error checking."""
        # Check if we have write permissions
        try:
            test_file = venv_path.parent / ".write_test"
            test_file.touch()
            test_file.unlink()
        except PermissionError:
            self.post("error", f"No write permission for: {venv_path.parent}")
            return False
        
        self.post("log", f"Creating virtual environment at: {venv_path} ...")
        
        # Use system Python to create venv
        cmd = [sys.executable, "-m", "venv", str(venv_path)]
        
        # Add --prompt option for nicer venv name
        cmd.extend(["--prompt", self.project_dir.name])
        
        rc = self.run_command_stream(cmd)
        if rc != 0:
            self.post("error", "Virtual environment creation failed.")
            return False
        
        self.post("ok", "Virtual environment created successfully.")
        return True
    
    def install_packages_batch(self, pip_executable: str) -> bool:
        """Install packages in batch mode for speed."""
        if not self.packages:
            return True
        
        self.post("log", f"Installing {len(self.packages)} package(s) in batch...")
        
        # Validate all packages first
        valid_packages = []
        for pkg in self.packages:
            is_valid, msg = validate_package_name(pkg)
            if is_valid:
                valid_packages.append(pkg)
            else:
                self.post("warn", f"Skipping invalid package: {pkg} ({msg})")
        
        if not valid_packages:
            self.post("log", "No valid packages to install.")
            return True
        
        # Try batch install first
        cmd = [pip_executable, "install"] + valid_packages
        rc = self.run_command_stream(cmd)
        
        if rc == 0:
            self.post("ok", "Batch installation successful.")
            return True
        else:
            self.post("warn", "Batch installation failed, trying individual packages...")
            return self.install_packages_individual(pip_executable, valid_packages)
    
    def install_packages_individual(self, pip_executable: str, packages: list) -> bool:
        """Install packages one by one."""
        total = len(packages)
        success_count = 0
        
        for idx, pkg in enumerate(packages, start=1):
            if self.stop_event.is_set():
                return False
            
            self.post("log", f"Installing [{idx}/{total}]: {pkg} ...")
            cmd = [pip_executable, "install", pkg]
            rc = self.run_command_stream(cmd)
            
            if rc == 0:
                success_count += 1
            else:
                self.post("error", f"Failed to install: {pkg}")
            
            # Update progress
            self.post("progress", idx / total)
        
        if success_count == total:
            self.post("ok", "All packages installed successfully.")
            return True
        elif success_count > 0:
            self.post("warn", f"Installed {success_count}/{total} packages.")
            return True
        else:
            self.post("error", "No packages were installed.")
            return False
    
    def write_requirements(self) -> bool:
        """Write requirements.txt file."""
        req_path = self.project_dir / "requirements.txt"
        try:
            with req_path.open("w", encoding="utf-8") as f:
                f.write("# Generated by Project Builder\n")
                f.write(f"# Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                for pkg in self.packages:
                    f.write(pkg.strip() + "\n")
            self.post("ok", f"requirements.txt saved ({req_path})")
            return True
        except Exception as e:
            self.post("error", f"Failed to write requirements.txt: {e}")
            return False
    
    def create_readme(self) -> bool:
        """Create README.md file."""
        readme_path = self.project_dir / "README.md"
        try:
            with readme_path.open("w", encoding="utf-8") as f:
                f.write(f"""# {self.project_dir.name}

## Description
Project created with Project Builder on {datetime.now().strftime('%Y-%m-%d')}

## Project Structure
```
{self.project_dir.name}/
├── src/           # Source code
├── tests/         # Test files
├── docs/          # Documentation
├── data/          # Data files
├── notebooks/     # Jupyter notebooks
├── config/        # Configuration files
├── logs/          # Log files
├── venv/          # Virtual environment (gitignored)
├── requirements.txt
└── README.md
```

## Installation
1. Create virtual environment:
   ```bash
   python -m venv venv
   ```

2. Activate virtual environment:
   - Windows: `venv\\Scripts\\activate`
   - macOS/Linux: `source venv/bin/activate`

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage
```python
# Add your code here
import sys
sys.path.append('src')

def main():
    print("Hello from {self.project_dir.name}!")

if __name__ == "__main__":
    main()
```

## Development
- Run tests: `pytest tests/`
- Format code: `black src/ tests/`
- Lint code: `pylint src/`

## License
Add your license here
""")
            self.post("ok", "README.md created.")
            return True
        except Exception as e:
            self.post("error", f"Failed to create README.md: {e}")
            return False
    
    def create_git_repo(self) -> bool:
        """Initialize git repository."""
        try:
            # Check if git is available
            subprocess.run(["git", "--version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            self.post("warn", "Git not found or not in PATH. Skipping git initialization.")
            return False
        
        self.post("log", "Initializing git repository...")
        
        try:
            # Initialize repository
            subprocess.run(["git", "init"], cwd=self.project_dir, check=True, capture_output=True)
            
            # Create .gitignore
            gitignore_path = self.project_dir / ".gitignore"
            gitignore_content = """# Python
venv/
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# IDE
.vscode/
.idea/
*.swp
*.swo
*~

# OS
.DS_Store
Thumbs.db

# Project specific
data/
logs/
*.db
*.sqlite3
.env
.secrets
"""
            with gitignore_path.open("w", encoding="utf-8") as f:
                f.write(gitignore_content)
            
            # Add files and initial commit
            subprocess.run(["git", "add", "."], cwd=self.project_dir, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial commit: Project created with Project Builder"],
                cwd=self.project_dir,
                check=True,
                capture_output=True
            )
            
            self.post("ok", "Git repository initialized with initial commit.")
            return True
            
        except subprocess.CalledProcessError as e:
            self.post("error", f"Git operation failed: {e}")
            return False
        except Exception as e:
            self.post("error", f"Failed to initialize git: {e}")
            return False
    
    def _cleanup_partial(self):
        """Clean up partially created project if failed early."""
        if self._cleanup_on_cancel and self.venv_path and self.venv_path.exists():
            try:
                shutil.rmtree(self.venv_path)
                self.post("log", "Cleaned up partial virtual environment.")
            except Exception as e:
                self.post("warn", f"Failed to cleanup: {e}")
    
    def run(self):
        """Main worker method."""
        try:
            # 1. Create project folder
            try:
                self.project_dir.mkdir(parents=True, exist_ok=True)
                self.post("ok", f"Project folder: {self.project_dir}")
            except Exception as e:
                self.post("error", f"Cannot create project folder: {e}")
                return
            
            # 2. Create directory structure
            self.create_directory_structure()
            
            # 3. Create virtual environment
            self.venv_path = self.project_dir / "venv"
            if self.venv_path.exists():
                self.post("warn", "venv already exists — reusing.")
            else:
                if not self.create_venv(self.venv_path):
                    self._cleanup_partial()
                    return
            
            # 4. Install packages
            pip_exec = which_pip_in_venv(self.venv_path)
            if not Path(pip_exec).exists():
                # Try alternative using python -m pip
                python_exec = which_python_in_venv(self.venv_path)
                if Path(python_exec).exists():
                    self.post("log", "Using python -m pip (pip binary not found).")
                    pip_exec = python_exec
                    use_python_for_pip = True
                else:
                    self.post("error", "Cannot locate pip in venv. Aborting.")
                    self._cleanup_partial()
                    return
            else:
                use_python_for_pip = False
            
            if self.packages:
                if use_python_for_pip:
                    # Build commands with python -m pip
                    pip_cmd_base = [pip_exec, "-m", "pip"]
                    cmd = pip_cmd_base + ["install"] + self.packages
                    rc = self.run_command_stream(cmd)
                    if rc != 0:
                        self.post("error", "Package installation failed.")
                        self._cleanup_partial()
                        return
                else:
                    if not self.install_packages_batch(pip_exec):
                        self._cleanup_partial()
                        return
            else:
                self.post("log", "No packages to install.")
            
            # 5. Write requirements.txt
            self.write_requirements()
            
            # 6. Create README.md if requested
            if self.options.get('create_readme', True):
                self.create_readme()
            
            # 7. Initialize git repo if requested
            if self.options.get('create_git', False):
                self.create_git_repo()
            
            # 8. Calculate and report project size
            self.project_size = self.calculate_project_size()
            size_str = self.format_size(self.project_size)
            self.post("info", f"Project size: {size_str}")
            
            # 9. Success!
            self.success = True
            self.post("done", f"✅ Project '{self.project_dir.name}' created successfully!")
            self.post("summary", {
                "project_path": str(self.project_dir),
                "project_size": size_str,
                "package_count": len(self.packages),
                "git_initialized": self.options.get('create_git', False)
            })
            
        except Exception as e:
            self.post("error", f"Unexpected error: {e}")
            import traceback
            self.post("error", traceback.format_exc())
        finally:
            # Always send completion signal
            if not self.success:
                self.post("done", "❌ Project creation failed.")
            self.post("progress_complete", None)

# -----------------------------
# Enhanced UI with Themes
# -----------------------------
class ProjectBuilderUI(tk.Tk):
    """Main application window with enhanced UI."""
    
    def __init__(self):
        super().__init__()
        self.title("Project Builder — Enhanced")
        self.geometry("920x680")
        self.minsize(800, 600)
        
        # Load configuration
        self.config = ConfigManager.load()
        
        # Themes
        self.themes = {
            "dark": {
                "bg": "#0f1115",
                "panel": "#16171b",
                "accent": "#3aa0ff",
                "accent_dark": "#2a8be0",
                "fg": "#cdd6e0",
                "subtle": "#8b8f98",
                "success": "#4CAF50",
                "error": "#F44336",
                "warning": "#FF9800",
                "info": "#2196F3"
            },
            "light": {
                "bg": "#f5f7fa",
                "panel": "#ffffff",
                "accent": "#3aa0ff",
                "accent_dark": "#2a8be0",
                "fg": "#333333",
                "subtle": "#666666",
                "success": "#4CAF50",
                "error": "#F44336",
                "warning": "#FF9800",
                "info": "#2196F3"
            }
        }
        
        # Set current theme
        self.current_theme = self.config.get('theme', 'dark')
        self.colors = self.themes[self.current_theme]
        
        # Control variables
        self.selected_dir = tk.StringVar(value=self.config.get('last_dir', str(Path.cwd())))
        self.project_name = tk.StringVar(value="MyProject")
        self.typing_effect = tk.BooleanVar(value=self.config.get('typing_effect', True))
        self.create_readme = tk.BooleanVar(value=self.config.get('create_readme', True))
        self.create_git = tk.BooleanVar(value=self.config.get('create_git', False))
        self.cleanup_on_fail = tk.BooleanVar(value=False)
        self.preset_var = tk.StringVar(value=self.config.get('selected_preset', ''))
        
        # Message queue & control
        self.msgq = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.glow_phase = 0
        self.current_progress = 0
        self.target_progress = 0
        
        # Initialize UI
        self._build_style()
        self._build_widgets()
        self._setup_keyboard_shortcuts()
        self._periodic_check()
        self._animate_glow()
        
        # Center window on screen
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = (self.winfo_screenwidth() // 2) - (width // 2)
        y = (self.winfo_screenheight() // 2) - (height // 2)
        self.geometry(f'{width}x{height}+{x}+{y}')
        
    def _build_style(self):
        """Configure ttk styles."""
        style = ttk.Style(self)
        style.theme_use("clam")
        
        # Configure colors
        style.configure("TFrame", background=self.colors["panel"])
        style.configure("TLabel", background=self.colors["panel"], foreground=self.colors["fg"])
        style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"))
        
        # Button styles
        style.configure("TButton", 
                       background=self.colors["accent"],
                       foreground="white",
                       borderwidth=1,
                       relief="flat",
                       padding=(10, 5),
                       font=("Segoe UI", 10, "bold"))
        style.map("TButton",
                 background=[("active", self.colors["accent_dark"]),
                           ("disabled", self.colors["subtle"])],
                 relief=[("pressed", "sunken")])
        
        # Entry styles
        style.configure("TEntry",
                       fieldbackground=self.colors["bg"],
                       foreground=self.colors["fg"],
                       borderwidth=1,
                       relief="solid")
        
        # Checkbutton styles
        style.configure("TCheckbutton",
                       background=self.colors["panel"],
                       foreground=self.colors["fg"])
        
        # Combobox styles
        style.configure("TCombobox",
                       fieldbackground=self.colors["bg"],
                       foreground=self.colors["fg"])
        
        # Progressbar style
        style.configure("Custom.Horizontal.TProgressbar",
                       troughcolor=self.colors["subtle"],
                       background=self.colors["accent"],
                       bordercolor=self.colors["subtle"],
                       lightcolor=self.colors["accent"],
                       darkcolor=self.colors["accent_dark"])
        
        # Configure window background
        self.configure(bg=self.colors["bg"])
        
    def _build_widgets(self):
        """Build all UI widgets."""
        # Top header
        header_frame = ttk.Frame(self, style="TFrame")
        header_frame.pack(fill="x", padx=20, pady=(15, 10))
        
        ttk.Label(header_frame, text="🚀 Project Builder", style="Header.TLabel").pack(side="left")
        
        # Theme toggle button
        theme_text = "🌙" if self.current_theme == "dark" else "☀️"
        self.theme_btn = ttk.Button(header_frame, text=theme_text, 
                                   command=self._toggle_theme, width=3)
        self.theme_btn.pack(side="right", padx=(0, 10))
        
        # Main content (two panes)
        main_container = ttk.Frame(self, style="TFrame")
        main_container.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        
        # Left pane (controls)
        left_pane = ttk.Frame(main_container, style="TFrame")
        left_pane.pack(side="left", fill="y", padx=(0, 15))
        
        # Right pane (logs & progress)
        right_pane = ttk.Frame(main_container, style="TFrame")
        right_pane.pack(side="left", fill="both", expand=True)
        
        # ========== LEFT PANE ==========
        # Project Info Section
        info_frame = ttk.LabelFrame(left_pane, text="Project Information", style="TFrame")
        info_frame.pack(fill="x", pady=(0, 15))
        
        # Project name
        ttk.Label(info_frame, text="Project Name").pack(anchor="w", pady=(10, 0))
        name_entry = ttk.Entry(info_frame, textvariable=self.project_name, font=("Segoe UI", 11))
        name_entry.pack(fill="x", padx=10, pady=5)
        self._create_tooltip(name_entry, "Enter a name for your project")
        
        # Project folder
        ttk.Label(info_frame, text="Project Folder").pack(anchor="w", pady=(10, 0))
        folder_frame = ttk.Frame(info_frame, style="TFrame")
        folder_frame.pack(fill="x", padx=10, pady=5)
        
        folder_entry = ttk.Entry(folder_frame, textvariable=self.selected_dir)
        folder_entry.pack(side="left", fill="x", expand=True)
        
        browse_btn = ttk.Button(folder_frame, text="Browse", 
                               command=self._browse_folder, width=8)
        browse_btn.pack(side="left", padx=(5, 0))
        
        # Presets Section
        presets_frame = ttk.LabelFrame(left_pane, text="Project Templates", style="TFrame")
        presets_frame.pack(fill="x", pady=(0, 15))
        
        ttk.Label(presets_frame, text="Select a template:").pack(anchor="w", padx=10, pady=(10, 0))
        self.preset_combo = ttk.Combobox(presets_frame, textvariable=self.preset_var,
                                        values=[""] + ProjectPresets.get_preset_names(),
                                        state="readonly", font=("Segoe UI", 10))
        self.preset_combo.pack(fill="x", padx=10, pady=5)
        self.preset_combo.bind("<<ComboboxSelected>>", self._apply_preset)
        self._create_tooltip(self.preset_combo, "Select a template to auto-fill packages")
        
        # Packages Section
        packages_frame = ttk.LabelFrame(left_pane, text="Python Packages", style="TFrame")
        packages_frame.pack(fill="both", expand=True, pady=(0, 15))
        
        # Packages text widget with scrollbar
        packages_container = ttk.Frame(packages_frame, style="TFrame")
        packages_container.pack(fill="both", expand=True, padx=10, pady=10)
        
        self.txt_packages = scrolledtext.ScrolledText(
            packages_container,
            height=8,
            bg=self.colors["bg"],
            fg=self.colors["fg"],
            insertbackground=self.colors["fg"],
            font=("Consolas", 9),
            relief="solid",
            borderwidth=1
        )
        self.txt_packages.pack(fill="both", expand=True)
        
        # Load saved packages
        saved_packages = self.config.get('packages', [])
        if saved_packages:
            self.txt_packages.insert("1.0", "\n".join(saved_packages))
        
        self._create_tooltip(self.txt_packages, 
                           "Enter packages (one per line or space separated)\n"
                           "Examples:\nnumpy==1.24.0\npandas\nmatplotlib>=3.5")
        
        # Options Section
        options_frame = ttk.LabelFrame(left_pane, text="Options", style="TFrame")
        options_frame.pack(fill="x", pady=(0, 15))
        
        # Two columns for options
        options_col1 = ttk.Frame(options_frame, style="TFrame")
        options_col1.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        
        options_col2 = ttk.Frame(options_frame, style="TFrame")
        options_col2.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        
        ttk.Checkbutton(options_col1, text="Typing effect", 
                       variable=self.typing_effect).pack(anchor="w", pady=2)
        ttk.Checkbutton(options_col1, text="Create README.md", 
                       variable=self.create_readme).pack(anchor="w", pady=2)
        ttk.Checkbutton(options_col1, text="Cleanup on failure", 
                       variable=self.cleanup_on_fail).pack(anchor="w", pady=2)
        
        ttk.Checkbutton(options_col2, text="Initialize Git repo", 
                       variable=self.create_git).pack(anchor="w", pady=2)
        
        # Action Buttons
        action_frame = ttk.Frame(left_pane, style="TFrame")
        action_frame.pack(fill="x")
        
        self.btn_create = ttk.Button(action_frame, text="🚀 Create Project", 
                                    command=self.on_create, style="TButton")
        self.btn_create.pack(fill="x", pady=(0, 5))
        
        self.btn_cancel = ttk.Button(action_frame, text="✋ Cancel / Stop", 
                                    command=self.on_cancel, style="TButton")
        self.btn_cancel.pack(fill="x")
        self.btn_cancel.state(["disabled"])
        
        # ========== RIGHT PANE ==========
        # Progress Section
        progress_frame = ttk.LabelFrame(right_pane, text="Progress", style="TFrame")
        progress_frame.pack(fill="x", pady=(0, 15))
        
        # Progress bar
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_bar = ttk.Progressbar(
            progress_frame,
            style="Custom.Horizontal.TProgressbar",
            orient="horizontal",
            mode="determinate",
            variable=self.progress_var,
            maximum=1.0
        )
        self.progress_bar.pack(fill="x", padx=10, pady=(5, 0))
        
        # Status and size labels
        status_frame = ttk.Frame(progress_frame, style="TFrame")
        status_frame.pack(fill="x", padx=10, pady=5)
        
        self.status_label = ttk.Label(status_frame, text="Ready", 
                                     font=("Segoe UI", 9), style="TLabel")
        self.status_label.pack(side="left")
        
        self.size_label = ttk.Label(status_frame, text="", 
                                   foreground=self.colors["subtle"],
                                   font=("Segoe UI", 9), style="TLabel")
        self.size_label.pack(side="right")
        
        # Log Output Section
        log_frame = ttk.LabelFrame(right_pane, text="Log Output", style="TFrame")
        log_frame.pack(fill="both", expand=True)
        
        # Log text widget with custom tags for colors
        self.log_text = tk.Text(
            log_frame,
            bg=self.colors["bg"],
            fg=self.colors["fg"],
            insertbackground=self.colors["fg"],
            font=("Consolas", 9),
            wrap="word",
            borderwidth=0,
            highlightthickness=0
        )
        self.log_text.pack(fill="both", expand=True, padx=2, pady=2)
        
        # Configure text tags for colors
        self.log_text.tag_config("success", foreground=self.colors["success"])
        self.log_text.tag_config("error", foreground=self.colors["error"])
        self.log_text.tag_config("warning", foreground=self.colors["warning"])
        self.log_text.tag_config("info", foreground=self.colors["info"])
        self.log_text.tag_config("subtle", foreground=self.colors["subtle"])
        
        # Log control buttons
        log_controls = ttk.Frame(log_frame, style="TFrame")
        log_controls.pack(fill="x", padx=5, pady=5)
        
        ttk.Button(log_controls, text="📋 Copy", command=self._copy_log, width=8).pack(side="left", padx=2)
        ttk.Button(log_controls, text="🗑️ Clear", command=self._clear_log, width=8).pack(side="left", padx=2)
        ttk.Button(log_controls, text="📁 Open Project", command=self._open_project, width=12).pack(side="right", padx=2)
        
        # Make log widget read-only
        self.log_text.config(state="disabled")
    
    def _create_tooltip(self, widget, text):
        """Create a tooltip for a widget."""
        def show_tooltip(event):
            # Create tooltip window
            tooltip = tk.Toplevel(self)
            tooltip.wm_overrideredirect(True)
            tooltip.wm_geometry(f"+{event.x_root+10}+{event.y_root+10}")
            
            # Style tooltip
            tooltip.configure(bg="#ffffe0", relief="solid", borderwidth=1)
            
            label = tk.Label(tooltip, text=text, bg="#ffffe0", fg="#000000",
                           justify="left", padx=8, pady=4, font=("Segoe UI", 9))
            label.pack()
            
            widget.tooltip = tooltip
        
        def hide_tooltip(event):
            if hasattr(widget, 'tooltip') and widget.tooltip:
                widget.tooltip.destroy()
                delattr(widget, 'tooltip')
        
        widget.bind("<Enter>", show_tooltip)
        widget.bind("<Leave>", hide_tooltip)
    
    def _setup_keyboard_shortcuts(self):
        """Setup keyboard shortcuts."""
        self.bind("<Control-o>", lambda e: self._browse_folder())
        self.bind("<Control-n>", lambda e: self.on_create())
        self.bind("<Control-c>", lambda e: self.on_cancel())
        self.bind("<Control-l>", lambda e: self._clear_log())
        self.bind("<Control-s>", lambda e: self._copy_log())
        self.bind("<F5>", lambda e: self._refresh_ui())
        self.bind("<Escape>", lambda e: self.on_cancel())
    
    # -----------------------
    # UI Actions
    # -----------------------
    def _browse_folder(self):
        """Browse for project folder."""
        initial_dir = self.selected_dir.get() or os.getcwd()
        folder = filedialog.askdirectory(initialdir=initial_dir, title="Select Project Folder")
        if folder:
            self.selected_dir.set(folder)
            self._save_config()
    
    def _apply_preset(self, event=None):
        """Apply selected preset to packages."""
        preset_name = self.preset_var.get()
        if preset_name:
            packages = ProjectPresets.get_preset(preset_name)
            self.txt_packages.delete("1.0", "end")
            self.txt_packages.insert("1.0", "\n".join(packages))
            self._save_config()
    
    def _toggle_theme(self):
        """Toggle between dark and light themes."""
        self.current_theme = "light" if self.current_theme == "dark" else "dark"
        self.colors = self.themes[self.current_theme]
        
        # Update config
        self.config['theme'] = self.current_theme
        self._save_config()
        
        # Update theme button
        theme_text = "🌙" if self.current_theme == "dark" else "☀️"
        self.theme_btn.config(text=theme_text)
        
        # Rebuild widgets with new theme
        self._build_style()
        self._update_widget_colors()
    
    def _update_widget_colors(self):
        """Update widget colors after theme change."""
        # Update text widgets
        self.txt_packages.config(bg=self.colors["bg"], fg=self.colors["fg"],
                                insertbackground=self.colors["fg"])
        self.log_text.config(bg=self.colors["bg"], fg=self.colors["fg"],
                            insertbackground=self.colors["fg"])
        
        # Update text tags
        self.log_text.tag_config("success", foreground=self.colors["success"])
        self.log_text.tag_config("error", foreground=self.colors["error"])
        self.log_text.tag_config("warning", foreground=self.colors["warning"])
        self.log_text.tag_config("info", foreground=self.colors["info"])
        self.log_text.tag_config("subtle", foreground=self.colors["subtle"])
        
        # Update size label
        self.size_label.config(foreground=self.colors["subtle"])
        
        # Force redraw
        self.update_idletasks()
    
    @ui_thread_safe
    def on_create(self):
        """Start project creation."""
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "A build is already in progress. Use Cancel to stop.")
            return
        
        # Validate project name
        pname = self.project_name.get().strip()
        if not pname:
            messagebox.showwarning("Input Error", "Please enter a project name.")
            return
        
        # Validate project name characters
        if not re.match(r'^[a-zA-Z0-9_\- ]+$', pname):
            messagebox.showwarning("Input Error", 
                                 "Project name can only contain letters, numbers, spaces, hyphens, and underscores.")
            return
        
        # Get parent directory
        try:
            parent_dir = Path(self.selected_dir.get()).expanduser().resolve()
        except Exception as e:
            messagebox.showerror("Path Error", f"Invalid project folder path: {e}")
            return
        
        # Check write permissions
        try:
            test_file = parent_dir / ".write_test"
            test_file.touch()
            test_file.unlink()
        except PermissionError:
            messagebox.showerror("Permission Error", 
                               f"No write permission for: {parent_dir}")
            return
        except Exception as e:
            messagebox.showerror("Path Error", f"Cannot access folder: {e}")
            return
        
        project_dir = parent_dir / pname
        
        # Warn if project already exists
        if project_dir.exists():
            if not messagebox.askyesno("Overwrite?", 
                                      f"Project '{pname}' already exists.\nDo you want to overwrite it?"):
                return
        
        # Read packages
        raw = self.txt_packages.get("1.0", "end").strip()
        packages = []
        if raw:
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Split by spaces, but handle version specifiers
                parts = re.split(r'\s+', line)
                packages.extend(parts)
        
        # Validate packages
        validated_packages = []
        for pkg in packages:
            is_valid, msg = validate_package_name(pkg)
            if is_valid:
                validated_packages.append(pkg)
            else:
                if not messagebox.askyesno("Invalid Package", 
                                         f"Package '{pkg}' is invalid: {msg}\nContinue anyway?"):
                    return
        
        # Save config
        self.config['packages'] = packages
        self.config['selected_preset'] = self.preset_var.get()
        self._save_config()
        
        # Clear log and reset UI
        self._clear_log()
        self.progress_var.set(0.0)
        self.current_progress = 0
        self.target_progress = 0
        self.size_label.config(text="")
        
        # Show project info
        self._append_log(f"🚀 Starting project creation...", "info")
        self._append_log(f"Project: {project_dir}", "info")
        self._append_log(f"Packages: {len(validated_packages)}", "info")
        self._append_log(f"Options: README={self.create_readme.get()}, Git={self.create_git.get()}", "info")
        self._append_log("-" * 50, "subtle")
        
        # Setup worker options
        options = {
            'create_readme': self.create_readme.get(),
            'create_git': self.create_git.get(),
            'cleanup_on_fail': self.cleanup_on_fail.get()
        }
        
        # Start worker
        self.stop_event.clear()
        self.worker = ProjectWorker(project_dir, validated_packages, 
                                   self.msgq, self.stop_event, options)
        self.worker.start()
        
        # Update UI state
        self.btn_create.state(["disabled"])
        self.btn_cancel.state(["!disabled"])
        self._set_status("Creating project...")
    
    @ui_thread_safe
    def on_cancel(self):
        """Cancel current operation."""
        if self.worker and self.worker.is_alive():
            if messagebox.askyesno("Cancel", "Stop the current operation?"):
                self.stop_event.set()
                self._append_log("⚠️ Cancellation requested...", "warning")
                self._set_status("Cancelling...")
        else:
            self._append_log("No active operation to cancel.", "info")
    
    def _copy_log(self):
        """Copy log contents to clipboard."""
        try:
            content = self.log_text.get("1.0", "end-1c")
            self.clipboard_clear()
            self.clipboard_append(content)
            self._append_log("Log copied to clipboard", "success")
        except Exception as e:
            self._append_log(f"Failed to copy log: {e}", "error")
    
    def _clear_log(self):
        """Clear log window."""
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self._append_log("Log cleared", "subtle")
    
    def _open_project(self):
        """Open project folder in file explorer."""
        try:
            project_path = Path(self.selected_dir.get()) / self.project_name.get()
            if project_path.exists():
                if sys.platform.startswith("win"):
                    os.startfile(project_path)
                elif sys.platform == "darwin":  # macOS
                    subprocess.run(["open", str(project_path)])
                else:  # Linux
                    subprocess.run(["xdg-open", str(project_path)])
            else:
                messagebox.showinfo("Not Found", "Project folder doesn't exist yet.")
        except Exception as e:
            messagebox.showerror("Error", f"Cannot open folder: {e}")
    
    def _refresh_ui(self):
        """Refresh UI elements."""
        self.update_idletasks()
    
    def _save_config(self):
        """Save current configuration."""
        self.config['last_dir'] = self.selected_dir.get()
        self.config['typing_effect'] = self.typing_effect.get()
        self.config['create_readme'] = self.create_readme.get()
        self.config['create_git'] = self.create_git.get()
        ConfigManager.save(self.config)
    
    # -----------------------
    # Log & UI helpers
    # -----------------------
    def _append_log(self, text: str, tag: str = "log"):
        """Append text to log with appropriate tag."""
        self.log_text.config(state="normal")
        
        # Map tag names to color tags
        tag_map = {
            "ok": "success",
            "error": "error",
            "warn": "warning",
            "log": "info",
            "info": "info",
            "subtle": "subtle",
            "success": "success",
            "warning": "warning"
        }
        
        color_tag = tag_map.get(tag, "info")
        
        # Typing effect if enabled
        if self.typing_effect.get() and tag not in ["error", "warning"]:
            self.log_text.insert("end", text + "\n", color_tag)
            self.log_text.see("end")
            self.log_text.update_idletasks()
        else:
            self.log_text.insert("end", text + "\n", color_tag)
            self.log_text.see("end")
        
        self.log_text.config(state="disabled")
    
    @ui_thread_safe
    def _set_status(self, text: str):
        """Update status label."""
        self.status_label.config(text=text)
    
    def _animate_glow(self):
        """Animate button glow effect."""
        try:
            if self.btn_create.instate(["disabled"]):
                # Don't animate when disabled
                self.after(100, self._animate_glow)
                return
            
            # Calculate glow intensity
            self.glow_phase = (self.glow_phase + 0.05) % (2 * math.pi)
            intensity = 0.08 + 0.08 * (1 + math.sin(self.glow_phase)) / 2
            
            # Adjust color based on theme
            base_color = self.colors["accent"]
            if self.current_theme == "dark":
                # Parse hex color
                r, g, b = int(base_color[1:3], 16), int(base_color[3:5], 16), int(base_color[5:7], 16)
                # Lighten
                r = min(255, int(r + (255 - r) * intensity))
                g = min(255, int(g + (255 - g) * intensity))
                b = min(255, int(b + (255 - b) * intensity))
                glow_color = f"#{r:02x}{g:02x}{b:02x}"
                
                # Create temporary style for glow
                style = ttk.Style()
                style.configure("Glow.TButton", background=glow_color)
                self.btn_create.configure(style="Glow.TButton")
        except Exception:
            pass
        
        self.after(120, self._animate_glow)
    
    def _animate_progress(self):
        """Smoothly animate progress bar."""
        if abs(self.current_progress - self.target_progress) > 0.001:
            # Ease-in-out animation
            diff = self.target_progress - self.current_progress
            self.current_progress += diff * 0.2
            self.progress_var.set(self.current_progress)
            self.after(20, self._animate_progress)
    
    def _periodic_check(self):
        """Check message queue and update UI."""
        processed = False
        
        while True:
            try:
                kind, payload = self.msgq.get_nowait()
                processed = True
                
                if kind == "log":
                    self._append_log(payload, "log")
                elif kind == "progress":
                    self.target_progress = float(payload)
                    self._animate_progress()
                elif kind == "progress_complete":
                    self.target_progress = 1.0
                    self._animate_progress()
                elif kind == "ok":
                    self._append_log(payload, "success")
                elif kind == "warn":
                    self._append_log(payload, "warning")
                elif kind == "error":
                    self._append_log(payload, "error")
                elif kind == "info":
                    self._append_log(payload, "info")
                elif kind == "done":
                    self._append_log(payload, "success" if "✅" in payload else "error")
                    self._set_status("Done" if "✅" in payload else "Failed")
                    
                    # Enable/disable buttons
                    self.btn_create.state(["!disabled"])
                    self.btn_cancel.state(["disabled"])
                    
                    # Save last successful project location
                    if "✅" in payload:
                        self.config['last_dir'] = str(Path(self.selected_dir.get()).resolve())
                        ConfigManager.save(self.config)
                elif kind == "summary":
                    # Display project summary
                    project_path = payload.get("project_path", "")
                    project_size = payload.get("project_size", "0 B")
                    package_count = payload.get("package_count", 0)
                    git_initialized = payload.get("git_initialized", False)
                    
                    self._append_log("=" * 50, "subtle")
                    self._append_log("📊 PROJECT SUMMARY:", "info")
                    self._append_log(f"  Path: {project_path}", "info")
                    self._append_log(f"  Size: {project_size}", "info")
                    self._append_log(f"  Packages: {package_count}", "info")
                    self._append_log(f"  Git: {'✅ Yes' if git_initialized else '❌ No'}", "info")
                    
                    # Update size label
                    self.size_label.config(text=f"Size: {project_size}")
                    
                    # Save project to recent projects
                    if project_path:
                        recent = self.config.get('recent_projects', [])
                        if project_path not in recent:
                            recent.insert(0, project_path)
                            recent = recent[:10]  # Keep only 10 most recent
                            self.config['recent_projects'] = recent
                            ConfigManager.save(self.config)
                
            except queue.Empty:
                break
        
        # Check if worker finished
        if self.worker and not self.worker.is_alive() and not processed:
            # Worker finished without sending done message
            self.btn_create.state(["!disabled"])
            self.btn_cancel.state(["disabled"])
            if self.status_label.cget("text") not in ["Done", "Failed"]:
                self._set_status("Idle")
        
        self.after(100, self._periodic_check)

# -----------------------------
# Application Entry Point
# -----------------------------
def main():
    """Main entry point for the application."""
    try:
        # Create and run application
        app = ProjectBuilderUI()
        
        # Set window icon if available
        try:
            icon_path = resource_path("icon.ico")
            if Path(icon_path).exists():
                app.iconbitmap(icon_path)
        except Exception:
            pass
        
        # Start main loop
        app.mainloop()
        
    except Exception as e:
        # Show error message if app fails to start
        error_msg = f"Failed to start application:\n\n{str(e)}"
        messagebox.showerror("Fatal Error", error_msg)
        sys.exit(1)

if __name__ == "__main__":
    main()
