#!/usr/bin/env python3
"""Shared filesystem guards for explicit project operations (Python 3.9+).

Checks reject root/ancestor links and detect directory identity changes during
normal use; they do not guarantee safety against adversarial filesystem races.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat


class OperationError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def fail(code, message):
    raise OperationError(code, message)


def is_reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def identity(info):
    return info.st_dev, info.st_ino


class RootGuard:
    def __init__(self, value):
        path = Path(value).expanduser()
        if ".." in path.parts:
            fail("unsafe_path", "Parent traversal ('..') is not accepted; provide the intended directory directly.")
        self.path = Path(os.path.abspath(path))
        self.original = None
        self.check()
        self.original = identity(self.path.lstat())

    def check(self):
        for path in [*reversed(self.path.parents), self.path]:
            try:
                info = path.lstat()
            except FileNotFoundError:
                fail("missing_root", "The selected root and its parents must already exist.")
            if is_reparse(info):
                fail("reparse_point", "The selected root or an ancestor is a symlink, junction, or reparse point.")
            if not stat.S_ISDIR(info.st_mode):
                fail("not_directory", "The selected root and its parents must be directories.")
        if self.original is not None and identity(self.path.lstat()) != self.original:
            fail("root_changed", "The selected directory changed during this operation; no further writes are allowed.")

    def item(self, name, expected=None):
        self.check()
        path = self.path / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if is_reparse(info):
            fail("reparse_point", "A planned item is a symlink, junction, or reparse point: " + name)
        if expected == "file" and not stat.S_ISREG(info.st_mode):
            fail("invalid_item", "A regular file is required: " + name)
        if expected == "directory" and not stat.S_ISDIR(info.st_mode):
            fail("invalid_item", "A directory is required: " + name)
        return info
