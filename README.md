

# Lakota

Inspired by Git, Lakota is a version-control system for numerical
series. It is meant to be used on top of S3, on the local filesystem
or in memory.

Documentation: https://bertrandchenal.github.io/lakota/

# Quickstart

The following script will create a timeseries on a repository backed
by a local folder and read it back.

``` python
from lakota import Repo, Schema

ts_schema = Schema(timestamp="timestamp*", value="float")
repo = Repo("my-data-folder")  # or Repo("s3:///my-s3-bucket")
clct = repo.create_collection(ts_schema, "temperature")
series = clct.series('Brussels')
df = {
    "timestamp": [
        "2020-01-01T00:00",
        "2020-01-02T00:00",
        "2020-01-03T00:00",
        "2020-01-04T00:00",
    ],
    "value": [1, 2, 3, 4],
}

series.write(df)
df = series[:'2020-01-03'].df()
print(df)
# ->
#    timestamp  value
# 0 2020-01-01    1.0
# 1 2020-01-02    2.0
# 2 2020-01-03    3.0
```

Let's try something more complex based on the WHO COVID dataset, we
first load some data in Lakota:

``` python
from lakota import Repo, Schema
from requests import get
from pandas import read_csv

# Instantiate a local repo and define a schema
repo = Repo('file:///db')
schema = Schema(**{
    'date_reported': 'timestamp*',
    'new_cases': 'int',
    'cumulative_cases': 'int',
    'new_deaths': 'int',
    'cumulative_deaths': 'int',
})

# Download csv from WHO's website
resp = get('https://covid19.who.int/WHO-COVID-19-global-data.csv', stream=True)
resp.raw.decode_content=True
df = read_csv(resp.raw)
rename = {c: c.strip().lower() for c in df.columns}
df = df.rename(columns=rename)

# Create one series per country
clct = repo.create_collection(schema, 'covid')
for key, sub_df in df.groupby(['who_region', 'country']):
    sub_df = sub_df.sort_values(by='date_reported')
    who_region, country = key
    series = clct / f'{who_region}_{country}'
    series.write(sub_df)
```

Then we can read some data back:

``` python
from lakota import Repo

repo = Repo('db')
series = repo / 'covid' / 'EURO_Belgium'

start = '2020-08-20T00:00:00'
stop = '2020-08-30T00:00:00'
df = series[start:stop].df()
print(df)
# ->
#   date_reported  new_cases  cumulative_cases  new_deaths  cumulative_deaths
# 0    2020-08-20        612             80868           9               9854
# 1    2020-08-21        581             81449           4               9858
# 2    2020-08-22        512             81961           3               9861
# 3    2020-08-23        207             82168           5               9866
# 4    2020-08-24        116             82284           3               9869
# 5    2020-08-25        610             82894           5               9874
# 6    2020-08-26        524             83418           5               9879
# 7    2020-08-27        521             83939           4               9883
# 8    2020-08-28        520             84459           3               9886
# 9    2020-08-29        445             84904           3               9889
```


# Caching

Caching can be enabled by instantiating a Repo object combining
several locations:

``` python
import os
from lakota import Repo, Schema
from lakota.utils import logger


# Instantiate a remote repo and populate it
remote_repo = Repo('/tmp/remote_repo')
schema = Schema(timestamp='timestamp*', value='float')
clct = remote_repo.create_collection(schema, "temperature")
timestamp = [
    "2020-01-01T00:00",
    "2020-01-02T00:00",
    "2020-01-03T00:00",
    "2020-01-04T00:00",
]

# Create a series
series = clct / 'Brussels'
series.write({
    'timestamp': timestamp,
    'value': range(4),
})


# Use memory as cache
cached_repo = Repo(['memory://', '/tmp/remote_repo'])
series = cached_repo / 'temperature' / 'Brussels'
print(series.df())
# ->
#    timestamp  value
# 0 2020-01-01    0.0
# 1 2020-01-02    1.0
# 2 2020-01-03    2.0
# 3 2020-01-04    3.0


# To illustrate caching behavior, we "destroy" the source repo
os.rename('/tmp/remote_repo', '/tmp/remote_repo_bis')
# Reading the data wont work, the cache still contains data, but rely
# on remote repo for listing it. So if a file is removed from remote
# it wont be read from local.
series = cached_repo / 'temperature' / 'Brussels'
print(series.df())
# ->
# Empty DataFrame
# Columns: [timestamp, value]
# Index: []

# Yet the data is there, we cheat by accessing the in-memory data on
# its own:
repo = Repo(pod=cached_repo.pod.local)
series = repo / 'temperature' / 'Brussels'
print(series.df())
# ->
#    timestamp  value
# 0 2020-01-01    0.0
# 1 2020-01-02    1.0
# 2 2020-01-03    2.0
# 3 2020-01-04    3.0

# If we "fix" the source repo, our cache works again. This time we
# activate logging to show that with a warm cache the remote repo is
# only used for listing the changelog of the collection:
os.rename('/tmp/remote_repo_bis', '/tmp/remote_repo')
series = cached_repo / 'temperature' / 'Brussels'
logger.setLevel('DEBUG')
print(series.df())
# ->
# DEBUG:2020-12-18 14:34:19: LIST /tmp/remote_repo/70/33/e90fcdda169d2f5d08da17507b0c5db52029 .
# DEBUG:2020-12-18 14:34:19: READ memory://70/33/e90fcdda169d2f5d08da17507b0c5db52029 00000000000-0000000000000000000000000000000000000000.176760ef1f5-efd4198272b4a671db506c360ce036e1d28c2efb
# DEBUG:2020-12-18 14:34:19: READ memory://e0/62 63beda1a1c2656d1089d3cb4d39d98eed342
# DEBUG:2020-12-18 14:34:19: READ memory://39/95 0a757393aea125b8018395c48fa5279e2753
#    timestamp  value
# 0 2020-01-01    0.0
# 1 2020-01-02    1.0
# 2 2020-01-03    2.0
# 3 2020-01-04    3.0
```

