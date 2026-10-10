"""Where Claude Code keeps its files, and which project's sessions to read.

config_dir() honors CLAUDE_CONFIG_DIR (else ~/.claude). Sessions live under
<config>/projects/<slug>/, where slug is the project's absolute, symlink-resolved
path with every non-alphanumeric character replaced by '-'
(e.g. /a/b.c -> -a-b-c, /tmp on macOS -> -private-tmp).
Standard library only; imported by measure/, audit/ and ab/ scripts.
"""
import os
import re


class ScopeError(Exception):
    """The requested project has no session directory."""


def config_dir():
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def config_path(*parts):
    return os.path.join(config_dir(), *parts)


def projects_dir():
    return config_path("projects")


def project_slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(os.path.expanduser(path)))


def add_scope_args(ap):
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--project", metavar="PATH",
                   help="read sessions of the project at PATH (default: the current directory)")
    g.add_argument("--all-projects", action="store_true",
                   help="read sessions of every project")


def scoped_dir(project=None, all_projects=False, root=None):
    """Return the directory to read: root (all projects) or root/<slug of project or cwd>.

    Raises ScopeError when the project's directory does not exist (fail closed)."""
    root = root or projects_dir()
    if all_projects:
        return root
    target = project or os.getcwd()
    d = os.path.join(root, project_slug(target))
    if not os.path.isdir(d):
        raise ScopeError("no Claude Code sessions for project %s (looked in %s). "
                         "Pass --project PATH or --all-projects."
                         % (os.path.realpath(os.path.expanduser(target)), d))
    return d


def resolve_root(path=None, project=None, all_projects=False):
    """Scope an explicit (or default) transcript path.

    A directory that directly holds *.jsonl files is one project's session dir: read as-is.
    Anything else is a projects root: scoped like scoped_dir()."""
    if path and os.path.isdir(path) and any(f.endswith(".jsonl") for f in os.listdir(path)):
        return path
    return scoped_dir(project, all_projects, root=path)
