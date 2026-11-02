"""Independent regression coverage for compact live history, without network."""
import copy
from unittest.mock import Mock

import pytest

from compact_bars import CompactBars
from history_data import HistoricalDataFeed
from strategy_rules import MomentumConfig, evaluate_momentum, holding_signal
from test_daily_momentum import completed_bars


@pytest.mark.parametrize('count', [0, 1, 19, 20, 124, 125, 126, 127, 260])
@pytest.mark.parametrize('risk_adjusted', [False, True])
def test_window_boundary_preserves_entry_and_holding_signals(count, risk_adjusted):
    rows = completed_bars('2026-09-21', count=count)
    for i, row in enumerate(rows):
        # Prices deliberately differ from the raw basis. Keep exact float64
        # arithmetic, including tiny representable changes near thresholds.
        factor = 1.0000000000000002 if i % 2 else 1.125
        for field in ('open', 'close', 'high', 'low'):
            row[field] *= factor
        row['adjustment_factor'] = factor
        row['is_st'] = i == count - 1
    compact = CompactBars(rows, 126)
    config = MomentumConfig(risk_adjusted=risk_adjusted)
    assert evaluate_momentum(rows, '2026-09-21', config) == evaluate_momentum(compact, '2026-09-21', config)
    assert holding_signal(rows, '2026-09-21') == holding_signal(compact, '2026-09-21')
    assert [dict(row) for row in compact] == rows[-126:]


def test_fields_preserve_missing_none_boolean_large_int_and_mixed_types():
    rows = [
        {'date': '2026-09-18', 'is_st': False, 'raw_close': None, 'industry': 'a',
         'n': 2**53, 'huge': 2**60 + 3, 'mixed': True, 'negative_zero': -0.0},
        {'date': '2026-09-21', 'is_st': True, 'industry': None,
         'n': 1.5, 'huge': 2.0, 'mixed': 'unknown'},
    ]
    compact = CompactBars(rows)
    for expected, actual in zip(rows, compact):
        assert dict(actual) == expected
        for key, value in expected.items():
            assert type(actual[key]) is type(value)
    assert 'raw_close' not in compact[1]
    assert compact[0]['raw_close'] is None
    with pytest.raises(TypeError):
        compact[0]['raw_close'] = 1


@pytest.mark.parametrize('compact', [False, True])
def test_feed_compacts_before_callback_and_result_accumulation(tmp_path, compact):
    feed = HistoricalDataFeed(tmp_path)
    feed.compact_limit = 126 if compact else None
    feed._sources.status = Mock(return_value={})
    rows = completed_bars('2026-09-21', count=260)
    metadata = dict(fresh=True, raw_prices_available=True,
                    coverage={'count': 260}, warning=['preserve-provenance'])
    feed.metadata['sh600000'] = metadata
    feed.fetch_stock_kline = Mock(return_value=('sh600000', 'stock', rows))
    callbacks = []
    result = feed.fetch_universe_klines(['sh600000'], '2025-01-01', '2026-09-21',
                                       on_item=lambda sym, item: callbacks.append(item))
    item = result['sh600000']
    assert callbacks[0] is item
    assert isinstance(item['bars'], CompactBars) is compact
    assert len(item['bars']) == (126 if compact else 260)
    assert item['metadata'] == metadata
    assert ('by_date' in item) is (not compact)
    # Conversion must not truncate or mutate the on-disk/full download data.
    assert len(rows) == 260


def test_peek_converts_each_symbol_and_keeps_coverage(tmp_path):
    feed = HistoricalDataFeed(tmp_path)
    feed.compact_limit = 126
    rows = completed_bars('2026-09-21', count=260)
    source = {'bars': rows, 'metadata': {'coverage': {'count': 260}, 'fresh': False},
              'error': 'stale'}
    feed._peek_cached = Mock(side_effect=lambda *args: copy.deepcopy(source))
    result = feed.peek_universe(['sh600000', 'sz000001'], '2025-01-01', '2026-09-21')
    for item in result.values():
        assert isinstance(item['bars'], CompactBars)
        assert len(item['bars']) == 126
        assert item['metadata'] == source['metadata']
        assert item['error'] == 'stale'
