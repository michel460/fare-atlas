"""Finding and loading config.yaml.

Every tool here needs the same file, and they were each building the path
themselves, which is how the code ended up looking in `tracker/` while the
README told you to create it at the repository root. One resolver, three
places it looks:

  $FARE_CONFIG            an explicit path, wins over everything
  <this directory>        alongside the scripts
  <parent directory>      the repository root, next to config.example.yaml

Missing, it says so in a sentence you can act on rather than a stack trace.
"""
import os

import yaml

DIR = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(os.path.dirname(DIR), "config.example.yaml")


class Missing(Exception):
    """No config.yaml anywhere we look."""


def path():
    """Where config.yaml is, or where it should go if it does not exist."""
    explicit = os.environ.get("FARE_CONFIG")
    if explicit:
        return os.path.abspath(explicit)
    for candidate in (os.path.join(DIR, "config.yaml"),
                      os.path.join(os.path.dirname(DIR), "config.yaml")):
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(os.path.dirname(DIR), "config.yaml")


def load():
    p = path()
    if not os.path.isfile(p):
        raise Missing(
            "no config.yaml yet. Copy the example and edit it:\n"
            "    cp %s %s" % (
                os.path.relpath(EXAMPLE) if os.path.isfile(EXAMPLE) else "config.example.yaml",
                os.path.relpath(p)))
    with open(p) as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise Missing("%s does not look like a config file" % p)
    return cfg
