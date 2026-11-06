class BoundedDict(dict):
    """Dict with a size cap that drops the least recently used key (reads count as use)."""

    def __init__(self, maxlen: int):
        super().__init__()
        self.maxlen = maxlen

    def _touch(self, key):
        super().__setitem__(key, super().pop(key))   # re-insert at the end

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self._touch(key)
        return value

    def get(self, key, default=None):
        if key in self:
            return self[key]
        return default

    def __setitem__(self, key, value):
        if key in self:
            super().pop(key)
        elif len(self) >= self.maxlen:
            self.pop(next(iter(self)))
        super().__setitem__(key, value)
