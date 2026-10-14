"""Exercise the actual browser payload builder offline, without a browser service."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


HTML = (Path(__file__).resolve().parent / "web" / "index.html").read_text()
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node is needed only for browser JavaScript regression checks")
class TestWebBacktest(unittest.TestCase):
    def run_js(self, assertions):
        controls = HTML[HTML.index("    function updateBacktestControls("):HTML.index("    async function runBacktestUI(")]
        fields = {name: dict(value=value, style={}) for name, value in {
            "btStrategy": "Momentum_Rotation_Pro", "btPool": "core_active", "btStart": "2024-01-01",
            "btEnd": "2026-10-13", "btCash": "100000", "btTakeProfit": "3.5", "btStopLoss": "-6",
            "btParameters": '{"max_positions":8,"stop_loss_pct":-10,"alpha_high":0.88}',
            "btUseCurrentConfig": "", "btTakeProfitField": "", "btStopLossField": "",
            "btParametersEditor": "", "btContextNote": "",
        }.items()}
        fields["btUseCurrentConfig"]["checked"] = True
        script = "const assert = require('node:assert/strict');\n"
        script += "const fields = " + json.dumps(fields) + ";\n"
        script += "const document = {getElementById: key => fields[key]};\n"
        script += "let backtestContext = {current_strategy_id:'Momentum_Rotation_Pro', current_parameters:{max_positions:3}, current_pool_count:3485};\n"
        script += controls + "\n" + assertions
        subprocess.run([NODE], input=script, text=True, capture_output=True, check=True, timeout=10)

    def test_current_config_request_keeps_small_pool_and_does_not_send_legacy_overrides(self):
        self.run_js("""
          updateBacktestControls();
          const payload = buildBacktestPayload();
          assert.equal(payload.use_current_config, true);
          assert.equal(payload.pool_type, 'core_active');
          assert.equal('parameters' in payload, false);
          assert.equal('stop_loss' in payload, false);
          assert.equal('take_profit' in payload, false);
          assert.match(fields.btContextNote.textContent, /生产监控池不同/);
          assert.equal(fields.btTakeProfitField.style.display, 'none');
          assert.equal(fields.btStopLossField.style.display, 'none');
        """)

    def test_independent_json_and_explicit_full_watchlist_are_sent_unchanged(self):
        self.run_js("""
          fields.btUseCurrentConfig.checked = false;
          fields.btPool.value = 'current_watchlist';
          const edited = fields.btParameters.value;
          updateBacktestControls();
          assert.equal(fields.btParameters.value, edited);
          const payload = buildBacktestPayload();
          assert.equal(payload.use_current_config, false);
          assert.equal(payload.pool_type, 'current_watchlist');
          assert.deepEqual(payload.parameters, JSON.parse(edited));
          assert.match(fields.btContextNote.textContent, /3485只/);
          assert.match(fields.btContextNote.textContent, /耗时较长/);
        """)

    def test_legacy_strategy_keeps_legacy_controls_and_cannot_use_current_momentum_config(self):
        self.run_js("""
          fields.btStrategy.value = 'Minervini_SEPA';
          updateBacktestControls();
          assert.equal(fields.btUseCurrentConfig.disabled, true);
          assert.equal(fields.btUseCurrentConfig.checked, false);
          const payload = buildBacktestPayload();
          assert.equal(payload.use_current_config, false);
          assert.equal(payload.take_profit, 3.5);
          assert.equal(payload.stop_loss, -6);
          assert.equal('parameters' in payload, false);
        """)

    def test_entire_inline_javascript_parses(self):
        inline = "\n".join(re.findall(r"<script>([\s\S]*?)</script>", HTML))
        subprocess.run([NODE, "--check"], input=inline, text=True, capture_output=True, check=True, timeout=10)

    def test_entry_controls_preserve_zero_and_current_parameters(self):
        controls = HTML[HTML.index("    function loadStrategyParameterControls("):HTML.index("    async function submitStrategySwitch(")]
        ids = ("modalMaxWeight", "modalMaxPositions", "modalStopLoss", "modalEntryGapDown",
               "modalEntryGapUp", "modalEntryIntradayDrop", "modalEntryRequireMA20")
        fields = {key: dict(value="", checked=True, tagName="INPUT") for key in ids}
        script = "const assert = require('node:assert/strict');\nconst fields = " + json.dumps(fields) + ";\n"
        script += "const document = {getElementById: key => fields[key]}; let selectedStrategyId='Momentum_Rotation_Pro';\n"
        script += controls + """
          loadStrategyParameterControls({max_stock_weight:.17,max_positions:8,stop_loss_pct:-7,
            max_entry_gap_down_pct:0,max_entry_gap_up_pct:4,max_entry_intraday_drop_pct:2.5,entry_require_ma20:false});
          assert.deepEqual(buildStrategySwitchPayload(), {strategy_id:'Momentum_Rotation_Pro',
            max_stock_weight:.17,max_positions:8,stop_loss:-7,max_entry_gap_down_pct:0,
            max_entry_gap_up_pct:4,max_entry_intraday_drop_pct:2.5,entry_require_ma20:false});
          fields.modalEntryGapDown.value='';
          assert.throws(buildStrategySwitchPayload, /有限数值/);
          selectedStrategyId='TurtleBreakout';
          assert.equal('max_entry_gap_down_pct' in buildStrategySwitchPayload(),false);
        """
        subprocess.run([NODE], input=script, text=True, capture_output=True, check=True, timeout=10)

    def test_entry_rejections_use_text_and_translate_latest_events(self):
        function = HTML[HTML.index("    function renderEntryRejections("):HTML.index("    function quickBuy(")]
        script = "const assert = require('node:assert/strict'); const badge={}; const document={getElementById:()=>badge};\n"
        script += function + """
          renderEntryRejections([{symbol:'sh605577',date:'2026-10-14',reason:'entry_limit_down'},
            {name:'<img onerror=alert(1)>',date:'2026-10-14',reason:'entry_gap_down'}]);
          assert.match(badge.innerText,/跌停禁止开仓/);
          assert.match(badge.innerText,/低开超过阈值/);
          assert.equal('innerHTML' in badge,false);
          renderEntryRejections([]); assert.equal(badge.innerText,'');
        """
        subprocess.run([NODE], input=script, text=True, capture_output=True, check=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
