def test_alaska_scrape_imports_and_configures():
    import alaska_scrape
    assert alaska_scrape.MAX_LEGS_PER_SHARD == 40
    assert alaska_scrape.SHARDS >= 1
    # the scraper class is importable and is the AS httpx scraper
    from scrapers.alaska import AlaskaScraper
    assert AlaskaScraper.airline_code == "AS"
