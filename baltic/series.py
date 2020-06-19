import json
import time

from numpy import arange, lexsort

from .changelog import Changelog
from .segment import Segment


def intersect(info, start, end):
    ok_start = not end or info["start"] <= end
    ok_end = not start or info["end"] >= start
    if not (ok_start and ok_end):
        return None
    # return reduced range
    return (max(info["start"], start), min(info["end"], end))


class Series:
    """
    Combine a zarr group and a changelog to provide a versioned and
    concurrent management of timeseries.
    """

    def __init__(self, schema, pod):
        self.schema = schema
        self.pod = pod
        self.reset()

    def reset(self):
        self.chl_pod = self.pod / "changelog"
        self.changelog = Changelog(self.chl_pod)
        self.sgm_pod = self.pod / "segment"

    def read(self, start=[], end=[], limit=None):
        """
        Read all matching segment and combine them
        """
        start = self.schema.deserialize(start)
        end = self.schema.deserialize(end)

        # Collect all rev info
        series_info = []
        for content in self.changelog.read():
            info = json.loads(content)
            info["start"] = self.schema.deserialize(info["start"])
            info["end"] = self.schema.deserialize(info["end"])
            if intersect(info, start, end):
                series_info.append(info)
        # Order revision backward
        series_info = list(reversed(series_info))
        # Recursive discovery of matching segments
        segments = self._read(series_info, start, end, limit=limit)

        if not segments:
            return Segment(self.schema)
        return Segment.concat(self.schema, *segments)

    def _read(self, series_info, start, end, limit=None):
        segments = []
        for pos, info in enumerate(series_info):
            match = intersect(info, start, end)
            if not match:
                continue

            # instanciate segment
            sgm = Segment.from_pod(self.schema, self.sgm_pod, info["columns"])
            segments.append(sgm.slice(*match, closed="both"))

            mstart, mend = match
            # recurse left
            if mstart > start:
                left_sgm = self._read(
                    series_info[pos + 1 :], start, mstart, limit=limit
                )
                segments = left_sgm + segments

            # recurse right
            if mend < end:
                if limit is not None:
                    limit = limit - len(sgm)
                    if limit < 1:
                        break
                right_sgm = self._read(series_info[pos + 1 :], mend, end, limit=limit)
                segments = segments + right_sgm

            break
        return segments

    def write(self, sgm, start=None, end=None):
        # Make sure segment is sorted
        sort_mask = lexsort([sgm[n] for n in sgm.schema.idx])
        assert (sort_mask == arange(len(sgm))).all()

        col_digests = sgm.save(self.sgm_pod)
        idx_start = start or sgm.start()
        idx_end = end or sgm.end()

        info = {
            "start": self.schema.serialize(idx_start),
            "end": self.schema.serialize(idx_end),
            "size": sgm.size(),  # needed to implement squashing strategies
            "timestamp": time.time(),
            "columns": col_digests,
        }
        content = json.dumps(info)
        self.changelog.commit([content])

    def truncate(self):
        self.chl_pod.clear()
        self.sgm_pod.clear()
        self.reset()

    def squash(self):
        """
        Remove all the revisions, collapse all segments into one
        """

        # FIXME: it would make more sense to create a snapshot and
        # keep historical content in an archive group. (and have
        # another command that remove archives)
        sgm = self.read()
        self.truncate()
        self.write(sgm)
