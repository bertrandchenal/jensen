from numpy import asarray
from pandas import DataFrame, date_range

from lakota.schema import Codec, Schema
from lakota.utils import strpt


def test_simple_codec():
    dt = "M8[s]"
    ts = date_range(f"2020-01-01", f"2021-01-01", freq="1min", closed="left")
    arr = asarray(ts, dtype=dt)
    codec = Codec(dt, "blosc")
    data = codec.encode(arr)
    arr2 = codec.decode(data)
    assert all(arr == arr2)

    dt = "f8"
    arr = asarray(range(-100, 100), dtype=dt)
    codec = Codec(dt, "blosc")
    data = codec.encode(arr)
    arr2 = codec.decode(data)
    assert all(arr == arr2)


def test_vlen_codecs():
    for codecs in ("", "vlen-utf8", "vlen-utf8 gzip"):
        schema = Schema(f"val str*  |{codecs}")

        arr = asarray(["ham", "spam"])
        buff = schema["val"].codec.encode(arr)
        arr2 = schema["val"].codec.decode(buff)

        assert all(arr == arr2)
        assert arr.dtype == arr2.dtype


def test_schema_from_frame():
    frm = {
        "timestamp": asarray(["2020-01-01", "2020-01-02"], dtype="M8[s]"),
        "float": asarray([1, 2], dtype="float"),
        "int": asarray([1, 2], dtype="int"),
        "str": asarray([1, 2], dtype="U"),
    }

    for use_df in (True, False):
        if use_df:
            frm = DataFrame(frm)
        schema = Schema.from_frame(frm, ["timestamp"])
        assert schema["str"].codec.dt == "O"
        assert schema["timestamp"].codec.dt == "M8[s]"
        assert schema["int"].codec.dt == "i8"
        assert schema["float"].codec.dt == "f8"


def test_serialize():
    schema = Schema(
        """
    timestamp timestamp*
    float f8
    int i8
    str str
    """
    )

    ts = strpt("2020-01-01")
    values = (ts, 1.1, 1, "one")
    expected = ("2020-01-01 00:00:00", "1.1", "1", "one")
    assert schema.serialize(values) == expected
    assert schema.deserialize(expected) == values
