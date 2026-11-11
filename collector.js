(()=>{
const SCOUT="__SCOUT_ORIGIN__",K="scout_basket";
let b=[];try{b=JSON.parse(localStorage.getItem(K)||"[]")}catch(e){}
const add=c=>{c.name=(c.name||"").trim();if(c.name.length<3||c.name.length>90)return;
 const x=b.find(y=>y.name.toLowerCase()===c.name.toLowerCase());
 if(x){if(c.website)x.website=c.website;if(c.city&&!x.city)x.city=c.city}else b.push(c)};
const cityOf=el=>{for(let n=el,i=0;n&&i<8;n=n.parentElement,i++){const m=(n.innerText||"").match(/United Kingdom,\s*([^\n]+)/);if(m)return m[1].trim()}return""};
const site=[...document.querySelectorAll("a[href]")].find(a=>/website/i.test(a.innerText||"")&&!/europages|visable|wlw\./.test(a.href));
const path=location.pathname;let added=0;const before=b.length;
if(/\/company\//.test(path)){
 let name=document.querySelector("h1")?.innerText||"";
 if(/\/products\//.test(path)){const c=[...document.querySelectorAll('a[href*="/company/"]')].find(a=>!/\/products\//.test(a.href)&&(a.innerText||"").trim().length>2);if(c)name=c.innerText}
 const m=document.body.innerText.match(/United Kingdom,\s*([^\n]+)/);
 add({name,website:site?site.href:"",city:m?m[1].trim():"",country:"United Kingdom",profile:location.href.split("/products/")[0].split("?")[0].split("#")[0]});added=1;
}else{
 const g={};document.querySelectorAll('a[href*="/company/"]').forEach(a=>{if(/\/products\//.test(a.href))return;const u=a.href.split("?")[0].split("#")[0];const t=(a.innerText||"").trim();
  if(!t||/contact|portfolio|view all|products/i.test(t))return;if(!g[u]||t.length>g[u].t.length)g[u]={t,a}});
 Object.entries(g).forEach(([u,v])=>add({name:v.t,city:cityOf(v.a),country:"United Kingdom",profile:u,website:""}));added=b.length-before;
}
try{localStorage.setItem(K,JSON.stringify(b))}catch(e){}
let p=document.getElementById("scout-panel");if(p)p.remove();p=document.createElement("div");p.id="scout-panel";
p.style.cssText="position:fixed;z-index:2147483647;right:16px;bottom:16px;background:#121826;color:#e6e9ef;font:14px system-ui;padding:14px 16px;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.35);max-width:300px";
const w=b.filter(x=>x.website).length;
p.innerHTML='<b style="color:#22c55e">Scout</b> · '+(added?("added "+added+"<br>"):"already in your list<br>")+'<span style="opacity:.8">'+b.length+" collected, "+w+" with website</span>"
+'<div style="margin-top:10px;display:flex;gap:8px"><button id="sc-send" style="background:#22c55e;border:0;color:#fff;padding:7px 12px;border-radius:8px;font-weight:600;cursor:pointer">Send to Scout</button>'
+'<button id="sc-clear" style="background:#232d40;border:0;color:#e6e9ef;padding:7px 10px;border-radius:8px;cursor:pointer">Clear</button><button id="sc-x" style="background:none;border:0;color:#8b95a7;cursor:pointer">✕</button></div>';
document.body.appendChild(p);
p.querySelector("#sc-send").onclick=()=>{window.open(SCOUT+"/#collect="+encodeURIComponent(JSON.stringify(b)),"scout");localStorage.removeItem(K);p.remove()};
p.querySelector("#sc-clear").onclick=()=>{localStorage.removeItem(K);p.remove()};
p.querySelector("#sc-x").onclick=()=>p.remove();
})();
