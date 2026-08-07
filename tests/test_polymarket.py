from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from orderbook_ball.polymarket import (
    _event_icon,
    _is_live_binary_market,
    _market_from_sdk,
    _search_result_from_sdk,
)


def _market(*, closed=False, active=True, enable_order_book=True, with_tokens=True):
    yes = SimpleNamespace(label="Yes", token_id="yes-token" if with_tokens else None)
    no = SimpleNamespace(label="No", token_id="no-token" if with_tokens else None)
    return SimpleNamespace(
        id="7",
        slug="yes-no-market",
        question="Will it happen?",
        group_item_title=None,
        condition_id="0xabc",
        outcomes=SimpleNamespace(yes=yes, no=no),
        state=SimpleNamespace(
            closed=closed,
            archived=False,
            active=active,
            enable_order_book=enable_order_book,
        ),
    )


def _event(markets):
    optimized = SimpleNamespace(
        image_url_optimized="https://example.com/optimized.webp",
        image_url_source="https://example.com/source.png",
    )
    return SimpleNamespace(
        id="42",
        slug="will-it-happen",
        title="Will it happen?",
        subtitle="A useful subtitle",
        category="News",
        icon="https://example.com/icon.png",
        image="https://example.com/image.png",
        display=SimpleNamespace(icon_optimized=optimized, image_optimized=None),
        metrics=SimpleNamespace(
            volume=Decimal("123456.5"),
            volume_24hr=Decimal("9000"),
            liquidity=Decimal("42000"),
        ),
        schedule=SimpleNamespace(end_date=datetime(2026, 9, 1, tzinfo=UTC)),
        markets=tuple(markets),
    )


def test_sdk_market_adapter_preserves_token_pair():
    result = _market_from_sdk(_market())
    assert result is not None
    assert result.a_label == "Yes"
    assert result.aprime_label == "No"
    assert result.a_token == "yes-token"
    assert result.aprime_token == "no-token"


def test_live_binary_filter_rejects_closed_or_tokenless_market():
    assert _is_live_binary_market(_market())
    assert not _is_live_binary_market(_market(closed=True))
    assert not _is_live_binary_market(_market(with_tokens=False))


def test_search_result_keeps_sdk_metadata_and_live_market_count():
    event = _event([_market(), _market(closed=True)])
    result = _search_result_from_sdk(event)
    assert result is not None
    assert result.slug == "will-it-happen"
    assert result.binary_market_count == 1
    assert result.volume == 123456.5
    assert result.volume_24h == 9000
    assert result.liquidity == 42000
    assert result.end_date.startswith("2026-09-01")


def test_optimized_icon_is_preferred():
    assert _event_icon(_event([_market()])) == "https://example.com/optimized.webp"
