import numpy as np
from typing import Union, Optional, Any

class NumpySeries:
    """
    A fixed‑length ring buffer backed by a NumPy array, mimicking `collections.deque`
    with full slicing support and O(1) appends/pops from both ends.

    Example:
        >>> s = NumpySeries(maxlen=5)
        >>> for i in range(1, 6): s.append(i)
        >>> s.appendleft(0)          # now [0,1,2,3,4] (5 drops 5)
        >>> s.pop()                  # 4
        >>> s.popleft()              # 0
        >>> s[-2:]                   # last two: array([2., 3.])
    """

    def __init__(self, maxlen: int, dtype: type = float):
        if maxlen <= 0:
            raise ValueError("maxlen must be positive")
        self._maxlen = maxlen
        self._dtype = dtype
        self._data = np.full(maxlen, np.nan if dtype == float else 0, dtype=dtype)
        self._start = 0          # index of the oldest element
        self._count = 0          # number of valid elements (0..maxlen)

    @property
    def maxlen(self) -> int:
        return self._maxlen

    # ---- Deque-like methods ----

    def append(self, value: Any) -> None:
        """Add value to the right (newest). If full, drop the leftmost."""
        if self._count == self._maxlen:
            # full: overwrite the oldest (start) and advance start
            self._data[self._start] = value
            self._start = (self._start + 1) % self._maxlen
        else:
            pos = (self._start + self._count) % self._maxlen
            self._data[pos] = value
            self._count += 1

    def appendleft(self, value: Any) -> None:
        """Add value to the left (oldest). If full, drop the rightmost."""
        if self._count == self._maxlen:
            # full: overwrite the element just before start (newest)
            new_start = (self._start - 1) % self._maxlen
            self._data[new_start] = value
            self._start = new_start
        else:
            self._start = (self._start - 1) % self._maxlen
            self._data[self._start] = value
            self._count += 1

    def pop(self) -> Any:
        """Remove and return the rightmost (newest) element. Raises IndexError if empty."""
        if self._count == 0:
            raise IndexError("pop from empty NumpySeries")
        pos = (self._start + self._count - 1) % self._maxlen
        value = self._data[pos]
        self._count -= 1
        # optional: clear the slot (set to NaN/0) – not required but cleaner
        # self._data[pos] = (np.nan if self._dtype == float else 0)
        return value

    def popleft(self) -> Any:
        """Remove and return the leftmost (oldest) element. Raises IndexError if empty."""
        if self._count == 0:
            raise IndexError("popleft from empty NumpySeries")
        value = self._data[self._start]
        self._start = (self._start + 1) % self._maxlen
        self._count -= 1
        return value

    def clear(self) -> None:
        """Remove all elements."""
        self._start = 0
        self._count = 0
        # Reset data to NaN/0 (optional, for cleanliness)
        self._data.fill(np.nan if self._dtype == float else 0)

    # ---- Indexing and slicing ----

    def __len__(self) -> int:
        return self._count

    def __getitem__(self, key: Union[int, slice]) -> Union[float, np.ndarray]:
        if isinstance(key, slice):
            indices = self._slice_to_indices(key)
            if indices is None:
                raise IndexError("Invalid slice")
            return self._data[indices]
        elif isinstance(key, int):
            # negative indexing
            if key < 0:
                key = self._count + key
            if key < 0 or key >= self._count:
                raise IndexError("Index out of range")
            phys_idx = (self._start + key) % self._maxlen
            return self._data[phys_idx]
        else:
            raise TypeError(f"Invalid index type: {type(key)}")

    def _slice_to_indices(self, s: slice) -> Optional[np.ndarray]:
        """Convert a slice to an array of physical indices."""
        start, stop, step = s.indices(self._count)
        if step == 0:
            return None
        logical_indices = list(range(start, stop, step))
        if not logical_indices:
            return np.array([], dtype=int)
        phys_indices = [(self._start + i) % self._maxlen for i in logical_indices]
        return np.array(phys_indices, dtype=int)

    def __iter__(self):
        for i in range(self._count):
            yield self[i]

    def __repr__(self) -> str:
        return f"NumpySeries(maxlen={self._maxlen}, data={list(self)})"

    def to_list(self) -> list:
        return list(self)

    def to_array(self) -> np.ndarray:
        return np.array(list(self), dtype=self._dtype)

    def __array__(self, dtype=None):
        return self.to_array() if dtype is None else self.to_array().astype(dtype)

    def __bool__(self) -> bool:
        return self._count > 0