# Push & Pull

The following example demonstrate how to push/pull data between
repositories or between collection.

``` python
from lakota import Repo, Schema

# Instantiate a remote repo and populate it
remote_repo = Repo('/tmp/remote_repo')
schema = Schema(timestamp='timestamp*', value='float')
clct = remote_repo.create_collection(schema, "temperature")
timestamp = [
    "2020-01-01T00:00",
    "2020-01-02T00:00",
    "2020-01-03T00:00",
    "2020-01-04T00:00",
]
# Create a series
series = clct / 'Brussels'
series.write({
    'timestamp': timestamp,
    'value': range(4),
})

# Local repo is empty
local_repo = Repo('/tmp/local_repo')
print(local_repo.ls())
# -> []

# Pull everything from remote
local_repo.pull(remote_repo)
# List again
print(local_repo.ls())
# -> ['temperature']

# And read some values
series = local_repo / 'temperature' / 'Brussels'
print(series.df())
# ->
#    timestamp  value
# 0 2020-01-01    0.0
# 1 2020-01-02    1.0
# 2 2020-01-03    2.0
# 3 2020-01-04    3.0

# Create a new collection on local repo
clct = local_repo.create_collection(schema, "rainfall")
timestamp = [
    "2020-01-01T00:00",
    "2020-01-02T00:00",
    "2020-01-03T00:00",
    "2020-01-04T00:00",
]
# Create a series
series = clct / 'Brussels'
series.write({
    'timestamp': timestamp,
    'value': range(4),
})

# Push that collection only, under another name
remote_clct = remote_repo.create_collection(schema, 'precipitation')
clct.push(remote_clct)

print(remote_clct.series('Brussels').df())
#  ->
#    timestamp  value
# 0 2020-01-01    0.0
# 1 2020-01-02    1.0
# 2 2020-01-03    2.0
# 3 2020-01-04    3.0
```

# Merging

In case of concurrent writes, the changelog (that keeps track of the
history of modification of a given timeseries) may diverge. In this
case the collection has to be merged.

``` python
from pprint import pprint
from lakota import Repo, Schema


# Instanciate a two repos and populate them
repo = Repo('/tmp/repo')
repo_bis = Repo('/tmp/repo_bis')
schema = Schema('''
timestamp timestamp*
value float
''')
clct = repo.create_collection(schema, "temperature")

# First push (with an empty collection)
repo.push(repo_bis)
clct_bis = repo_bis / 'temperature'
pprint(clct_bis.ls())
# -> []

# Create a series and write original repo
timestamp = [
    "2020-01-01T00:00",
    "2020-01-02T00:00",
    "2020-01-03T00:00",
    "2020-01-04T00:00",
]
series = clct / 'Brussels'
series.write({
    'timestamp': timestamp,
    'value': range(4),
})

# Let's write some data through clct_bis
timestamp = [
    "2020-01-02T00:00", # starts 1 day later !
    "2020-01-03T00:00",
    "2020-01-04T00:00",
    "2020-01-05T00:00",
]
series_bis = clct_bis / 'Brussels'
series_bis.write({
    'timestamp': timestamp,
    'value': range(10, 14), # Other values!
})


# And try to pull and read it with the original repo, the second write (on
# `series_bis`) overshadows the first on (on `series`):
repo.pull(repo_bis)
pprint(series.df())
# ->
#    timestamp  value
# 0 2020-01-02   10.0
# 1 2020-01-03   11.0
# 2 2020-01-04   12.0
# 3 2020-01-05   13.0

# But no data is lost, we have two heads in the changelog, a merge
# will fix the situation:
pprint(clct.changelog.log())
# ->
# [<Revision 00000000000-0000000000000000000000000000000000000000.176768e5f52-efd4198272b4a671db506c360ce036e1d28c2efb *>,
#  <Revision 00000000000-0000000000000000000000000000000000000000.176768e5f57-a7ea839500abe659fef815a7591104e948d98d13 *>]
clct.merge()
pprint(clct.changelog.log())
# ->
# [<Revision 00000000000-0000000000000000000000000000000000000000.176768ef53a-efd4198272b4a671db506c360ce036e1d28c2efb >,
#  <Revision 176768ef53a-efd4198272b4a671db506c360ce036e1d28c2efb.176768ef546-3494115c8959ec5dac132f1d10c658f4ab4dc8e4 *>,
#  <Revision 00000000000-0000000000000000000000000000000000000000.176768ef53e-a7ea839500abe659fef815a7591104e948d98d13 >,
#  <Revision 176768ef53e-a7ea839500abe659fef815a7591104e948d98d13.176768ef547-3494115c8959ec5dac132f1d10c658f4ab4dc8e4 *>]


# We see that the merge as created two revisions both pointing to the
# new head (`176768ef547-34941...`).
# And we see the effect of the merge, the 1st of January is back:p
print(series.df())
#  ->
#     timestamp  value
# 0 2020-01-01    0.0
# 1 2020-01-02   10.0
# 2 2020-01-03   11.0
# 3 2020-01-04   12.0
# 4 2020-01-05   13.0
```

In case of concurrent writes on the same part of the timeseries, the
last write wins. It's important to note that machine clock is
important here, if concurrent writes here-above were made through two
different machines with incorrect clock the "last write" would be the
one from the machine with a clock running ahead.


# Roadmap / Ideas

- Selective suppression of past revisions
- Shallow clone
