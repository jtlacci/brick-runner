"""Read a brick repository through Git only, so every input is pinned by a hash."""

from __future__ import annotations

from dataclasses import dataclass
import io
from pathlib import Path
import subprocess
import tarfile


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Repo:
    path: Path

    def git(self, *args: str, binary: bool = False) -> str | bytes:
        result = subprocess.run(["git", *args], cwd=self.path, capture_output=True, check=False)
        if result.returncode != 0:
            raise GitError(result.stderr.decode(errors="replace").strip() or f"git {' '.join(args)} failed")
        return result.stdout if binary else result.stdout.decode()

    def commit(self, ref: str) -> str:
        """The full commit hash for a ref. Refuses anything that is not a commit."""
        return str(self.git("rev-parse", "--verify", f"{ref}^{{commit}}")).strip()

    def tree(self, commit: str, path: str) -> str | None:
        """The Git tree or blob hash of a path at a commit, or None."""
        try:
            return str(self.git("rev-parse", f"{commit}:{path}")).strip()
        except GitError:
            return None

    def show(self, commit: str, path: str) -> str | None:
        try:
            return str(self.git("show", f"{commit}:{path}"))
        except GitError:
            return None

    def files(self, commit: str, prefix: str) -> list[str]:
        """Every file path under a prefix at a commit."""
        out = str(self.git("ls-tree", "-r", "--name-only", commit, "--", prefix))
        return [line for line in out.splitlines() if line]

    def export(self, commit: str, destination: Path, paths: list[str] | None = None) -> None:
        """Write the tree of a commit (or some of its paths) into a folder."""
        data = self.git("archive", "--format=tar", commit, *(["--", *paths] if paths else []), binary=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:  # type: ignore[arg-type]
            archive.extractall(destination, filter="data")

    def commit_time(self, commit: str) -> str:
        return str(self.git("show", "-s", "--format=%cI", commit)).strip()
