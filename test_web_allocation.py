"""Run the real allocation UI and parameter roundtrip without network calls."""
from pathlib import Path
import shutil
import subprocess
import unittest

HTML = (Path(__file__).resolve().parent / 'web' / 'index.html').read_text()
NODE = shutil.which('node')


@unittest.skipUnless(NODE, 'Node required for allocation browser checks')
class TestWebAllocation(unittest.TestCase):
    def run_js(self, source, checks):
        result = subprocess.run([NODE], input="const assert=require('node:assert/strict');\n" + source + checks,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def render(self, checks):
        source = HTML[HTML.index('    function renderAllocationStatus('):HTML.index('    function renderDataMonitor(')]
        self.run_js("""
          const fields = new Proxy({}, {get:(obj,key)=>obj[key] || (obj[key]={
            set innerHTML(value) { throw new Error('allocation text must never use HTML'); }
          })});
          const document = {getElementById:key=>fields[key]};
        """ + source, checks)

    def test_old_response_does_not_imply_candidates_are_orders(self):
        self.render("""
          renderAllocationStatus({candidate_count:97});
          assert.match(fields.dmAllocationStatus.innerText,/服务未提供/);
          assert.match(fields.dmAllocationStatus.innerText,/不能据候选数判断是否买入/);
          assert.match(fields.dmAllocationSchedule.innerText,/待获取/);
        """)

    def test_queue_reserves_are_not_displayed_as_97_purchase_orders(self):
        self.render("""
          renderAllocationStatus({allocation_status:{vacant_slots:2,pending_entry_count:97,
            refill_enabled:true,can_refill:false,wait_reason:'等待开市',
            next_rebalance_signal_date:'2026-10-20',next_execution_date:'2026-10-21',
            last_rebalance:'2026-10-13',entry_signal_date:'2026-10-16'}});
          assert.match(fields.dmAllocationStatus.innerText,/空位 2 个/);
          assert.match(fields.dmAllocationStatus.innerText,/待检查候选 97 只/);
          assert.match(fields.dmAllocationStatus.innerText,/最多补空位 2 只/);
          assert.match(fields.dmAllocationStatus.innerText,/等待开市/);
          assert.match(fields.dmAllocationSchedule.innerText,/2026-10-20.*该次轮动最早执行: 2026-10-21/);
          assert.match(fields.dmAllocationSchedule.title,/2026-10-13.*2026-10-16/);
        """)

    def test_untrusted_reason_and_dates_are_rendered_only_as_text(self):
        self.render("""
          const value='<img src=x onerror=alert(1)>';
          renderAllocationStatus({allocation_status:{vacant_slots:0,pending_entry_count:0,
            refill_enabled:false,wait_reason:value,next_rebalance_signal_date:value}});
          assert.match(fields.dmAllocationStatus.innerText,/空位补仓已关闭/);
          assert.ok(fields.dmAllocationStatus.innerText.includes(value));
          assert.ok(fields.dmAllocationSchedule.innerText.includes(value));
          renderAllocationStatus({allocation_status:{can_refill:true}});
          assert.match(fields.dmAllocationStatus.innerText,/仍须满足开仓和成交约束/);
        """)

    def test_refill_checkbox_roundtrip_preserves_false_and_defaults_true(self):
        source = HTML[HTML.index('    function loadStrategyParameterControls('):HTML.index('    async function submitStrategySwitch(')]
        self.run_js("""
          const fields = new Proxy({}, {get:(obj,key)=>obj[key] || (obj[key]={value:'',tagName:'INPUT'})});
          const document = {getElementById:key=>fields[key]};
          let selectedStrategyId='Momentum_Rotation_Pro';
        """ + source, """
          loadStrategyParameterControls({});
          assert.equal(fields.modalRefillVacancies.checked,true);
          assert.equal(buildStrategySwitchPayload().refill_vacancies,true);
          loadStrategyParameterControls({refill_vacancies:false});
          assert.equal(fields.modalRefillVacancies.checked,false);
          assert.equal(buildStrategySwitchPayload().refill_vacancies,false);
          fields.modalRefillVacancies.checked=true;
          assert.equal(buildStrategySwitchPayload().refill_vacancies,true);
          selectedStrategyId='TurtleBreakout';
          assert.equal('refill_vacancies' in buildStrategySwitchPayload(),false);
        """)


if __name__ == '__main__':
    unittest.main()
