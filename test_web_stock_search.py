"""Offline checks of actual search/quote JavaScript with a minimal DOM."""
from pathlib import Path
import shutil
import subprocess
import unittest

HTML = (Path(__file__).resolve().parent / 'web' / 'index.html').read_text()
NODE = shutil.which('node')

@unittest.skipUnless(NODE, 'Node required for offline browser checks')
class TestWebStockSearch(unittest.TestCase):
    def run_js(self, checks, extra=''):
        functions = HTML[HTML.index('    // Dedicated stock search'):HTML.index('    function renderQuotes(')]
        script = '''const assert=require('node:assert/strict');
        class El {
          constructor(){this.children=[];this.style={};this.value='';this.disabled=false;this.events={};this._text='';}
          set textContent(x){this._text=String(x);this.children=[];}
          get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
          set innerHTML(x){throw Error('Unsafe HTML');}
          appendChild(e){this.children.push(e);return e;}
          replaceChildren(){this.children=[];this._text='';}
          addEventListener(k,cb){this.events[k]=cb;}
          scrollIntoView(){}
        }
        const fields={};const document={hidden:false,createElement:()=>new El(),getElementById:k=>fields[k]||(fields[k]=new El())};
        let authorized=true;let currentQuotes={};let currentSide='BUY';let posts=0;let toasts=[];
        function showToast(t){toasts.push(t);}function setSide(s){currentSide=s;}
        let apiFetch=async()=>{throw Error('mock missing');};
        ''' + functions + '\n' + extra + '\n(async()=>{' + checks + '\n})().catch(e=>{console.error(e);process.exit(1);});'
        run = subprocess.run([NODE], input=script, text=True, capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_escaped_results_and_quote_no_first_match_order(self):
        self.run_js(r'''
        fields.stockSearchInput=document.getElementById('stockSearchInput');fields.stockSearchInput.value='银行';
        apiFetch=async()=>({ok:true,json:async()=>[{symbol:'sh600036',code:'600036',name:'<img onerror=alert(1)>',quote_available:false}]});
        await searchStocks();
        assert.equal(selectedStockSymbol,null);
        assert.match(fields.stockSearchResults.textContent,/<img onerror/);
        assert.equal(fields.stockSearchResults.children.length,1);
        assert.equal(fields.stockSearchResults.children[0].children.length,0);
        renderStockQuote({symbol:'sh600036',code:'600036',name:'<script>',quote_available:true,fresh:false,is_trading_time:true,can_buy:true,can_sell:true,available_shares:100,
        quote:{current:10,change_pct:-2,open:11,pre_close:12,volume_lots:5,bids:[{level:2,price:9,volume:2}],asks:[]}});
        assert.equal(fields.stockQuoteBuy.disabled,true);assert.equal(fields.stockQuoteSell.disabled,true);
        assert.match(fields.stockQuoteDetails.textContent,/报价过期/);
        assert.match(fields.stockQuoteDetails.textContent,/9.00 \/ 2/);
        ''')

    def test_slow_search_cannot_overwrite_new_query(self):
        self.run_js(r'''
        let resolveOld;
        apiFetch=async(url)=>url.includes('old') ? new Promise(r=>resolveOld=r) : {ok:true,json:async()=>[{symbol:'sh600036',code:'600036',name:'new'}]};
        document.getElementById('stockSearchInput').value='old';const old=searchStocks();
        document.getElementById('stockSearchInput').value='new';await searchStocks();
        resolveOld({ok:true,json:async()=>[{symbol:'sh600000',code:'600000',name:'old'}]});await old;
        assert.match(fields.stockSearchResults.textContent,/new/);assert.doesNotMatch(fields.stockSearchResults.textContent,/old/);
        ''')

    def test_quote_refresh_no_overlap_or_previous_selection_overwrite(self):
        self.run_js(r'''
        let resolvers=[];let calls=0;apiFetch=()=>{calls++;return new Promise(r=>resolvers.push(r));};
        selectedStockSymbol='sh600000';const old=refreshStockQuote();await refreshStockQuote();assert.equal(calls,1);
        const next=selectStockQuote('sh600036');assert.equal(calls,2);
        resolvers[1]({ok:true,json:async()=>({symbol:'sh600036',code:'600036',name:'new',quote:null})});await next;
        resolvers[0]({ok:true,json:async()=>({symbol:'sh600000',code:'600000',name:'old',quote:null})});await old;
        assert.equal(selectedStockQuote.symbol,'sh600036');assert.match(fields.stockQuoteDetails.textContent,/new/);
        document.hidden=true;await refreshStockQuote();assert.equal(calls,2);
        authorized=false;document.hidden=false;await refreshStockQuote();assert.equal(calls,2);
        ''')

    def test_prefill_market_price_explicit_side_and_no_post(self):
        self.run_js(r'''
        selectedStockQuote={symbol:'sh600036',fresh:true,quote_available:true,can_buy:true,can_sell:true,available_shares:237,quote:{current:10}};
        selectedStockQuoteReceivedAt=Date.now();
        apiFetch=()=>{posts++;};fillStockQuoteOrder('BUY');
        assert.equal(fields.orderSymbol.value,'sh600036');assert.equal(fields.orderShares.value,100);assert.equal(fields.orderPrice.value,'');assert.equal(currentSide,'BUY');
        fillStockQuoteOrder('SELL');assert.equal(fields.orderShares.value,237);assert.equal(currentSide,'SELL');assert.equal(posts,0);
        selectedStockQuote.fresh=false;fields.orderShares.value=999;fillStockQuoteOrder('BUY');assert.equal(fields.orderShares.value,999);
        ''')

    def test_quote_expires_locally_while_refresh_is_pending(self):
        self.run_js(r'''
        selectedStockQuote={symbol:'sh600036',code:'600036',name:'test',fresh:true,quote_available:true,can_buy:true,can_sell:false,quote:null,quote_age_seconds:119};
        selectedStockQuoteReceivedAt=Date.now()-2000;
        renderStockQuote(selectedStockQuote);assert.equal(fields.stockQuoteBuy.disabled,true);
        document.getElementById('orderSymbol').value='unchanged';fillStockQuoteOrder('BUY');
        assert.equal(fields.orderSymbol.value,'unchanged');
        ''')

    def test_submit_reports_actual_partial_fill_and_blocks_double_submit(self):
        submit = HTML[HTML.index('    let orderSubmitting = false;'):HTML.index('    async function toggleStrategy(')]
        self.run_js(r'''
        document.getElementById('orderSymbol').value='sh600036';document.getElementById('orderShares').value='500';document.getElementById('orderPrice').value='';
        let finish;apiFetch=async()=>{posts++;return new Promise(r=>finish=r);};
        let refreshes=0;refreshStockQuote=()=>{refreshes++;};
        const first=submitOrder();assert.equal(fields.submitOrderBtn.disabled,true);await submitOrder();assert.equal(posts,1);
        finish({json:async()=>({success:true,shares:200,status:'partial'})});await first;
        assert.match(toasts[0],/实际成交 200股，剩余 300股未成交/);assert.equal(fields.submitOrderBtn.disabled,false);assert.equal(refreshes,1);
        ''', extra=submit+'\nfunction fetchData(){}')

if __name__=='__main__':unittest.main()
