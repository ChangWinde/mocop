import assert from "node:assert/strict";
import "../src/mocop/static/storage_analysis.js";
const {size,delta,usage,directoryRows,render}=globalThis.MocopStorageAnalysis;
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
assert.equal(rows[2].path,nas.mountPoint);
assert.equal(rows[2].fs,nas);
assert.equal(rows[3].fs,object);
assert.equal(usage(directories[0]).text,"1.0 GiB");
assert.equal(usage(directories[1]).text,"≥ 4.0 GiB");
assert.equal(usage({status:"timeout",children:[]}).text,"待统计");
class Element {
  constructor(tag) {this.tag=tag;this.children=[];this.dataset={};}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children=children;}
  get childElementCount() {return this.children.length;}
  get text() {return `${this.textContent || ""} ${this.children.map(c=>c.text || "").join(" ")}`;}
}
globalThis.document={createElement:tag=>new Element(tag)};
const container=new Element("div");render({servers:[server]},container);
assert.match(container.text,/完整目录路径.*总容量.*剩余容量.*目录占用/);
assert.match(container.text,/\/mnt\/archive\/share/);
assert.match(container.text,/额度未知/);
assert.doesNotMatch(container.text,/256\.00 TiB/);
const find=(e,tag)=>e.tag===tag?[e]:e.children.flatMap(c=>find(c,tag));
assert.equal(find(container,"details").some(e=>e.open),false,"diagnostics must start collapsed");
assert.equal(find(container,"table").length,1);
assert.equal(find(container,"tbody")[0].children.length,4);
console.log("Storage analysis contracts passed");
