"""A cancelled/invalid replay must leave no SQLite work files behind."""
import pytest
from test_momentum_backtest import fixture_data, engine


@pytest.mark.parametrize('phase', ['prepare', 'replay'])
def test_cancel_closes_partial_store(tmp_path, phase):
    data, dates = fixture_data()

    def cancel(current, done, total):
        if current == phase:
            raise InterruptedError('cancelled')

    runner = engine(data, dates, streaming=True, spill_results=True,
                    work_dir=tmp_path, progress=cancel)
    with pytest.raises(InterruptedError):
        runner.run()
    assert list(tmp_path.iterdir()) == []
    assert runner._stream_store is None
    runner.close()


@pytest.mark.parametrize('invalid', ['duplicate', 'raw'])
def test_invalid_input_closes_partial_store(tmp_path, invalid):
    data, dates = fixture_data()
    bars = data['stocks']['sh600096']['bars']
    if invalid == 'duplicate':
        bars.insert(5, dict(bars[4]))
    else:
        bars[5].pop('raw_close')
    runner = engine(data, dates, streaming=True, work_dir=tmp_path)
    with pytest.raises(ValueError):
        runner.run()
    assert list(tmp_path.iterdir()) == []
    runner.close()


@pytest.mark.parametrize('error,raw', [('cache_miss', False),
    ('cache_range_insufficient', True), ('risk_status_unverified', True),
    (None, False)])
def test_cache_only_excludes_untrusted_symbol(tmp_path, error, raw):
    from streaming_history import CacheOnlyFeed
    from unittest.mock import patch
    feed = CacheOnlyFeed(cache_dir=tmp_path)
    item = dict(name='test', bars=[{'date':'2024-01-02', 'raw_close':10}],
                error=error, metadata=dict(raw_prices_available=raw, error=error))
    with patch.object(feed, '_peek_cached', return_value=item):
        symbol, name, bars = feed.fetch_stock_kline('sh600096', '2024-01-01', '2024-02-01')
    assert bars == []
    assert feed.metadata[symbol]['cache_only'] is True
    assert feed.metadata[symbol]['error'] == (error or 'raw_prices_missing')


def test_cache_only_preserves_verified_bars_and_benchmark_failure(tmp_path):
    from streaming_history import CacheOnlyFeed
    from unittest.mock import patch
    feed = CacheOnlyFeed(cache_dir=tmp_path)
    item = dict(name='test', bars=[{'date':'2024-01-02'}], error=None,
                metadata=dict(raw_prices_available=True, fresh=False))
    with patch.object(feed, '_peek_cached', return_value=item):
        assert feed.fetch_stock_kline('sh600096', '2024-01-01', '2024-02-01')[2] == item['bars']
    with patch.object(feed, '_peek_cached', return_value=dict(item, error='cache_miss', bars=[])):
        with pytest.raises(ValueError, match='cached_benchmark_unavailable'):
            feed.fetch_benchmark_kline(start_date='2024-01-01', end_date='2024-02-01')
