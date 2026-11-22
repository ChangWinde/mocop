// A minimal element model for the view-leaf tests. It implements only what the
// leaves use through their injected factories: tree building, text, classes,
// attributes, dataset, inline custom properties, and click listeners.
export class FakeElement {
  constructor(tag, namespace = null) {
    this.tagName = tag.toUpperCase();
    this.namespace = namespace;
    this.children = [];
    this.attributes = new Map();
    this.dataset = {};
    this.listeners = new Map();
    this.className = "";
    this.hidden = false;
    this.title = "";
    this.type = "";
    this.disabled = false;
    this.ownText = "";
    const properties = new Map();
    this.style = {
      setProperty: (name, value) => properties.set(name, String(value)),
      getPropertyValue: (name) => properties.get(name) ?? "",
    };
    this.classList = {
      add: (...names) => names.forEach((name) => this.toggleClass(name, true)),
      remove: (...names) => names.forEach((name) => this.toggleClass(name, false)),
      toggle: (name, force) => this.toggleClass(name, force),
      contains: (name) => this.className.split(/\s+/).includes(name),
    };
  }

  toggleClass(name, force) {
    const names = new Set(this.className.split(/\s+/).filter(Boolean));
    const present = force ?? !names.has(name);
    if (present) names.add(name);
    else names.delete(name);
    this.className = [...names].join(" ");
    return present;
  }

  set textContent(value) {
    this.children = [];
    this.ownText = String(value);
  }

  get textContent() {
    return this.ownText + this.children.map((child) => (typeof child === "string" ? child : child.textContent)).join("");
  }

  append(...nodes) {
    for (const node of nodes) {
      if (node == null) continue;
      if (node instanceof FakeFragment) this.children.push(...node.children);
      else this.children.push(node);
    }
  }

  replaceChildren(...nodes) {
    this.children = [];
    this.ownText = "";
    this.append(...nodes);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  click() {
    this.listeners.get("click")?.({ preventDefault() {}, stopPropagation() {} });
  }

  // Depth-first search by class name, enough for structural assertions.
  findAll(className) {
    const found = [];
    const visit = (node) => {
      if (typeof node === "string") return;
      if (node.classList.contains(className)) found.push(node);
      node.children.forEach(visit);
    };
    this.children.forEach(visit);
    return found;
  }

  find(className) {
    return this.findAll(className)[0] || null;
  }
}

export class FakeFragment {
  constructor() {
    this.children = [];
  }

  append(...nodes) {
    this.children.push(...nodes.filter((node) => node != null));
  }
}

export function create(tag, className, text) {
  const element = new FakeElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

export function svg(tag, attributes = {}) {
  const element = new FakeElement(tag, "http://www.w3.org/2000/svg");
  Object.entries(attributes).forEach(([name, value]) => element.setAttribute(name, value));
  return element;
}
