import assert from "node:assert/strict";
import "../src/mocop/static/storage_analysis.js";
const {size,delta,usage,budget,directoryRows,render}=globalThis.MocopStorageAnalysis;
assert.equal(size(null),"—");assert.equal(size(0),"0.0 GiB");assert.equal(size(2**40),"1.00 TiB");assert.equal(delta(-(2**30)),"−1.0 GiB");assert.equal(delta(null),"首个样本");
const disk={key:"local",mountPoint:"/",totalBytes:2**40,availableBytes:2**30};
const nas={key:"nas",mountPoint:"/mnt/archive/share",totalBytes:2**41,availableBytes:2**39};
const object={key:"object",mountPoint:"/objects",capacityVirtual:true,totalBytes:2**48,availableBytes:2**48};
const directories=[{path:"/home/alice",status:"ok",bytes:2**30,filesystemKey:"local"},{path:"/mnt/archive",status:"partial",children:[{path:"/mnt/archive/share",status:"ok",bytes:2**32}]},{path:"/objects",status:"timeout",children:[{path:"/objects/project",bytes:2**30}]}];
const server={host:"gpu-01",priority:-100,directories,filesystems:[disk,nas,object]};
const rows=directoryRows(server);
assert.equal(rows[0].fs,disk);
assert.equal(rows[1].fs,null,"NAS container must not show OS capacity");
assert.equal(rows[1].nested,1);
assert.equal(rows.length,3,"only configured roots belong in the main table");
assert.equal(rows[2].fs,object);
assert.equal(usage(directories[0]).text,"1.0 GiB");
assert.equal(usage(directories[1]).text,"≥ 4.0 GiB");
assert.equal(usage({status:"timeout",children:[]}).text,"待统计");
assert.equal(usage({status:"unreadable",observedBytes:2**40,children:[{bytes:2**30}]}).text,"≥ 1.00 TiB","retain the whole accessible summary, not just top 32 children");
assert.match(budget({limitBytes:1e11,status:"ok",bytes:120e9}),/超出 100 GB.*20\.0 GB/);
assert.match(budget({limitBytes:1e11,status:"partial",observedBytes:30e9}),/待完整统计/);
assert.doesNotMatch(budget({limitBytes:1e11,status:"partial",observedBytes:30e9}),/未超限/);
assert.match(budget({limitBytes:1e11,status:"ok",bytes:50e9}),/未超限/);
const memory={key:"ram",kind:"memory",mountPoint:"/shared/alice/runtime",totalBytes:2**35,availableBytes:2**34};
const scoped={path:"/shared",usagePath:"/shared/alice",purpose:"临时高速空间，尽量留空",limitBytes:1e11,status:"ok",bytes:120e9,filesystemKey:"local"};
assert.equal(directoryRows({directories:[scoped],filesystems:[disk,memory]})[0].fs,disk,"tmpfs must not hide the actual disk capacity");
class Element {
  constructor(tag) {this.tag=tag;this.children=[];this.dataset={};}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children=children;}
  get childElementCount() {return this.children.length;}
  get text() {return `${this.textContent || ""} ${this.children.map(c=>c.text || "").join(" ")}`;}
}
globalThis.document={createElement:tag=>new Element(tag)};
const container=new Element("div");render({servers:[server]},container);
assert.match(container.text,/完整目录路径.*总容量.*剩余容量.*我占用的/);
assert.match(container.text,/\/mnt\/archive\/share/);
assert.match(container.text,/额度未知/);
assert.doesNotMatch(container.text,/256\.00 TiB/);
const find=(e,tag)=>e.tag===tag?[e]:e.children.flatMap(c=>find(c,tag));
assert.equal(find(container,"details").some(e=>e.open),false,"server rows and diagnostics must start collapsed");
assert.equal(find(container,"details").filter(e=>e.className === "sa-card").length,1,"each server must be a collapsed details row");
assert.equal(find(container,"table").length,1);
assert.equal(find(container,"tbody")[0].children.length,3);
render({servers:[{...server,directories:[scoped],filesystems:[disk,memory]}]},container);
assert.match(container.text,/临时高速空间，尽量留空/);
assert.match(container.text,/统计范围：\/shared\/alice/);
assert.match(container.text,/超出 100 GB/);
assert.match(container.text,/内存临时盘，不计入磁盘占用/);
console.log("Storage analysis contracts passed");
