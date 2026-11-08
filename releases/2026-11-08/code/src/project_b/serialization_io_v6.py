"""Buffer-compatible quota writer; preserves the existing pickle protocol."""
import pickle


class QuotaWriter:
    def __init__(self,handle,guard):
        self.handle=handle
        self.guard=guard

    def write(self,data):
        view=data.raw() if isinstance(data,pickle.PickleBuffer) else memoryview(data)
        if self.guard:
            self.guard.check_write(view.nbytes)
        return self.handle.write(view)
