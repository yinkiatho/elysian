import numpy as np
from typing import Union, Optional, Any


class NumpySeries:
    """
    A fixed-length ring buffer backed by a NumPy array, mimicking `collections.deque`
    with full slicing support and O(1) appends/pops from both ends.

    Example:
        >>> s = NumpySeries(maxlen=5)
        >>> for i in range(1, 6): s.append(i)
        >>> s.appendleft(0)          # now [0,1,2,3,4] (5 drops out)
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
        self._start = 0
        self._count = 0

    @property
    def maxlen(self) -> int:
        return self._maxlen

    # ---- Deque-like methods ----

    def append(self, value: Any) -> None:
        """Add value to the right (newest). If full, drop the leftmost."""
        if self._count == self._maxlen:
            self._data[self._start] = value
            self._start = (self._start + 1) % self._maxlen
        else:
            pos = (self._start + self._count) % self._maxlen
            self._data[pos] = value
            self._count += 1

    def appendleft(self, value: Any) -> None:
        """Add value to the left (oldest). If full, drop the rightmost."""
        if self._count == self._maxlen:
            # Move start back one to claim the tail slot, overwriting the newest
            self._start = (self._start - 1) % self._maxlen
            self._data[self._start] = value
            # _count stays the same — we replaced one element
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
        self._data.fill(np.nan if self._dtype == float else 0)

    # ---- Indexing and slicing ----

    def __len__(self) -> int:
        return self._count

    def __getitem__(self, key: Union[int, slice]) -> Union[float, np.ndarray]:
        if isinstance(key, slice):
            indices = self._slice_to_indices(key)
            # FIX: empty slice should return empty array with correct dtype,
            # not raise; invalid step=0 should be ValueError not IndexError
            if indices is None:
                raise ValueError("slice step cannot be zero")
            if len(indices) == 0:
                return np.array([], dtype=self._dtype)
            return self._data[indices]
        elif isinstance(key, int):
            if key < 0:
                key = self._count + key
            if key < 0 or key >= self._count:
                raise IndexError("index out of range")
            phys_idx = (self._start + key) % self._maxlen
            return self._data[phys_idx]
        else:
            raise TypeError(f"invalid index type: {type(key)}")

    def _slice_to_indices(self, s: slice) -> Optional[np.ndarray]:
        """Convert a slice to an array of physical indices in logical order."""
        # s.indices() handles None start/stop and negative values correctly
        start, stop, step = s.indices(self._count)
        if step == 0:
            return None  # signal ValueError to caller
        logical_indices = range(start, stop, step)
        if not logical_indices:
            return np.array([], dtype=int)
        phys_indices = [(self._start + i) % self._maxlen for i in logical_indices]
        return np.array(phys_indices, dtype=int)

    def __setitem__(self, key: int, value: Any) -> None:
        if isinstance(key, int):
            if key < 0:
                key = self._count + key
            if key < 0 or key >= self._count:
                raise IndexError("index out of range")
            phys_idx = (self._start + key) % self._maxlen
            self._data[phys_idx] = value
        else:
            raise TypeError("NumpySeries only supports integer assignment")

    def __iter__(self):
        for i in range(self._count):
            yield self[i]

    def __repr__(self) -> str:
        return f"NumpySeries(maxlen={self._maxlen}, data={list(self)})"

    def to_list(self) -> list:
        return list(self)

    def to_array(self) -> np.ndarray:
        # FIX: use _slice_to_indices for a zero-copy-friendly path when unwrapped,
        # fall back to list conversion when wrapped
        if self._count == 0:
            return np.array([], dtype=self._dtype)
        end = (self._start + self._count) % self._maxlen
        if self._start < end:
            # contiguous — slice directly
            return self._data[self._start:end].copy()
        else:
            # wrapped — two segments
            return np.concatenate([
                self._data[self._start:],
                self._data[:end]
            ])

    def __array__(self, dtype=None):
        arr = self.to_array()
        return arr if dtype is None else arr.astype(dtype)

    def __bool__(self) -> bool:
        return self._count > 0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_basic_append_and_index():
    s = NumpySeries(maxlen=5)
    for i in range(1, 6):
        s.append(i)
    _assert(list(s) == [1, 2, 3, 4, 5], "basic append")
    _assert(s[0] == 1, "index 0")
    _assert(s[-1] == 5, "index -1")
    _assert(s[-2] == 4, "index -2")
    print("PASS test_basic_append_and_index")


def test_append_overflow():
    s = NumpySeries(maxlen=3)
    for i in range(1, 7):
        s.append(i)
    _assert(list(s) == [4, 5, 6], f"overflow append: got {list(s)}")
    print("PASS test_append_overflow")


def test_appendleft_not_full():
    s = NumpySeries(maxlen=5)
    s.append(2); s.append(3)
    s.appendleft(1)
    _assert(list(s) == [1, 2, 3], f"appendleft not full: got {list(s)}")
    print("PASS test_appendleft_not_full")


def test_appendleft_full_drops_newest():
    s = NumpySeries(maxlen=3)
    s.append(1); s.append(2); s.append(3)   # [1, 2, 3]
    s.appendleft(0)                           # should give [0, 1, 2], dropping 3
    _assert(list(s) == [0, 1, 2], f"appendleft full: got {list(s)}")
    print("PASS test_appendleft_full_drops_newest")


def test_appendleft_full_docstring_example():
    s = NumpySeries(maxlen=5)
    for i in range(1, 6):
        s.append(i)                   # [1,2,3,4,5]
    s.appendleft(0)                   # [0,1,2,3,4]
    _assert(list(s) == [0, 1, 2, 3, 4], f"docstring appendleft: got {list(s)}")
    r = s.pop()
    _assert(r == 4, f"pop after appendleft: got {r}")
    l = s.popleft()
    _assert(l == 0, f"popleft after appendleft: got {l}")
    _assert(list(s) == [1, 2, 3], f"after pop+popleft: got {list(s)}")
    print("PASS test_appendleft_full_docstring_example")


def test_pop_and_popleft():
    s = NumpySeries(maxlen=5)
    for i in range(1, 6):
        s.append(i)
    _assert(s.pop() == 5, "pop newest")
    _assert(s.popleft() == 1, "popleft oldest")
    _assert(list(s) == [2, 3, 4], f"after pop+popleft: {list(s)}")
    print("PASS test_pop_and_popleft")


def test_slicing():
    s = NumpySeries(maxlen=5)
    for i in range(1, 6):
        s.append(i)
    np.testing.assert_array_equal(s[-2:], [4, 5])
    np.testing.assert_array_equal(s[1:4], [2, 3, 4])
    np.testing.assert_array_equal(s[::2], [1, 3, 5])
    np.testing.assert_array_equal(s[::-1], [5, 4, 3, 2, 1])
    _assert(len(s[0:0]) == 0, "empty slice has length 0")
    _assert(s[0:0].dtype == np.float64, "empty slice dtype")
    print("PASS test_slicing")


def test_slicing_wrapped_buffer():
    # Force the buffer to wrap: fill, then append more to shift _start
    s = NumpySeries(maxlen=4)
    for i in range(1, 7):
        s.append(i)              # logical: [3,4,5,6], _start != 0
    np.testing.assert_array_equal(s[:], [3, 4, 5, 6])
    np.testing.assert_array_equal(s[-2:], [5, 6])
    np.testing.assert_array_equal(s[1:3], [4, 5])
    print("PASS test_slicing_wrapped_buffer")


def test_to_array_wrapped():
    s = NumpySeries(maxlen=4)
    for i in range(1, 7):
        s.append(i)
    np.testing.assert_array_equal(s.to_array(), [3, 4, 5, 6])
    print("PASS test_to_array_wrapped")


def test_empty_errors():
    s = NumpySeries(maxlen=3)
    try:
        s.pop()
        _assert(False, "should raise")
    except IndexError:
        pass
    try:
        s.popleft()
        _assert(False, "should raise")
    except IndexError:
        pass
    try:
        _ = s[0]
        _assert(False, "should raise")
    except IndexError:
        pass
    print("PASS test_empty_errors")


def test_clear():
    s = NumpySeries(maxlen=3)
    s.append(1); s.append(2)
    s.clear()
    _assert(len(s) == 0, "clear len")
    _assert(not s, "clear bool")
    s.append(10)
    _assert(list(s) == [10], f"append after clear: {list(s)}")
    print("PASS test_clear")


def test_int_dtype():
    s = NumpySeries(maxlen=4, dtype=int)
    s.append(1); s.append(2); s.append(3)
    _assert(s[0] == 1, "int dtype index")
    np.testing.assert_array_equal(s[:], [1, 2, 3])
    print("PASS test_int_dtype")


if __name__ == "__main__":
    test_basic_append_and_index()
    test_append_overflow()
    test_appendleft_not_full()
    test_appendleft_full_drops_newest()
    test_appendleft_full_docstring_example()
    test_pop_and_popleft()
    test_slicing()
    test_slicing_wrapped_buffer()
    test_to_array_wrapped()
    test_empty_errors()
    test_clear()
    test_int_dtype()
    print("\nAll tests passed.")