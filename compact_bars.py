"""Lossless, read-only column storage for the live strategy's bounded history."""
from array import array
from collections.abc import Mapping, Sequence
import sys

_MISSING = object()

class _Row(Mapping):
    __slots__ = ('owner', 'index')
    def __init__(self, owner, index):
        self.owner, self.index = owner, index
    def __iter__(self):
        return (key for key, (_, mask) in self.owner.columns.items() if mask[self.index])
    def __len__(self):
        return sum(bool(mask[self.index]) for _, mask in self.owner.columns.values())
    def __getitem__(self, key):
        values, mask = self.owner.columns[key]
        kind = mask[self.index]
        if not kind:
            raise KeyError(key)
        if kind == 1:
            return None
        value = values[self.index]
        if kind == 3:
            return bool(value)
        if kind == 4:
            return int(value)
        return value

class CompactBars(Sequence):
    """Preserve missing/None/bool/int/float/string values; never round prices."""
    def __init__(self, rows, limit=None):
        rows = list(rows)
        if limit is not None:
            rows = rows[-limit:]
        self.length = len(rows)
        self.columns = {}
        for key in dict.fromkeys(k for row in rows for k in row):
            values = [row.get(key, _MISSING) for row in rows]
            present = [v for v in values if v is not _MISSING and v is not None]
            numeric = all(isinstance(v, (float, int)) and (not isinstance(v, int) or abs(v) <= 2**53) for v in present)
            data = array('d') if numeric else []
            mask = bytearray()
            for v in values:
                kind = 0 if v is _MISSING else 1 if v is None else 3 if isinstance(v, bool) else 4 if isinstance(v, int) and numeric else 2
                mask.append(kind)
                if kind in (0, 1):
                    data.append(0 if numeric else None)
                else:
                    data.append(sys.intern(v) if isinstance(v, str) else v)
            self.columns[sys.intern(key)] = (data, mask)
    def __len__(self):
        return self.length
    def __getitem__(self, index):
        if isinstance(index, slice):
            return [_Row(self, i) for i in range(*index.indices(self.length))]
        if index < 0:
            index += self.length
        if not 0 <= index < self.length:
            raise IndexError(index)
        return _Row(self, index)
    def __eq__(self, other):
        return list(self) == list(other)
