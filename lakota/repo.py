"""
The `Repo` class manage the organisation of a storage location. It
provides creation and deletion of collections, synchronization with
remote repositories and garbage collection.


## Create repositories

Create a `Repo` instance:
```python
# in-memory
repo = Repo()
repo = Repo("memory://")
# From a local directory
repo = Repo('some/local/path')
repo = Repo('file://some/local/path')
# From an S3 location
repo = Repo('s3:///my_bucket')
# Use a list of uri to enable caching
repo = Repo(['memory://', 's3:///my_bucket'])
repo = Repo(['file:///tmp/local_cache', 's3:///my_bucket'])
```

S3 authentication is handled by
[s3fs](https://s3fs.readthedocs.io/en/latest/#credentials "s3fs
credentials"). So you can either put your credentials in a
configuration files or in environment variables. If it's not possible,
you can still pass them as arguments:

```python
pod = POD.from_uri('s3:///bucket_name', key=key, secret=secret, token=token)
repo = Repo(pod=pod)
```

Similarly, you can use a compatible service through the `endpoint_url` parameter:

```python
pod = POD.from_uri('s3:///bucket_name', endpoint_url='http://127.0.0.1:5300')
repo = Repo(pod=pod)
```

## Access collections

Create one or several collections:
```python
# Define schema
schema = Schema(timestamp='int*', value='float')
# Create one collection
repo.create_collection(schema, 'my_collection')
# Create a few more
labels = ['one', 'or_more', 'labels']
repo.create_collection(schema, *labels)
```

List and instanciate collections
```python
print(list(repo.ls())) # Print collections names
# Instanciate a collection
clct = repo.collection('my_collection')
# like pathlib, the `/` operator can be used
clct = repo / 'my_collection'
```

See `lakota.collection` on how to manipulate collections


## Garbage Collection

After some times, some series can be overwritten, deleted, squashed or
merged. Sooner or later some pieces of data will get dereferenced,
those can be deleted to recover storage space. It is simply done with
the `gc` method, which returns the number of deleted files.

```python
nb_file_deleted = repo.gc()
```
"""

import csv
import json
from io import BytesIO, StringIO
from itertools import chain
from time import time

from .changelog import zero_hash
from .collection import Collection
from .pod import POD
from .schema import Schema
from .utils import Pool, hashed_path, hexdigest, hextime, logger, settings

__all__ = ["Repo"]


