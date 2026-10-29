import assert from "node:assert/strict";
import "../src/mocop/static/storage_analysis.js";
const {size,delta}=globalThis.MocopStorageAnalysis;
assert.equal(size(null),"—");assert.equal(size(0),"0.0 GiB");assert.equal(size(2**40),"1.00 TiB");assert.equal(delta(-(2**30)),"−1.0 GiB");assert.equal(delta(null),"首个样本");
class Element {
  constructor(tag) {this.tag=tag;this.children=[];this.dataset={};}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children=children;}
  get childElementCount() {return this.children.length;}
  get text() {return `${this.textContent || ""} ${this.children.map(c=>c.text || "").join(" ")}`;}
}
globalThis.document={createElement:tag=>new Element(tag)};
const container=new Element("div");
globalThis.MocopStorageAnalysis.render({servers:[{host:"gpu-01",priority:-100,knownBytes:0,complete:false,directories:[{path:"/mnt/archive",status:"timeout",bytes:null,children:[{path:"/mnt/archive/project/subfolder",bytes:1024}]}],filesystems:[{mountPoint:"/mnt/objects",capacityVirtual:true,availableBytes:2**48}]}]},container);
assert.match(container.text,/\/mnt\/archive\/project\/subfolder/);
assert.match(container.text,/虚拟容量/);
assert.match(container.text,/尚无完整统计/);
assert.match(container.text,/已完成子目录至少/);
assert.doesNotMatch(container.text,/256\.00 TiB/);
console.log("Storage analysis contracts passed");
