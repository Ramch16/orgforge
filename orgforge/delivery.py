"""Export the exact signed-off Git tree, excluding untracked local files."""
from pathlib import Path
import subprocess
import tempfile
import os

from .pipeline import PipelineError


def export_product(pipeline, pid: int, destination: Path) -> Path:
    project = pipeline.project(pid)
    if project['stage'] not in ('done', 'live'):
        raise PipelineError('Only a signed-off, verified product can be exported.')
    ws = pipeline.workspace(project)
    if ws.changed_files() != '(no uncommitted changes)' or ws.git('rev-parse', 'HEAD') != ws.git('rev-parse', 'release'):
        raise PipelineError('Workspace differs from the signed-off release.')
    destination = destination.resolve()
    if destination == ws.root or ws.root in destination.parents:
        raise PipelineError('Export outside the product workspace to keep the release clean.')
    if destination.exists():
        raise PipelineError('Output already exists. Choose a new path.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=destination.parent, suffix='.zip')
    os.close(fd)
    try:
        result = subprocess.run(['git', 'archive', '--format=zip', f'--output={temp}', 'release'],
                                cwd=ws.root, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise PipelineError(f'Could not export release: {result.stderr}')
        # Link atomically without replacing an output created concurrently.
        os.link(temp, destination)
    finally:
        Path(temp).unlink(missing_ok=True)
    return destination
