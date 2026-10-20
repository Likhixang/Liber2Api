'use strict';
// No browser, npm packages, subprocesses, or network: run the actual inline scripts
// against a deliberately small in-memory DOM and explicit fetch/clipboard fakes.
const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const html = fs.readFileSync(path.join(__dirname, '../app/templates/index.html'), 'utf8');
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const renderedInput = process.env.WEBUI_RENDERED_PAGES_FILE ? fs.readFileSync(process.env.WEBUI_RENDERED_PAGES_FILE, 'utf8') : '';
const renderedPages = renderedInput ? JSON.parse(renderedInput) : null;
const decode = s => s.replace(/&quot;/g, '"').replace(/&#39;|&#x27;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
class Element {
  constructor(tag = 'div', doc) {
    this.tagName = tag.toUpperCase(); this.doc = doc; this.attrs = {}; this.children = [];
    this.listeners = {}; this.style = {}; this.value = ''; this.checked = false; this.disabled = false;
    this._text = ''; this._html = '';
    this.classList = {
      add: (...xs) => { const s = new Set(this.className.split(/\s+/)); xs.forEach(x => s.add(x)); this.className = [...s].join(' '); },
      remove: (...xs) => { this.className = this.className.split(/\s+/).filter(x => !xs.includes(x)).join(' '); },
      contains: x => this.className.split(/\s+/).includes(x),
      toggle: x => this.classList.contains(x) ? this.classList.remove(x) : this.classList.add(x),
    };
  }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = v; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'value') this.value = String(v); }
  getAttribute(k) { return Object.hasOwn(this.attrs, k) ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  get dataset() { return Object.fromEntries(Object.entries(this.attrs).filter(([k]) => k.startsWith('data-')).map(([k,v]) => [k.slice(5).replace(/-([a-z])/g, (_,c) => c.toUpperCase()), v])); }
  appendChild(n) { n.parentElement = this; this.children.push(n); return n; }
  remove() { if(this.parentElement) this.parentElement.children = this.parentElement.children.filter(n => n !== this); this.parentElement = null; }
  contains(n) { return n === this || this.children.some(c => c.contains(n)); }
  focus() { this.doc.activeElement = this; }
  addEventListener(k, f) { (this.listeners[k] ||= []).push(f); }
  dispatchEvent(e) { e.target ||= this; e.currentTarget = this; for (const f of this.listeners[e.type] || []) f(e); if(e.bubbles !== false && this.parentElement) this.parentElement.dispatchEvent(e); }
  click() { this.dispatchEvent({type:'click', preventDefault(){}, stopPropagation(){this.bubbles=false;}}); }
  matches(s) {
    if(s.includes(',')) return s.split(',').some(x => this.matches(x.trim()));
    if(s.startsWith('#')) return this.getAttribute('id') === s.slice(1);
    if(s.startsWith('.')) return this.classList.contains(s.slice(1));
    const a = s.match(/^(\w+)?\[([^\]=]+)(?:="([^"]*)")?\]$/);
    if(a) return (!a[1] || this.tagName === a[1].toUpperCase()) && this.getAttribute(a[2]) !== null && (a[3] === undefined || this.getAttribute(a[2]) === a[3]);
    return this.tagName === s.toUpperCase();
  }
  closest(s) { return this.matches(s) ? this : this.parentElement?.closest(s) || null; }
  querySelectorAll(s) { return this.children.flatMap(n => [...(n.matches(s) ? [n] : []), ...n.querySelectorAll(s)]); }
  querySelector(s) { return this.querySelectorAll(s)[0] || null; }
  get textContent() { return this._text + this.children.map(n => n.textContent).join(''); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get innerText() { return this.textContent; }
  set innerText(v) { this.textContent = v; }
  get innerHTML() { return this._html; }
  set innerHTML(value) {
    this._html = value; this.children = []; this._text = ''; let current = this;
    for(const m of String(value).matchAll(/<!--[\s\S]*?-->|<\/?[a-zA-Z][^>]*>|[^<]+/g)) {
      const v = m[0]; if(v.startsWith('<!--')) continue;
      if(v.startsWith('</')) { current = current.parentElement || this; continue; }
      if(v.startsWith('<')) {
        const tag = v.match(/^<([\w-]+)/)[1]; const n = new Element(tag, this.doc);
        for(const a of v.slice(tag.length + 1, -1).matchAll(/([^\s=\/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) n.setAttribute(a[1], decode(a[2] ?? a[3] ?? a[4] ?? ''));
        current.appendChild(n); if(!['input','br','hr','meta','link','img'].includes(tag) && !v.endsWith('/>')) current = n;
      } else current._text += decode(v);
    }
  }
}
function response(data = {}, status = 200, jsonError = false) {
  return {ok:status >= 200 && status < 300, status, headers:{get:() => 'application/json'}, json:async() => { if(jsonError) throw Error('invalid JSON'); return data; }, text:async() => String(data)};
}
const settle = async() => { for(let i=0;i<12;i++) await Promise.resolve(); };
async function browser(options = {}) {
  const document = new Element('document'); document.doc = document;
  document.documentElement = document.appendChild(new Element('html', document));
  document.body = document.documentElement.appendChild(new Element('body', document));
  document.createElement = tag => new Element(tag, document);
  document.getElementById = id => document.querySelector('#' + id);
  // Parse only page markup; strip Jinja branches deliberately and supply both gates.
  const markup = options.markup || html;
  document.body.innerHTML = markup.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/{%[\s\S]*?%}|{{[\s\S]*?}}/g,'');
  const values = new Map([['liber2api_logged_in_pass','legacy-admin-secret']]);
  const calls = [], copied = [], toasts = [], storageWrites = []; let reloads = 0;
  let reply = options.reply || (url => url === '/api/accounts' ? response({accounts:[],summary:{}}) : url === '/api/tokens' ? response({auth_enabled:true,tokens:[]}) : response({providers:[]}));
  const context = {
    document, console, URL, Uint8Array, crypto: webcrypto,
    localStorage:{getItem:k=>values.get(k) ?? null, setItem:(k,v)=>{values.set(k,v);storageWrites.push([k,v]);}, removeItem:k=>values.delete(k)},
    navigator:{clipboard:{writeText:async text => copied.push(text)}},
    fetch:async(url,init={})=>{ calls.push({url,init}); return reply(url,init); },
    requestAnimationFrame:f=>f(), setTimeout:()=>1, clearTimeout(){}, setInterval:()=>1, clearInterval(){}, confirm:()=>true,
    tailwind:{}, matchMedia:()=>({matches:false,addEventListener(){}}),
    location:{origin:'https://offline.invalid',reload:()=>reloads++},
  };
  context.window = context;
  if(!options.noIcons) context.lucide = {createIcons(){}};
  vm.createContext(context);
  for(const s of scripts) vm.runInContext(s,context,{filename:'index.html'});
  await settle();
  context.showToast = (message,type='info') => toasts.push({message,type});
  return {context,document,values,calls,copied,toasts,storageWrites,el:id=>document.getElementById(id),reloads:()=>reloads,reply:f=>{reply=f;}};
}

test('legacy password is erased at boot without automatic login', async() => {
  const b = await browser(); assert.equal(b.values.has('liber2api_logged_in_pass'),false);
  assert.equal(b.calls.some(c=>c.url === '/api/admin/login'),false);
});
test('successful login never persists password and clears input', async() => {
  const b = await browser(); b.el('admin-pass-input').value = 'fresh-admin-secret'; const before = b.reloads();
  await b.context.handleAdminLogin({preventDefault(){}});
  assert.equal(b.reloads(),before+1); assert.equal(b.el('admin-pass-input').value,'');
  assert.equal(b.storageWrites.some(([k,v])=>k.includes('pass') || v === 'fresh-admin-secret'),false);
});
test('logout reloads only on success and shows HTTP/network failures', async() => {
  const b = await browser(); const before = b.reloads(); await b.context.handleAdminLogout();
  assert.equal(b.reloads(),before+1); assert.equal(b.values.has('liber2api_logged_in_pass'),false);
  b.reply(()=>response({detail:'denied logout'},403)); await b.context.handleAdminLogout();
  assert.equal(b.reloads(),before+1); assert.match(b.toasts.at(-1).message,/denied logout/); assert.equal(b.toasts.at(-1).type,'error');
});
test('token data is never executable and copying preserves exact bytes', async() => {
  const b = await browser(); const raw = "sk-');globalThis.__injected=1;//\\\n&quot;&<x>\"";
  b.reply(()=>response({auth_enabled:true,tokens:[{id:7,name:'<img onerror=x>',token:raw,status:'active'}]}));
  await b.context.loadTokens(); const buttons = b.el('token-table-body').querySelectorAll('button');
  assert.ok(buttons.length >= 3); for(const btn of buttons) assert.equal(btn.getAttribute('onclick'),null);
  buttons[0].click(); await settle(); assert.equal(b.copied.at(-1),raw); assert.equal(b.context.__injected,undefined);
});
test('provider data uses inert attributes and existing tests send only saved ID', async() => {
  const b = await browser(); const evil = "');globalThis.__injected=1;//\\\n&quot;<x>\"";
  b.reply(()=>response({providers:[{id:9,name:evil,provider_type:'custom_http',api_url:'https://offline.invalid/'+evil,create_url:evil,domain:evil,priority:0,status:'active'},{id:10,name:evil,provider_type:'neighbours',api_url:evil,priority:0,status:'active'}]}));
  await b.context.loadMailProviders();
  for(const id of ['custom-mail-providers-tbody','builtin-providers-list']) for(const btn of b.el(id).querySelectorAll('button')) assert.equal(btn.getAttribute('onclick'),null);
  b.calls.length = 0; b.reply(()=>response({success:true,message:'saved private provider passed'}));
  b.el('custom-mail-providers-tbody').querySelector('button').click(); await settle();
  assert.equal(b.calls[0].url,'/api/mail-providers/9/test'); assert.equal(b.calls[0].init.method,'POST'); assert.equal(b.calls[0].init.body,undefined);
  assert.equal(b.context.__injected,undefined);
});
test('dashboard clears stale raw tokens when none active or auth disabled', async() => {
  const b = await browser();
  for(const data of [{auth_enabled:true,tokens:[{id:1,name:'disabled',token:'inactive-secret',status:'disabled'}]},{auth_enabled:true,tokens:[]},{auth_enabled:false,tokens:[]}]) {
    b.el('api-key-text').setAttribute('data-raw','stale-secret'); b.reply(()=>response(data)); await b.context.loadTokens();
    assert.ok(!b.el('api-key-text').getAttribute('data-raw')); assert.doesNotMatch(b.el('api-key-text').innerText,/inactive-secret|stale-secret/);
  }
});
test('random key generation requires browser cryptographic randomness', async() => {
  const b = await browser(); let used = false; b.context.crypto = {getRandomValues: a=>{used=true; return webcrypto.getRandomValues(a);}};
  vm.runInContext('Math.random = () => { throw new Error("insecure random"); }',b.context);
  b.context.generateRandomTokenInput(); assert.equal(used,true); assert.match(b.el('new-token-val').value,/^sk-[A-Za-z0-9_-]{32,}$/);
});
test('provider priority zero survives JSON submission', async() => {
  const b = await browser(); b.el('new-mail-name').value='zero'; b.el('new-mail-type').value='custom_http'; b.el('new-mail-api-url').value='https://offline.invalid/inbox'; b.el('new-mail-priority').value='0';
  await b.context.createMailProvider(); const call = b.calls.find(c=>c.url==='/api/mail-providers'&&c.init.method==='POST'); assert.equal(JSON.parse(call.init.body).priority,0);
});
test('all account and token mutations visibly reject non-2xx', async() => {
  const b = await browser(); b.reply(()=>response({detail:'operation rejected'},400));
  for(const [name,args] of [['testAccount',[1]],['refreshAccount',[1]],['resetAccount',[1]],['deleteAccount',[1]],['toggleTokenStatus',[1,'active']],['deleteToken',[1]],['testNewMailProvider',[]],['testExistingMailProvider',[1,'private']]]) {
    b.toasts.length=0; await b.context[name](...args);
    assert.ok(b.toasts.some(t=>t.type==='error'&&/operation rejected/.test(t.message)),name); assert.equal(b.toasts.some(t=>t.type==='success'),false,name);
  }
});
test('settings/list load failures are visible including non-JSON HTTP errors', async() => {
  const b = await browser(); b.reply(()=>response('gateway offline',502,true));
  for(const name of ['loadAccounts','loadTokens','loadMailProviders','loadProxySettings']) {
    b.toasts.length=0; await b.context[name](); assert.ok(b.toasts.some(t=>t.type==='error'&&/502/.test(t.message)),name);
  }
});
test('auth policy failure rolls back, success reads back authoritative state', async() => {
  const b = await browser(); const toggle=b.el('auth-policy-toggle');
  toggle.checked=false; b.reply(()=>response({detail:'policy denied'},400)); await b.context.toggleAuthPolicy();
  assert.equal(toggle.checked,true); assert.equal(toggle.disabled,false); assert.ok(b.toasts.some(t=>t.type==='error'&&/policy denied/.test(t.message)));
  b.calls.length=0; toggle.checked=false; b.reply(url=>response(url==='/api/tokens'?{auth_enabled:true,tokens:[]}:{status:'ok'})); await b.context.toggleAuthPolicy();
  assert.equal(toggle.checked,true); assert.ok(b.calls.some(c=>c.url==='/api/tokens'));
});
test('account actions do not overwrite an in-progress proxy form', async() => {
  const b=await browser(); b.el('setting-proxy-url').value='unsaved edit'; b.calls.length=0;
  b.reply(url=>response(url==='/api/accounts'?{accounts:[],summary:{}}:{success:true}));
  for(const name of ['testAccount','refreshAccount','resetAccount','deleteAccount']) { await b.context[name](1); await settle(); }
  b.el('code-input').value='123456'; await b.context.handleVerifyCode(); await settle();
  assert.equal(b.calls.some(c=>c.url==='/api/settings/proxy'),false); assert.equal(b.el('setting-proxy-url').value,'unsaved edit');
});
test('missing icon CDN does not prevent startup or mutations',async()=>{
  const b=await browser({noIcons:true}); b.el('admin-pass-input').value='example'; await b.context.handleAdminLogin({preventDefault(){}}); assert.ok(b.reloads()>0);
});
test('dialogs have names, Escape closes and restores focus; fields have accessible names',async()=>{
  const b=await browser(); const trigger=b.el('theme-btn'); trigger.focus(); b.context.openTokenModal(); await settle();
  const modal=b.el('token-modal'); assert.equal(modal.getAttribute('role'),'dialog'); assert.equal(modal.getAttribute('aria-modal'),'true'); assert.ok(modal.getAttribute('aria-labelledby')||modal.getAttribute('aria-label'));
  b.document.dispatchEvent({type:'keydown',key:'Escape',preventDefault(){}}); assert.equal(modal.classList.contains('hidden'),true); assert.equal(b.document.activeElement,trigger);
  for(const el of b.document.querySelectorAll('input,select')) { if(el.getAttribute('type')==='hidden') continue; const id=el.getAttribute('id'); assert.ok(el.getAttribute('aria-label') || b.document.querySelector(`label[for="${id}"]`),id); }
  assert.equal(b.el('toast-container').getAttribute('aria-live'),'polite');
});
test('clipboard permission and missing crypto failures are visible',async()=>{
  const b=await browser(); b.context.navigator.clipboard.writeText=async()=>{throw Error('clipboard denied');};
  await b.context.copyValue('synthetic-only'); assert.ok(b.toasts.some(t=>t.type==='error'&&/clipboard denied/.test(t.message)));
  b.context.crypto=undefined; b.el('new-token-val').value=''; b.context.generateRandomTokenInput();
  assert.equal(b.el('new-token-val').value,''); assert.equal(b.toasts.at(-1).type,'error');
});
test('logout network errors stay visible without reloading',async()=>{
  const b=await browser(); const before=b.reloads(); b.reply(()=>{throw Error('network offline');});
  await b.context.handleAdminLogout(); assert.equal(b.reloads(),before); assert.ok(b.toasts.some(t=>t.type==='error'&&/network offline/.test(t.message)));
});
test('auth policy readback failure rolls back and reports uncertainty',async()=>{
  const b=await browser(); b.el('auth-policy-toggle').checked=false;
  b.reply(url=>url==='/api/tokens'?response({detail:'readback offline'},503):response({status:'ok'}));
  await b.context.toggleAuthPolicy(); assert.equal(b.el('auth-policy-toggle').checked,true); assert.equal(b.el('auth-policy-toggle').disabled,false);
  assert.ok(b.toasts.some(t=>t.type==='error'&&/读回/.test(t.message)));
});
test('dialog tab focus wraps in both directions',async()=>{
  const b=await browser(); b.context.openTokenModal(); await settle();
  const buttons=b.el('token-modal').querySelectorAll('button, input, select');
  buttons.at(-1).focus(); b.document.dispatchEvent({type:'keydown',key:'Tab',preventDefault(){}}); assert.equal(b.document.activeElement,buttons[0]);
  buttons[0].focus(); b.document.dispatchEvent({type:'keydown',key:'Tab',shiftKey:true,preventDefault(){}}); assert.equal(b.document.activeElement,buttons.at(-1));
});
test('Jinja-rendered lock screen never calls management APIs or replays passwords',{skip:!renderedPages},async()=>{
  const b=await browser({markup:renderedPages.locked,noIcons:true}); assert.equal(b.calls.length,0); assert.equal(b.values.has('liber2api_logged_in_pass'),false);
  assert.ok(b.el('admin-pass-input')); assert.equal(b.el('api-key-text'),null);
});
test('Jinja-rendered dashboard loads active keys without embedding fallback secrets',{skip:!renderedPages},async()=>{
  assert.doesNotMatch(renderedPages.open,/disabled-legacy-secret/);
  const b=await browser({markup:renderedPages.open,noIcons:true}); assert.equal(b.el('admin-pass-input'),null);
  assert.ok(b.calls.some(c=>c.url==='/api/tokens')); assert.ok(!b.el('api-key-text').getAttribute('data-raw'));
});
test('account actions bind inert indexes rather than executable IDs',async()=>{
  const b=await browser(); const id='1);globalThis.__injected=1;//';
  b.reply(()=>response({accounts:[{id,email:'sample@example.com',status:'cooldown',cooldown_until:'2999-01-01',total_requests:0,failed_requests:0}],summary:{}}));
  await b.context.loadAccounts(); const buttons=b.el('accounts-table-body').querySelectorAll('button'); assert.equal(buttons.length,4);
  for(const button of buttons) assert.equal(button.getAttribute('onclick'),null);
  assert.equal(b.context.__injected,undefined);
});
test('legacy password cleanup runs before any third-party script',()=>{
  const cleanup=html.indexOf("localStorage.removeItem('liber2api_logged_in_pass')");
  assert.ok(cleanup>=0 && cleanup<html.indexOf('<script src='));
});
test('UI does not claim encryption at rest',()=>assert.doesNotMatch(html,/加密持久化/));
