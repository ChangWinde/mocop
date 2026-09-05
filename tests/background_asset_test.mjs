import assert from "node:assert/strict";

await import("../src/mocop/static/background-asset.js");

const { create, isAnimatedImage } = globalThis.MocopBackgroundAsset;
const MIB = 1024 * 1024;

const ascii = (text) => [...text].map((character) => character.charCodeAt(0));
const be32 = (value) => [value >>> 24, (value >>> 16) & 0xff, (value >>> 8) & 0xff, value & 0xff];
const le32 = (value) => [value & 0xff, (value >>> 8) & 0xff, (value >>> 16) & 0xff, value >>> 24];
const blobOf = (bytes, type) => new Blob([new Uint8Array(bytes)], { type });

const PNG_SIGNATURE = [0x89, ...ascii("PNG"), 0x0d, 0x0a, 0x1a, 0x0a];
const pngChunk = (kind, payload = []) => [...be32(payload.length), ...ascii(kind), ...payload, 0, 0, 0, 0];
const webp = (chunks) => [...ascii("RIFF"), ...le32(4 + chunks.length), ...ascii("WEBP"), ...chunks];
const webpChunk = (kind, size) => [...ascii(kind), ...le32(size), ...new Array(size + (size % 2)).fill(0)];
const avif = (brand) => [...be32(24), ...ascii("ftyp"), ...ascii(brand), ...be32(0), ...ascii("mif1"), ...ascii(brand)];

{
  // Container sniffing decides animation from the bytes, never the label.
  assert.equal(await isAnimatedImage(blobOf([0xff, 0xd8, 0xff, 0xe0], "image/jpeg")), false);
  assert.equal(
    await isAnimatedImage(blobOf([...PNG_SIGNATURE, ...pngChunk("IHDR", new Array(13).fill(0)), ...pngChunk("IEND")], "image/png")),
    false,
  );
  assert.equal(
    await isAnimatedImage(blobOf([...PNG_SIGNATURE, ...pngChunk("IHDR", new Array(13).fill(0)), ...pngChunk("acTL", new Array(8).fill(0))], "image/png")),
    true,
  );
  assert.equal(await isAnimatedImage(blobOf(webp([...webpChunk("VP8 ", 4)]), "image/webp")), false);
  assert.equal(await isAnimatedImage(blobOf(webp([...webpChunk("VP8X", 10), ...webpChunk("ANIM", 6)]), "image/webp")), true);
  assert.equal(await isAnimatedImage(blobOf(avif("avif"), "image/avif")), false);
  assert.equal(await isAnimatedImage(blobOf(avif("avis"), "image/avif")), true);
  // A truncated PNG chunk length cannot walk past the buffer.
  assert.equal(
    await isAnimatedImage(blobOf([...PNG_SIGNATURE, ...be32(0xffffff), ...ascii("IDAT")], "image/png")),
    false,
  );
  for (const [label, bytes, type] of [
    ["jpeg without SOI", [0x00, 0x00, 0x00], "image/jpeg"],
    ["png signature spoof", [...ascii("GIF89a"), 0, 0], "image/png"],
    ["webp without RIFF", [...ascii("RIFX"), 0, 0, 0, 0, ...ascii("WEBP")], "image/webp"],
    ["avif without brand", [...be32(24), ...ascii("ftyp"), ...ascii("mif1"), ...be32(0), ...ascii("mif1"), ...ascii("heic")], "image/avif"],
  ]) {
    await assert.rejects(isAnimatedImage(blobOf(bytes, type)), /图片内容与文件格式不匹配/, label);
  }
}

// A fake browser: bitmaps report the dimensions the test chooses, and the
// canvas encodes to a well-formed static WebP whose size follows the pixel
// count and quality so the bisection and shrink loop can be observed
// deterministically.
// Reports the encoder's byte count without allocating it: the body is one
// well-formed static WebP header, so re-validation of the result still passes.
class SizedWebp extends Blob {
  constructor(size) {
    super([new Uint8Array(webp([...ascii("VP8 "), ...le32(0)]))], { type: "image/webp" });
    this.reportedSize = size;
  }

