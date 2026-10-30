import './isolated-postgres.mjs';
import assert from 'node:assert/strict';
import {spawn, spawnSync} from 'node:child_process';
import {setTimeout as sleep} from 'node:timers/promises';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import puppeteer from 'puppeteer';
const repo=process.cwd(), py=path.join(repo,'.venv/bin/python');
const temp=fs.mkdtempSync(path.join(repo,'.tmp-qbm-perf-'));
const sock=net.createServer();await new Promise(r=>sock.listen(0,'127.0.0.1',r));
const port=sock.address().port;await new Promise(r=>sock.close(r));
const base=`http://127.0.0.1:${port}`;
let server,browser;let delayedMath=false,delayOld=false;
try {
 const seeded=spawnSync(py,['tests/e2e/_qb_manage_review_seed.py',temp+'/root',temp+'/seed'],{encoding:'utf8'});
 assert.equal(seeded.status,0,seeded.stderr);
 server=spawn(py,['-m','question_bank.cli','serve','--root',temp+'/root','--port',String(port)],{stdio:'ignore'});
 for(let i=0;i<100;i++){try{if((await fetch(base+'/healthz')).ok)break;}catch{}await sleep(100);}
 browser=await puppeteer.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 const page=await browser.newPage();await page.setViewport({width:1500,height:1000});
 const errors=[],outside=[],fonts=[];
 page.on('pageerror',e=>{errors.push(e.message);console.log('PAGEERROR',e.message)});
 page.on('console',m=>{if(m.type()==='error')console.log('CONSOLE',m.text())});
 page.on('requestfailed',r=>console.log('REQUESTFAILED',r.url(),r.failure()?.errorText));
 page.on('response',r=>{if(r.status()>=400||r.url().endsWith('/tex-mml-chtml.js'))console.log('HTTP',r.status(),r.url())});
 page.on('request',r=>{if(r.url().endsWith('.woff2'))fonts.push(r.url())});
 await page.setRequestInterception(true);
 page.on('request',async r=>{
  try {
   if(r.url().startsWith('data:')||r.url().startsWith('blob:')){await r.continue();return;}
   if(!r.url().startsWith(base)){outside.push(r.url());await r.abort();return;}
   if(r.method()!=='GET'){await r.abort();return;} // No AI/model calls or business mutations.
   if(r.url().endsWith('/tex-mml-chtml.js')){await sleep(5000);delayedMath=true;}
   if(delayOld && (r.url().includes('year=2015')||r.url().includes('paper_id=2015-SONGJIANG-ERMO')))await sleep(1200);
   if(!r.isInterceptResolutionHandled())await r.continue();
  }catch{}
 });
 await page.goto(base,{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>document.querySelector('#item-id')?.textContent.includes('Q002'),{timeout:4000});
 assert.equal(delayedMath,false,'Catalog and reader must render before the delayed formula library');
 assert.equal(await page.$eval('#bank-select',n=>n.disabled),false);
 console.log('PASS catalog and reader usable while MathJax is stalled');
 try { await page.waitForFunction(()=>Boolean(window.MathJax?.typesetPromise),{timeout:15000}); } catch(e) { console.log('MATHDEBUG', delayedMath, await page.evaluate(()=>({keys:Object.keys(window.MathJax||{}),startup:Object.keys(window.MathJax?.startup||{}),status:document.getElementById('formula-status').textContent,src:document.getElementById('mathjax-script').src}))); throw e; }
 await page.evaluate(()=>window.MathJax.startup.promise);
 await page.evaluate(async()=>{
  const node=document.createElement('div');node.id='perf-formula';node.textContent='\\(\\mathbb{R}+x^2\\)';document.body.append(node);QuestionMath.typeset(node);
 });
 await page.waitForSelector('#perf-formula mjx-container');await page.evaluate(()=>document.fonts.ready);
 assert.equal(outside.length,0,JSON.stringify(outside));assert.ok(fonts.length);
 console.log('PASS local formula rendering and font loading without external requests');
 delayOld=true;
 await page.select('#filter-year','2015');await sleep(50);await page.select('#filter-year','2016');
 await page.waitForFunction(()=>document.querySelector('#bank-select')?.value==='2016-XUHUI-YIMO'&&!document.querySelector('#bank-select').disabled);
 await sleep(1400);
 assert.equal(await page.$eval('#bank-select',n=>n.value),'2016-XUHUI-YIMO');
 console.log('PASS latest filter wins over a delayed canceled request');
 await page.select('#filter-year','');
 await page.waitForFunction(()=>document.querySelectorAll('#bank-select option').length===3&&!document.querySelector('#bank-select').disabled);
 await page.select('#bank-select','2015-SONGJIANG-ERMO');await sleep(50);await page.select('#bank-select','2016-XUHUI-YIMO');
 await page.waitForFunction(()=>document.querySelector('#item-id')?.textContent.includes('2016-XUHUI-YIMO'));
 await sleep(1400);
 assert.ok((await page.$eval('#item-id',n=>n.textContent)).includes('2016-XUHUI-YIMO'));
 assert.equal(errors.length,0,JSON.stringify(errors));
 console.log('PASS latest paper wins over delayed directory; no browser errors');
 console.log(JSON.stringify(await page.evaluate(()=>window.qbPerformance.summary())));
} finally {
 await browser?.close();
 if(server){server.kill('SIGTERM');await new Promise(r=>server.once('exit',r));}
 fs.rmSync(temp,{recursive:true,force:true});
}
