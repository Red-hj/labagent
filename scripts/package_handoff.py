"""Package code and task sheets; never include local secrets or runtime data."""
from pathlib import Path
import argparse
import sys
import zipfile
import re
import shutil

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="Package project sources and an optional update from a prior team package")
    parser.add_argument("--base-version", help="Build a changed-files-only archive from this existing version")
    args = parser.parse_args()
    if args.base_version and not re.fullmatch(r"\d+\.\d+\.\d+", args.base_version):
        parser.error("base-version must be a version such as 0.6.0")
    output = ROOT / "handoff" / "LabAgent_team.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    files = [ROOT / name for name in ("AGENTS.md", "README.md", "app.py", "agentarts_app.py", "start.cmd", "setup.cmd", "requirements.txt", "requirements-workflow.txt", "requirements-agentarts.txt", ".env.example", ".gitignore", "Dockerfile", "Dockerfile.agentarts", ".dockerignore", "render.yaml")]
    for directory in ("labagent", "web", "scripts", "tests", "docs", "data"):
        files.extend(file for file in (ROOT / directory).rglob("*") if file.is_file()
                     and "__pycache__" not in file.parts and file.suffix not in (".pyc", ".sqlite3")
                     and file.name != ".env" and file.relative_to(ROOT).parts[:2] != ("data", "course_notes"))
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(files):
            archive.write(file, "LabAgent/" + file.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Archive integrity check failed")
        assert not any(name.startswith("LabAgent/runtime/") or name.endswith("/.env") for name in archive.namelist())
        assert not any(name.startswith("LabAgent/data/course_notes/") for name in archive.namelist())
        print(f"Packaged {len(archive.namelist())} files")
    version = re.search(r'__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"', (ROOT / "labagent/__init__.py").read_text(encoding="utf-8")).group(1)
    versioned = output.with_name(f"LabAgent_team_v{version}.zip")
    shutil.copyfile(output, versioned)
    print(output)
    print(versioned)
    if args.base_version:
        baseline = output.with_name(f"LabAgent_team_v{args.base_version}.zip")
        if not baseline.exists() or args.base_version == version:
            parser.error("A different, existing baseline package is required")
        update = output.with_name(f"LabAgent_update_v{args.base_version}_to_v{version}.zip")
        with zipfile.ZipFile(baseline) as old, zipfile.ZipFile(versioned) as current, zipfile.ZipFile(update, "w", compression=zipfile.ZIP_DEFLATED) as patch:
            previous = set(old.namelist())
            changed = [name for name in current.namelist() if name not in previous or old.read(name) != current.read(name)]
            for name in changed:
                patch.writestr(name, current.read(name))
            removed = sorted(previous - set(current.namelist()))
        with zipfile.ZipFile(update) as patch:
            assert patch.testzip() is None
        print(f"Update contains {len(changed)} changed/new files; {len(removed)} removed files (no deletions are applied).")
        print("\n".join(name.removeprefix("LabAgent/") for name in changed))
        print(update)


if __name__ == "__main__":
    main()
