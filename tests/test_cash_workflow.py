import yaml

_WF = ".github/workflows/cash-browser-scrape.yml"


def _workflow():
    with open(_WF) as f:
        return yaml.safe_load(f)


def test_cash_workflow_runs_three_times_daily_with_safe_spacing():
    wf = _workflow()
    schedule = wf[True]["schedule"]
    crons = [s["cron"] for s in schedule]
    assert crons == ["15 6,14,22 * * *"]
    hours = [int(h) for h in crons[0].split()[1].split(",")]
    assert len(hours) == 3
    assert all(not (8 <= h <= 11) for h in hours)
    assert 20 not in hours


def test_cash_workflow_shards_and_route_limit_are_consistent():
    wf = _workflow()
    job = wf["jobs"]["scrape"]
    shards = job["strategy"]["matrix"]["shard"]
    env = job["steps"][-1]["env"]
    n = int(env["CASH_SHARDS"])
    assert shards == list(range(n))
    assert n == 6
    assert env["CASH_SHARD_INDEX"] == "${{ matrix.shard }}"
    assert env["CASH_SCRAPE_DAYS"] == "30"
    assert env["CASH_TOP_ROUTES"] == "800"
