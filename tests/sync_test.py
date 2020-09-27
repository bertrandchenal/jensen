from time import sleep

import pytest

from lakota import Frame, Repo, Schema
from lakota.utils import drange

schema = Schema(
    """
timestamp int*
value float
"""
)


def test_pull(threaded):
    c_label = "a_collection"
    s_label = "a_series"
    remote_repo = Repo()
    remote_coll = remote_repo.create_collection(schema, c_label)
    rseries = remote_coll / s_label
    for i in range(10):
        rseries.write(
            {
                "timestamp": range(i, i + 10),
                "value": range(i + 100, i + 110),
            }
        )
    expected = rseries.frame()

    # Test pull
    local_repo = Repo()
    local_coll = local_repo.create_collection(schema, c_label)
    local_coll.pull(remote_coll)
    lseries = local_coll / s_label
    assert lseries.frame() == expected

    # Test push
    other_repo = Repo()
    other_coll = other_repo.create_collection(schema, c_label)
    remote_coll.push(other_coll)
    oseries = other_coll / s_label
    assert oseries.frame() == expected

    # Test with existing series
    local_repo = Repo()
    local_coll = local_repo.create_collection(schema, c_label)
    local_coll.pull(remote_coll)
    lseries = other_repo.create_collection(schema, c_label) / s_label
    assert oseries.frame() == expected

    # Test with existing series with existing data
    local_repo = Repo()
    local_coll = local_repo.create_collection(schema, c_label)
    lseries = local_coll / s_label
    frm = Frame(
        schema,
        {
            "timestamp": range(0, 20),
            "value": range(10, 20),
        },
    )
    lseries.write(frm)
    local_coll.pull(remote_coll, s_label)
    assert lseries.frame() == frm

    # Test with existing series with other schema
    local_repo = Repo()
    other_schema = Schema(["timestamp int*", "value int"])
    local_coll = local_repo.create_collection(other_schema, c_label)
    lseries = local_coll / s_label

    with pytest.raises(ValueError):
        local_repo.pull(remote_repo)


@pytest.mark.parametrize("squash", [False, True])
def test_label_delete_push(squash):
    labels = list("abcd")
    local_repo = Repo()
    local_clct = local_repo.create_collection(schema, "a_collection")
    remote_repo = Repo()
    remote_clct = remote_repo.create_collection(schema, "a_collection")

    # Write some data
    frm = {
        "timestamp": [1, 2, 3],
        "value": [1, 2, 3],
    }
    for label in labels:
        series = local_clct / label
        series.write(frm)

    # Create some labels and push them
    local_clct.push(remote_clct)
    if squash:
        remote_clct.squash()
    assert list(local_clct) == labels
    assert list(remote_clct) == labels

    # FIXME !

    # Delete one local label and push again
    local_clct.delete("c")
    local_clct.push(remote_clct)
    if squash:
        remote_clct.squash()
    else:
        remote_clct.refresh()
    assert list(remote_clct) == list("abd")
    assert list(local_clct) == list("abd")

    # Delete one remote label and pull
    sleep(0.1)  # Needed to avoid concurrent writes
    remote_clct.delete("d")
    local_clct.pull(remote_clct)
    if squash:
        local_clct.squash()
    else:
        local_clct.refresh()
    assert list(remote_clct) == list("ab")
    assert list(local_clct) == list("ab")


@pytest.mark.parametrize("squash", [True, False])
def test_series_push(squash):
    label = "LABEL"
    local_repo = Repo()
    local_coll = local_repo.create_collection(schema, "a_collection")
    remote_repo = Repo()
    remote_coll = remote_repo.create_collection(schema, "a_collection")
    series = local_coll / label

    months = list(range(1, 12))
    for start, stop in zip(months[:-1], months[1:]):
        ts = drange(f"2020-{start}-01", f"2020-{stop}-01", days=1)
        values = [start] * len(ts)
        series.write({"timestamp": ts, "value": values})

    local_coll.push(remote_coll, label)