  get size() {
    return this.reportedSize;
  }
}

const webpOfSize = (size) => new SizedWebp(size);

function fakeBrowser({ width, height, bytesPerPixel }) {
  const encodes = [];
  const canvas = {
    width: 0,
    height: 0,
    getContext: () => ({ drawImage() {} }),
    toBlob(callback, type, quality) {
      encodes.push({ width: canvas.width, height: canvas.height, quality });
      assert.equal(type, "image/webp");
      callback(webpOfSize(Math.round(canvas.width * canvas.height * bytesPerPixel * quality)));
    },
  };
  const bitmaps = [];
  return {
    encodes,
    bitmaps,
    assets: create({
      indexedDB: null,
      createImageBitmap: async () => {
        const bitmap = { width, height, closed: false, close() { this.closed = true; } };
        bitmaps.push(bitmap);
        return bitmap;
      },
      createCanvas: () => canvas,
    }),
  };
}

const jpeg = (size) => new Blob([new Uint8Array([0xff, 0xd8, 0xff, ...new Array(size - 3).fill(0)])], { type: "image/jpeg" });

{
  // Validation order: type, emptiness, size cap, animation, decode, dimensions.
  const { assets, bitmaps } = fakeBrowser({ width: 1920, height: 1080, bytesPerPixel: 1 });
  await assert.rejects(assets.validate("not a blob"), /仅支持 PNG、JPEG、WebP 或 AVIF 图片/);
  await assert.rejects(assets.validate(new Blob([new Uint8Array(4)], { type: "image/gif" })), /仅支持/);
  await assert.rejects(assets.validate(new Blob([], { type: "image/png" })), /不能为空/);
  await assert.rejects(assets.validate(jpeg(8 * MIB + 1)), /不能超过 8 MiB/);
  await assert.rejects(assets.validate(jpeg(32 * MIB + 1), 32 * MIB), /不能超过 32 MiB/);
  assert.deepEqual(await assets.validate(jpeg(64)), { width: 1920, height: 1080 });
  assert.ok(bitmaps.every((bitmap) => bitmap.closed), "decoded bitmaps are released");

  const broken = fakeBrowser({ width: 0, height: 0, bytesPerPixel: 1 });
  broken.assets = create({
    indexedDB: null,
    createImageBitmap: async () => { throw new Error("decode failed"); },
    createCanvas: () => null,
  });
  await assert.rejects(broken.assets.validate(jpeg(64)), /已损坏或当前浏览器不支持/);
  const huge = fakeBrowser({ width: 9000, height: 100, bytesPerPixel: 1 });
  await assert.rejects(huge.assets.validate(jpeg(64)), /不能超过 8192 像素或 32 百万像素/);
  const dense = fakeBrowser({ width: 8000, height: 8000, bytesPerPixel: 1 });
  await assert.rejects(dense.assets.validate(jpeg(64)), /32 百万像素/);
}

{
  // A source under the storage cap is kept byte for byte.
  const { assets, encodes } = fakeBrowser({ width: 800, height: 600, bytesPerPixel: 1 });
  const source = jpeg(3 * MIB);
  const prepared = await assets.prepare(source);
  assert.equal(prepared.blob, source);
  assert.equal(prepared.compressed, false);
  assert.deepEqual(prepared.dimensions, { width: 800, height: 600 });
  assert.equal(encodes.length, 0);
}

{
  // An oversized source is first scaled to the compressed pixel budget, then
  // encoded at quality 0.9 when that fits.
  const { assets, encodes } = fakeBrowser({ width: 6000, height: 4000, bytesPerPixel: 0.4 });
  const prepared = await assets.prepare(jpeg(20 * MIB));
  assert.equal(prepared.compressed, true);
  assert.equal(prepared.blob.type, "image/webp");
  assert.ok(prepared.blob.size <= 8 * MIB);
  assert.deepEqual(encodes.map((encode) => encode.quality), [0.9]);
  // 6000x4000 exceeds both the 4096 edge and the 12 MP budget; the edge wins.
  assert.deepEqual([encodes[0].width, encodes[0].height], [4096, 2730]);
}

