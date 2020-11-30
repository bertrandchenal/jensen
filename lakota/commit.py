from numcodecs import registry
from numpy import asarray, concatenate, where

from .frame import Frame
from .schema import Codec, Schema
from .utils import hashed_path


class Commit:

    digest_codec = Codec("U")  # FIXME use better encoding
    len_codec = Codec("int")
    label_codec = Codec("str")
    closed_codec = Codec("str")  # Could be i1

    def __init__(self, schema, label, start, stop, digest, length, closed):
        assert list(digest) == list(schema)
        self.schema = schema
        self.label = label  # Array of str
        self.start = start  # Dict of Arrays
        self.stop = stop  # Dict of arrays
        self.digest = digest  # Dict of arrays
        self.length = length  # Array of int
        self.closed = closed  # Array of ("l", "r", "b", None)

    @classmethod
    def one(cls, schema, label, start, stop, digest, length, closed="both"):
        label = [label]
        start = dict(zip(schema.idx, ([s] for s in start)))
        stop = dict(zip(schema.idx, ([s] for s in stop)))
        digest = dict(zip(schema, (asarray([d], dtype="U") for d in digest)))
        length = [length]
        closed = [closed]
        return Commit(schema, label, start, stop, digest, length, closed)

    @classmethod
    def decode(cls, schema, payload):
        msgpck = registry.codec_registry["msgpack2"]()
        data = msgpck.decode(payload)[0]
        values = {}
        # Decode starts, stops and digests
        for key in ("start", "stop", "digest"):
            key_vals = {}
            columns = schema if key == "digest" else schema.idx
            for name in columns:
                codec = cls.digest_codec if key == "digest" else schema[name].codec
                key_vals[name] = codec.decode(data[key][name])
            values[key] = key_vals

        # Decode len and labels
        values["length"] = cls.len_codec.decode(data["length"])
        values["label"] = cls.label_codec.decode(data["label"])
        values["closed"] = cls.closed_codec.decode(data["closed"])
        return Commit(schema, **values)

    def encode(self):
        msgpck = registry.codec_registry["msgpack2"]()
        data = {}
        # Encode starts, stops and digests
        for key in ("start", "stop", "digest"):
            columns = self.schema if key == "digest" else self.schema.idx
            key_vals = {}
            for pos, name in enumerate(columns):
                codec = (
                    self.digest_codec if key == "digest" else self.schema[name].codec
                )
                arr = getattr(self, key)[name]
                key_vals[name] = codec.encode(arr)
            data[key] = key_vals

        # Encode digests
        for name in self.schema:
            data["digest"][name] = self.digest_codec.encode(self.digest[name])

        # Encode length, closed and labels
        data["length"] = self.len_codec.encode(self.length)
        data["closed"] = self.closed_codec.encode(self.closed)
        data["label"] = self.label_codec.encode(self.label)
        return msgpck.encode([data])

    def split(self, label, start, stop):
        start_values = {"label": self.label}
        start_values.update(self.start)
        stop_values = {"label": self.label}
        stop_values.update(self.stop)
        frm_start = Frame(Schema.from_frame(start_values), start_values)
        frm_stop = Frame(Schema.from_frame(stop_values), stop_values)
        start_pos = frm_stop.index((label,) + start, right=False)
        stop_pos = frm_start.index((label,) + stop, right=True)
        return start_pos, stop_pos

    def __len__(self):
        return len(self.label)

    def at(self, pos):
        if pos < 0:
            pos = len(self) + pos
        res = {}
        for key in ("start", "stop", "digest"):
            columns = self.schema if key == "digest" else self.schema.idx
            values = getattr(self, key)
            res[key] = tuple(values[n][pos] for n in columns)

        for key in ("label", "length", "closed"):
            res[key] = getattr(self, key)[pos]
        return res

    def update(self, label, start, stop, digest, length, closed="both"):
        if not start <= stop:
            raise ValueError(f"Invalid range {start} -> {stop}")
        inner = Commit.one(self.schema, label, start, stop, digest, length, closed)
        if len(self) == 0:
            return inner

        first = (self.at(0)["label"], self.at(0)["start"])
        last = (self.at(-1)["label"], self.at(-1)["stop"])
        if (label, start) <= first and (label, stop) >= last:
            return inner

        start_pos, stop_pos = self.split(label, start, stop)
        # Truncate start_pos row
        head = None
        if start_pos < len(self):
            start_row = self.at(start_pos)
            if start <= start_row["stop"] <= stop:
                # FIXME check start_row['label'] !
                start_row["stop"] = start
                # XXX adapt behaviour if current update is not closed==both
                start_row["closed"] = (
                    "left" if start_row["closed"] in ("left", "both") else None
                )
                if start_row["start"] < start_row["stop"]:
                    head = Commit.concat(
                        self.head(start_pos),
                        Commit.one(schema=self.schema, **start_row),
                    )
                # when start_row["start"] == start_row["stop"],
                # start_row stop and start are both "overshadowed" by
                # new commit

        if head is None:
            head = self.head(start_pos)

        # Truncate stop_pos row
        stop_pos = stop_pos - 1  # -1 because we did a bisect right in split
        tail = None
        if stop_pos < len(self):
            stop_row = self.at(stop_pos)
            if start <= stop_row["start"] <= stop:
                stop_row["start"] = stop
                # XXX adapt behavoour if current update is not closed==both
                stop_row["closed"] = (
                    "right" if stop_row["closed"] in ("right", "both") else None
                )
                if stop_row["start"] < stop_row["stop"]:
                    tail = Commit.concat(
                        self.tail(stop_pos + 1),
                        Commit.one(schema=self.schema, **stop_row),
                    )
                # when stop_row["start"] == stop_row["stop"],
                # stop_row stop and start are both "overshadowed" by
                # new commit
        if tail is None:
            tail = self.tail(stop_pos + 1)

        return Commit.concat(head, inner, tail)

    def slice(self, *pos):
        slc = slice(*pos)
        schema = self.schema
        start = {name: self.start[name][slc] for name in schema.idx}
        stop = {name: self.stop[name][slc] for name in schema.idx}
        digest = {name: self.digest[name][slc] for name in schema}
        label = self.label[slc]
        length = self.length[slc]
        closed = self.closed[slc]
        return Commit(schema, label, start, stop, digest, length, closed)

    def head(self, pos):
        return self.slice(None, pos)

    def tail(self, pos):
        return self.slice(pos, None)

    @classmethod
    def concat(cls, commit, *other_commits):
        schema = commit.schema
        all_ci = (commit,) + other_commits
        start = {
            name: concatenate([ci.start[name] for ci in all_ci]) for name in schema.idx
        }
        stop = {
            name: concatenate([ci.stop[name] for ci in all_ci]) for name in schema.idx
        }
        digest = {
            name: concatenate([ci.digest[name] for ci in all_ci]) for name in schema
        }
        label = concatenate([ci.label for ci in all_ci])
        length = concatenate([ci.length for ci in all_ci])
        closed = concatenate([ci.closed for ci in all_ci])

        return Commit(schema, label, start, stop, digest, length, closed)

    def __repr__(self):
        start = ", ".join(map(str, self.start.values()))
        stop = ", ".join(map(str, self.stop.values()))
        return f"<Commit ({start}) -> {stop})>"

    def segments(self, label, pod, start, stop):
        res = []
        (matches,) = where(self.label == label)
        for pos in matches:
            arr_start = tuple(arr[pos] for arr in self.start.values())
            arr_stop = tuple(arr[pos] for arr in self.stop.values())
            digest = [arr[pos] for arr in self.digest.values()]
            closed = self.closed[pos]

            # length = self.length[pos]
            # sgm = ShallowSegment(
            #     self.schema,
            #     pod,
            #     digest,
            #     start=arr_start,
            #     stop=arr_stop,
            #     length=length,
            # ).slice(start or arr_start, stop or arr_stop, closed)

            sgm = Segment(
                self.schema,
                pod,
                digest,
                start=arr_start if start is None else max(arr_start, start),
                stop=arr_stop if stop is None else min(arr_stop, stop),
                closed=closed,
            )

            res.append(sgm)
        return res


class Segment:
    def __init__(self, schema, pod, digests, start, stop, closed):
        self.schema = schema
        self.pod = pod
        self.start = start
        self.stop = stop
        self.closed = closed
        # self.length = length
        self.digest = dict(zip(schema, digests))
        self._frm = None

    def __len__(self):
        return len(self.frame)

    def read(self, name, start=None, stop=None):
        arr = self.frame[name][start:stop]
        return arr

    @property
    def frame(self):
        # TODO put only schema.idx in frame (and try to minimize reads)
        if self._frm is not None:
            return self._frm

        cols = {}
        for name in self.schema:
            folder, filename = hashed_path(self.digest[name])
            arr = self.pod.cd(folder).read(filename)
            cols[name] = self.schema[name].codec.decode(arr)
        frm = Frame(self.schema, cols).index_slice(
            self.start, self.stop, closed=self.closed
        )
        self._frm = frm
        return frm