from orderbook_ball.polymarket import _event_icon, _search_result_from_gamma


def _market(**overrides):
    value = {
        "slug": "yes-no-market",
        "question": "Will it happen?",
        "conditionId": "0xabc",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes-token", "no-token"]',
        "active": True,
        "closed": False,
        "enableOrderBook": True,
    }
    value.update(overrides)
    return value


def test_search_result_keeps_live_binary_markets_and_metadata():
    event = {
        "id": "42",
        "slug": "will-it-happen",
        "title": "Will it happen?",
        "subtitle": "A useful subtitle",
        "icon": "https://example.com/icon.png",
        "volume": "123456.5",
        "volume24hr": 9000,
        "liquidity": "42000",
        "endDate": "2026-09-01T00:00:00Z",
        "markets": [_market(), _market(slug="closed", closed=True)],
    }
    result = _search_result_from_gamma(event)
    assert result is not None
    assert result.slug == "will-it-happen"
    assert result.binary_market_count == 1
    assert result.volume == 123456.5
    assert result.volume_24h == 9000
    assert result.liquidity == 42000
    assert result.icon == "https://example.com/icon.png"


def test_search_result_rejects_events_without_live_binary_clob_market():
    event = {"slug": "bad", "title": "Bad", "markets": [_market(closed=True)]}
    assert _search_result_from_gamma(event) is None


def test_optimized_icon_is_preferred():
    event = {
        "icon": "https://example.com/source.png",
        "iconOptimized": {"imageUrlOptimized": "https://example.com/optimized.webp"},
    }
    assert _event_icon(event) == "https://example.com/optimized.webp"
