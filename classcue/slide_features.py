"""Lossless scratch features and bounded views for long-held slide pages."""

from collections import OrderedDict
from collections.abc import Mapping
import hashlib
from pathlib import Path
import tempfile

import numpy as np


class FeatureReference(Mapping):
    def __init__(self, store, key):
        self.store, self.key = store, key

    def __getitem__(self, name):
        return self.store.read(self.key)[name]

    def __iter__(self):
        return iter(("rgb", "gray", "edges", "valid"))

    def __len__(self):
        return 4


class FeatureStore:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.temporary = tempfile.TemporaryDirectory(
            prefix="features-", dir=self.root, ignore_cleanup_errors=True)
        self.directory = Path(self.temporary.name)
        self.recent = OrderedDict()
        self.references = {}

    def store(self, feature):
        digest = hashlib.sha256()
        for name in ("rgb", "gray", "edges", "valid"):
            value = feature[name]
            digest.update(repr((name, value.shape, value.dtype.str)).encode())
            digest.update(value.tobytes())
        key = digest.hexdigest()
        if key not in self.references:
            np.savez_compressed(self.directory / f"{key}.npz", **feature)
            self.references[key] = FeatureReference(self, key)
        self._remember(key, feature)
        return self.references[key]

    def _remember(self, key, feature):
        self.recent[key] = feature
        self.recent.move_to_end(key)
        while len(self.recent) > 16:
            self.recent.popitem(last=False)

    def read(self, key):
        if key not in self.recent:
            with np.load(self.directory / f"{key}.npz", allow_pickle=False) as archive:
                self._remember(key, {name: archive[name] for name in archive.files})
        self.recent.move_to_end(key)
        return self.recent[key]

    def clear(self):
        self.recent.clear()
        self.references.clear()
        if self.directory.resolve().parent != self.root:
            raise ValueError("Invalid PPT scratch directory")
        for path in self.directory.glob("*.npz"):
            path.unlink()

    def close(self):
        self.recent.clear()
        self.references.clear()
        self.temporary.cleanup()


class MaskedFeature(Mapping):
    def __init__(self, original, band_start, rows, cache):
        self.original, self.band_start, self.rows = original, band_start, rows
        self.cache = cache

    def __getitem__(self, name):
        key = id(self)
        if key not in self.cache:
            feature = {name: value.copy() for name, value in self.original.items()}
            for value in feature.values():
                value[self.band_start:][self.rows] = 0
            self.cache[key] = feature
            while len(self.cache) > 16:
                self.cache.popitem(last=False)
        self.cache.move_to_end(key)
        return self.cache[key][name]

    def __iter__(self):
        return iter(self.original)

    def __len__(self):
        return len(self.original)
