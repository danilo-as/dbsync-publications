from dbsync_publications.engine import referenced_keys


def test_referenced_keys_reads_distinct_non_null_keys_per_column_group(tmp_path):
    path = tmp_path / "child.csv"
    # Same layout as the export: COPY ... WITH (FORMAT csv, HEADER, NULL '\N').
    path.write_text(
        'id,region_id,country,code\n'
        '1,10,AR,""\n'
        '2,10,AR,x\n'
        '3,\\N,CL,y\n'
        '4,20,\\N,z\n',
        encoding="utf-8",
    )

    keys = referenced_keys(path, [("region_id",), ("country", "code")])

    assert keys == {
        ("region_id",): {("10",), ("20",)},
        # "" is an empty string (kept); \N is NULL (skipped).
        ("country", "code"): {("AR", ""), ("AR", "x"), ("CL", "y")},
    }
