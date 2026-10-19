"""Exercise actual holdings renderer and refresh isolation without external services."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest

HTML = (Path(__file__).resolve().parent / 'web' / 'index.html').read_text()
NODE = shutil.which('node')


@unittest.skipUnless(NODE, 'Node required for browser regression checks')
class TestWebRisk(unittest.TestCase):
    def run_js(self, source, checks):
        script = "const assert = require('node:assert/strict');\n" + source + '\n' + checks
        result = subprocess.run([NODE], input=script, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def renderer(self, checks):
        source = HTML[HTML.index('    function positionEscape('):HTML.index('    // Dedicated stock search')]
        self.run_js("const tbody = {}; const document={getElementById:()=>tbody};\n" + source, checks)

    def test_old_response_is_explicitly_unverified_and_keeps_loss_color(self):
        self.renderer(r"""
          renderPositions([{symbol:'sh605577',name:'龙版传媒',total_shares:300,available_shares:300,
            cost_price:17.66,last_price:16.51,market_value:4953,unrealized_pnl:-344.05,pnl_pct:-6.5}]);
          assert.match(tbody.innerHTML,/缓存估值，时间未核验/);
          assert.match(tbody.innerHTML,/行情时间未知/);
          assert.match(tbody.innerHTML,/风控状态暂不可用/);
          assert.match(tbody.innerHTML,/class="text-right down"/);
          assert.match(tbody.innerHTML,/-6.50%/);
          assert.match(tbody.innerHTML,/quickSell\('sh605577', 300\)/);
        """)

    def test_blocked_stop_displays_trigger_mode_reason_time_and_escapes_all_text(self):
        self.renderer("""
          renderPositions([{symbol:'sh605577',name:'<img src=x onerror=alert(1)>',total_shares:300,
            available_shares:0,cost_price:17.66,last_price:16.51,market_value:4953,
            unrealized_pnl:-344.05,pnl_pct:-6.5,quote_status:'stale',quote_time:'2026-10-19 09:30:00',
            risk:{status:'blocked',stop_loss_mode:'intraday',stop_price:16.6,triggered:true,
            reason:'止损触发',blocked_reason:'<script>alert(1)</script>',signal_date:'2026-10-19'}}]);
          assert.match(tbody.innerHTML,/卖出受阻/);
          assert.match(tbody.innerHTML,/盘中硬止损/);
          assert.match(tbody.innerHTML,/止损参考价 ¥16.60/);
          assert.match(tbody.innerHTML,/过期报价，仅供估值/);
          assert.match(tbody.innerHTML,/2026-10-19 09:30:00/);
          assert.match(tbody.innerHTML,/&lt;script&gt;/);
          assert.doesNotMatch(tbody.innerHTML,/<script>|<img/);
          assert.match(tbody.innerHTML,/disabled/);
        """)

    def test_daily_close_mode_is_explicit_and_empty_table_has_ten_columns(self):
        self.renderer("""
          renderPositions([{symbol:'sz002242',name:'九阳股份',risk:{status:'pending',stop_loss_mode:'daily_close'}}]);
          assert.match(tbody.innerHTML,/收盘确认，次日执行/);
          assert.match(tbody.innerHTML,/待执行退出/);
          renderPositions([]); assert.match(tbody.innerHTML,/colspan="10"/);
          assert.throws(()=>renderPositions({error:'bad response'}),/格式无效/);
        """)

    def test_refresh_one_endpoint_failure_does_not_freeze_holdings(self):
        source = HTML[HTML.index('    async function fetchData()'):HTML.index('    function renderSummary(')]
        self.run_js("""
          const state = {style:{}}; const document={getElementById:()=>state};
          const rendered=[];
          const apiFetch=async path=> { if(path==='/api/quotes') throw new Error('offline');
            return {ok:true,json:async()=>path==='/api/positions'?[{symbol:'sh605577'}]:{}}; };
          const renderPositions=value=>rendered.push(value[0].symbol);
          const renderSummary=()=>{};const renderQuotes=()=>{};const renderTrades=()=>{};
          const renderStatus=()=>{};const renderPerformance=()=>{};
        """ + source, """
          fetchData().then(()=>{
            assert.deepEqual(rendered,['sh605577']);
            assert.match(state.textContent,/刷新失败：行情/);
            assert.match(state.textContent,/可能仍为旧数据/);
          }).catch(error=>{console.error(error);process.exitCode=1;});
        """)

    def test_stop_mode_roundtrip_in_actual_strategy_payload(self):
        source = HTML[HTML.index('    function loadStrategyParameterControls('):HTML.index('    async function submitStrategySwitch(')]
        self.run_js("""
          const fields = new Proxy({}, {get:(obj,key)=>obj[key] || (obj[key]={value:'',tagName:'INPUT'})});
          const document = {getElementById:key=>fields[key]};
          let selectedStrategyId='Momentum_Rotation_Pro';
        """ + source, """
          loadStrategyParameterControls({});
          assert.equal(fields.modalStopLossMode.value,'intraday');
          assert.equal(buildStrategySwitchPayload().stop_loss_mode,'intraday');
          loadStrategyParameterControls({stop_loss_mode:'daily_close'});
          assert.equal(buildStrategySwitchPayload().stop_loss_mode,'daily_close');
          selectedStrategyId='TurtleBreakout';
          assert.equal('stop_loss_mode' in buildStrategySwitchPayload(),false);
        """)

    def test_no_duplicate_static_dom_ids(self):
        ids = re.findall(r'\bid="([^"{}$]+)"', HTML)
        self.assertEqual(len(ids), len(set(ids)), sorted(i for i in set(ids) if ids.count(i)>1))


if __name__ == '__main__':
    unittest.main()
