import yaml

_WF = ".github/workflows/alaska-scrape.yml"


def test_alaska_scrape_imports_and_configures():
    import alaska_scrape
    assert alaska_scrape.MAX_LEGS_PER_SHARD == 40
    assert alaska_scrape.SHARDS >= 1
    # the scraper class is importable and is the AS httpx scraper
    from scrapers.alaska import AlaskaScraper
    assert AlaskaScraper.airline_code == "AS"


def test_alaska_workflow_shard_matrix_is_consistent():
    """matrix must be 0..n-1 and ALASKA_SHARDS must equal the matrix length so the stride
    partition (due[idx::n]) covers the whole due set. Alaska's 252-route queue needs >=3 shards
    to keep up now that each scheduled run is capped by the wall-clock budget."""
    with open(_WF) as f:
        wf = yaml.safe_load(f)
    job = wf["jobs"]["scrape"]
    shards = job["strategy"]["matrix"]["shard"]
    env = job["steps"][-1]["env"]
    n = int(env["ALASKA_SHARDS"])
    assert shards == list(range(n)), f"matrix {shards} must be range(ALASKA_SHARDS={n})"
    assert n >= 3, "Alaska runs at least 3 fresh-IP shards"
    assert env["ALASKA_SHARD_INDEX"] == "${{ matrix.shard }}"
