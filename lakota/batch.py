from .changelog import phi
from .commit import Commit


class Batch:
    def __init__(self, collection, root=False):
        self.collection = collection
        self._ci_info = []
        self.revs = []
        self.root = root

    def append(self, label, start, stop, all_dig, frame_len, embedded):
        self._ci_info.append((label, start, stop, all_dig, frame_len, embedded))

    def extend(self, *other_batches):
        for b in other_batches:
            self._ci_info.extend(b._ci_info)

    def flush(self):
        if len(self._ci_info) == 0:
            return

        changelog = self.collection.changelog
        leaf_rev = None if self.root else changelog.leaf()
        all_ci_info = iter(self._ci_info)

        # Combine with last commit
        if leaf_rev:
            last_ci = leaf_rev.commit(self.collection)
        else:
            label, start, stop, all_dig, length, embedded = next(all_ci_info)
            last_ci = Commit.one(
                self.collection.schema,
                label,
                start,
                stop,
                all_dig,
                length,
                embedded=embedded,
            )
        for label, start, stop, all_dig, length, embedded in all_ci_info:
            last_ci = last_ci.update(
                label, start, stop, all_dig, length, embedded=embedded
            )

        # Save it
        payload = last_ci.encode()
        parent = leaf_rev.child if leaf_rev else phi
        self.revs = self.collection.changelog.commit(payload, parents=[parent])