class Repo:
    schema = Schema.kv(label="str*", meta="O")

    def __init__(self, uri=None, pod=None):
        """
        `uri` : a string or a list of string representing a storage
        location

        `pod`
        : a `lakota.pod.POD` instance
        """
        pod = pod or POD.from_uri(uri)
        folder, filename = hashed_path(zero_hash)
        self.pod = pod
        path = folder / filename
        self.registry = Collection("registry", self.schema, path, self)

    def ls(self):
        return [item.label for item in self.search()]

    def __iter__(self):
        return self.search()

    def search(self, label=None, namespace="collection"):
        if label:
            start = stop = (label,)
        else:
            start = stop = None
        series = self.registry.series(namespace)
        qr = series[start:stop] @ {"closed": "BOTH"}
        frm = qr.frame()
        for l in frm["label"]:
            yield self.collection(l, frm)

    def __truediv__(self, name):
        return self.collection(name)

    def collection(self, label, from_frm=None, namespace="collection"):
        series = self.registry.series(namespace)
        if from_frm:
            frm = from_frm.slice(*from_frm.index_slice([label], [label], closed="BOTH"))
        else:
            frm = series.frame(start=label, stop=label, closed="BOTH")

        if frm.empty:
            return None
        meta = frm["meta"][-1]
        return self.reify(label, meta)

    def create_collection(
        self, schema, *labels, raise_if_exists=True, namespace="collection"
    ):
        """
        `schema`
        : A `lakota.schema.Schema` instance

        `labels`
        : One or more collection name

        `raise_if_exists`
        : Raise an exception if the label is already present
        """
        assert isinstance(
            schema, Schema
        ), "The schema parameter must be an instance of lakota.Schema"
        meta = []
        schema_dump = schema.dumps()

        series = self.registry.series(namespace)
        current_labels = series.frame(
            start=min(labels), stop=max(labels), closed="BOTH", select="label"
        )["label"]

        for label in labels:
            label = label.strip()
            if len(label) == 0:
                raise ValueError(f"Invalid label: {label}")
            if label in current_labels and raise_if_exists:
                raise ValueError(f"Collection with label '{label}' already exists")

            key = label.encode()
            # Use digest to create collection folder (based on mode and label)
            digest = hexdigest(key)
            if namespace != "collection":
                digest = hexdigest(digest.encode(), namespace.encode())
            folder, filename = hashed_path(digest)
            meta.append({"path": str(folder / filename), "schema": schema_dump})

        series.write({"label": labels, "meta": meta})
        res = [self.reify(l, m) for l, m in zip(labels, meta)]
        if len(labels) == 1:
            return res[0]
        return res

    def reify(self, name, meta):
        schema = Schema.loads(meta["schema"])
        return Collection(name, schema, meta["path"], self)

    def archive(self, collection):
        label = collection.label
        archive = self.collection(label, mode="archive")
        if archive:
            return archive
        return self.create_collection(collection.schema, label, mode="archive")

    def delete(self, *labels, namespace="collection"):
        """
        Delete one or more collections

        `*labels`
        : Strings, names of the collection do delete

        """
        to_remove = []
        for l in labels:
            clct = self.collection(l)
            if not clct:
                continue
            to_remove.append(clct.changelog.pod)
        series = self.registry.series(namespace)
        series.delete(*labels)
        for pod in to_remove:
            try:
                pod.rm(".", recursive=True)
            except FileNotFoundError:
                continue

    def refresh(self):
        self.registry.refresh()

    def push(self, remote, *labels, shallow=False):
        """
        Push local revisions (and related segments) to `remote` Repo.
        `remote`
        : A `lakota.repo.Repo` instance

        `labels`
        : The collections to push. If not given, all collections are pushed
        """
        return remote.pull(self, *labels, shallow=shallow)

    def pull(self, remote, *labels, shallow=False):
        """
        Pull revisions from `remote` Repo (and related segments).
        `remote`
        : A `lakota.repo.Repo` instance

        `labels`
        : The collections to pull. If not given, all collections are pulled
        """

        assert isinstance(remote, Repo), "A Repo instance is required"
        # Pull registry
        self.registry.pull(remote.registry, shallow=shallow)
        # Extract frames
        local_cache = {l.label: l for l in self.search()}
        remote_cache = {r.label: r for r in remote.search()}
        if not labels:
            labels = remote_cache.keys()
        for label in labels:
            logger.info("Sync collection: %s", label)
            r_clct = remote_cache[label]
            if not label in local_cache:
                l_clct = self.create_collection(r_clct.schema, label)
            else:
                l_clct = local_cache[label]
                if l_clct.schema != r_clct.schema:
                    msg = (
                        f'Unable to sync collection "{label}",'
                        "incompatible meta-info."
                    )
                    raise ValueError(msg)
            l_clct.pull(r_clct, shallow=shallow)

    def merge(self):
        """
        Merge repository registry. Needed when collections have been created
        or deleted concurrently.
        """
        return self.registry.merge()

    def rename(self, from_label, to_label, namespace="collection"):
        """
        Change the label a collection
        """
        series = self.registry.series(namespace)
        frm = series.frame()
        if to_label in frm["label"]:
            raise ValueError(f'Collection "{to_label}" already exists')

        # replace in label column
        start, stop = frm.start(), frm.stop()
        labels = frm["label"]
        mask = labels == from_label
        labels[mask] = to_label
        frm["label"] = labels

        # Re-order frame
        frm = frm.sorted()
        series.write(
            frm,
            start=min(
                frm.start(), start
            ),  # Make sure we over-write the previous content
            stop=max(frm.stop(), stop),  # same
        )

    def gc(self):
        """
        Loop on all series, collect all used digests, and delete obsolete
        ones.
        """
        logger.info("Start GC")
        # Collect digests across folders
        base_folders = self.pod.ls()
        with Pool() as pool:
            for folder in base_folders:
                pool.submit(self._walk_folder, folder)
        all_dig = set(chain(*pool.results))

        # Collect digest from changelogs. Because commits are written
        # after the segments, we minimize chance to bury data created
        # concurrently.
        self.refresh()
        active_digests = set(self.registry.digests())
        for namespace in self.registry.ls():
            for clct in self.search(namespace=namespace):
                active_digests.update(clct.digests())

        nb_hard_del = 0
        nb_soft_del = 0
        current_ts_ext = f".{hextime()}"
        deadline = hextime(time() - settings.timeout)

        # Soft-Delete ("bury") files on fs not in changelogs
        inactive = all_dig - active_digests
        for dig in inactive:
            if not "." in dig:
                # Disable digest
                folder, filename = hashed_path(dig)
                path = str(folder / filename)
                self.pod.mv(path, path + current_ts_ext, missing_ok=True)
                nb_soft_del += 1
                continue

            # Inactive file, check timestamp & delete or re-enable it
            dig, ext = dig.split(".")

            if ext > deadline:
                # ext contains a ts created recently, we can not act on it yet
                continue

            folder, filename = hashed_path(dig)
            path = str(folder / filename)

            if dig in active_digests:
                # Re-enable by removing extension
                self.pod.mv(path + f".{ext}", path, missing_ok=True)
            else:
                # Permanent deletion
                self.pod.rm(path + f".{ext}", missing_ok=True)
                nb_hard_del += 1

        logger.info(
            "End of GC (hard deletions: %s, soft deletions: %s)",
            nb_hard_del,
            nb_soft_del,
        )
        return nb_hard_del, nb_soft_del

    def _walk_folder(self, folder):
        digs = []
        pod = self.pod.cd(folder)
        for filename in pod.walk(max_depth=2):
            digs.append(folder + filename.replace("/", ""))

        return digs

    def import_collections(self, src, collections=None):
        """
        Import collections from given `src`. It can url accepted by
        `POD.from_uri` or a `POD` instance. `collections` is the list of
        collections to load (all collection are loaded if not set).
        """
        if not isinstance(src, POD):
            src = POD.from_uri(src)
        names = collections or src.ls()
        for clc_name in names:
            clc = self / clc_name
            pod = src.cd(clc_name)
            if clc is None:
                json_schema = pod.read("_schema.json").decode()
                schema = Schema.loads(json.loads(json_schema))
                clc = self.create_collection(schema, clc_name)
            logger.info('Import collection "%s"', clc_name)
            with clc.multi():
                for file_name in pod.ls():
                    if file_name.startswith("_"):
                        continue
                    self.import_series(pod, clc, file_name)

    def import_series(self, from_pod, collection, filename):
        stem, ext = filename.rsplit(".", 1)
        column_names = sorted(collection.schema)
        if ext == "csv":
            buff = StringIO(from_pod.read(filename).decode())
            reader = csv.reader(buff)
            headers = next(reader)
            assert sorted(headers) == column_names
            columns = zip(*reader)
            frm = dict(zip(headers, columns))
            srs = collection / stem
            srs.write(frm)

        elif ext == "parquet":
            from pandas import read_parquet

            buff = BytesIO(from_pod.read(filename))
            df = read_parquet(buff)
            assert sorted(df.columns) == column_names
            srs = collection / stem
            srs.write(df)
        else:
            raise ValueError(f"Unable to load {filename}, extension not supported")

    def export_collections(self, dest, collections=None, file_type="csv"):
        if not isinstance(dest, POD):
            dest = POD.from_uri(dest)

        names = collections or self.ls()
        for clc_name in names:
            clc = self / clc_name
            if clc is None:
                logger.warn('Collection "%s" not found', clc_name)
            pod = dest.cd(clc_name)
            logger.info('Export collection "%s"', clc_name)
            schema = clc.schema.dumps()
            pod.write("_schema.json", json.dumps(schema).encode())
            for srs in clc:
                # Read series
                self.export_series(pod, srs, file_type)

    def export_series(self, pod, series, file_type):
        if file_type == "csv":
            frm = series.frame()
            columns = list(frm)
            # Save series as csv in buff
            buff = StringIO()
            writer = csv.writer(buff)
            writer.writerow(columns)
            rows = zip(*(frm[c] for c in columns))
            writer.writerows(rows)
            # Write generated content in pod
            buff.seek(0)
            pod.write(f"{series.label}.csv", buff.read().encode())

        elif file_type == "parquet":
            df = series.df()
            data = df.to_parquet(compression="brotli")
            pod.write(f"{series.label}.parquet", data)
        else:
            exit(f'Unsupported file type "{file_type}"')
