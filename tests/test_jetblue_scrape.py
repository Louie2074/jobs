import yaml

_WF = ".github/workflows/jetblue-scrape.yml"


def test_jetblue_scrape_imports_and_configures():
    import jetblue_scrape
    assert jetblue_scrape.MAX_LEGS_PER_SHARD == 30
    from scrapers.jetblue import JetBlueScraper
    assert JetBlueScraper.airline_code == "B6"


def test_jetblue_workflow_shard_matrix_is_consistent():
    """matrix must be 0..n-1 and JETBLUE_SHARDS must equal the matrix length so the stride
    partition (due[idx::n]) covers the whole due set. JetBlue runs >=2 shards so a scheduled
    run can drain its slice inside the wall-clock budget (single-shard runs overran the 60-min
    GitHub-Actions cap)."""
    with open(_WF) as f:
        wf = yaml.safe_load(f)
    job = wf["jobs"]["scrape"]
    shards = job["strategy"]["matrix"]["shard"]
    env = job["steps"][-1]["env"]
    n = int(env["JETBLUE_SHARDS"])
    assert shards == list(range(n)), f"matrix {shards} must be range(JETBLUE_SHARDS={n})"
    assert n >= 2, "JetBlue runs at least 2 fresh-IP shards"
    assert env["JETBLUE_SHARD_INDEX"] == "${{ matrix.shard }}"