{
  // When 0.9 overshoots but 0.5 fits, four bisection steps pick the best
  // quality under the cap without touching the dimensions.
  const { assets, encodes } = fakeBrowser({ width: 4000, height: 3000, bytesPerPixel: 1 });
  const prepared = await assets.prepare(jpeg(9 * MIB));
  assert.equal(prepared.compressed, true);
  assert.ok(prepared.blob.size <= 8 * MIB);
  const qualities = encodes.map((encode) => encode.quality);
  assert.equal(qualities[0], 0.9);
  assert.equal(qualities[1], 0.5);
  assert.equal(qualities.length, 6);
  assert.ok(encodes.every((encode) => encode.width === 4000 && encode.height === 3000));
  // The chosen blob is the largest candidate that fit.
  const fitting = encodes
    .map((encode) => Math.round(4000 * 3000 * encode.quality))
    .filter((size) => size <= 8 * MIB);
  assert.equal(prepared.blob.size, Math.max(...fitting));
}

{
  // When even quality 0.5 overshoots, the canvas shrinks by the byte ratio
  // (never less than 18 %) and tries again; five failures give up.
  const shrinking = fakeBrowser({ width: 4000, height: 4000, bytesPerPixel: 4 });
  const prepared = await shrinking.assets.prepare(jpeg(30 * MIB));
  assert.equal(prepared.compressed, true);
  assert.ok(prepared.blob.size <= 8 * MIB);
  const widths = [...new Set(shrinking.encodes.map((encode) => encode.width))];
  assert.ok(widths.length >= 2, "at least one shrink step happened");
  for (let index = 1; index < widths.length; index += 1) {
    assert.ok(widths[index] <= Math.floor(widths[index - 1] * 0.82));
  }
  // A single pixel that still overshoots cannot shrink further, so the loop
  // stops instead of spinning.
  const hopeless = fakeBrowser({ width: 4000, height: 4000, bytesPerPixel: 1e9 });
  await assert.rejects(hopeless.assets.prepare(jpeg(30 * MIB)), /无法在安全限制内压缩/);
  assert.deepEqual(
    hopeless.encodes.slice(-2).map((encode) => [encode.width, encode.height]),
    [[1, 1], [1, 1]],
  );
}

{
  // Storage goes through one IndexedDB transaction per call and the database
  // handle is closed afterwards even when the store rejects.
  const log = [];
  const store = { value: undefined };
  function request(work) {
    const pending = {};
    queueMicrotask(() => {
      try {
        pending.result = work();
        pending.onsuccess?.();
        pending.transaction.oncomplete?.();
      } catch (error) {
        pending.transaction.error = error;
        pending.transaction.onerror?.();
      }
    });
    return pending;
  }
  const indexedDB = {
    open() {
      const opening = {};
      queueMicrotask(() => {
        opening.result = {
          objectStoreNames: { contains: () => true },
          close: () => log.push("close"),
          transaction(name, mode) {
            log.push(`${mode}:${name}`);
            const transaction = {
              objectStore: () => ({
                get: () => { const r = request(() => store.value); r.transaction = transaction; return r; },
                put: (value) => { const r = request(() => { store.value = value; return "background"; }); r.transaction = transaction; return r; },
                delete: () => { const r = request(() => { throw new Error("quota"); }); r.transaction = transaction; return r; },
              }),
            };
            return transaction;
          },
        };
        opening.onsuccess?.();
      });
      return opening;
    },
  };
  const assets = create({ indexedDB, createImageBitmap: async () => ({ width: 1, height: 1, close() {} }), createCanvas: () => null });
  const blob = jpeg(16);
  assert.equal(await assets.writeStored(blob), "background");
  assert.equal(await assets.readStored(), blob);
  await assert.rejects(assets.deleteStored(), /quota/);
  assert.deepEqual(log, [
    "readwrite:assets", "close",
    "readonly:assets", "close",
    "readwrite:assets", "close",
  ]);
  assert.equal(assets.MAX_BYTES, 8 * MIB);
}

console.log("background-asset contract ok");
