// The operator's custom dashboard background, extracted from app.js under the
// ADR-0021 leaf pattern: IndexedDB storage of one binary asset, container
// sniffing that refuses animated or mislabelled files before any decode, the
// size and dimension limits, and the WebP re-encoding that brings an oversized
// source under the storage cap. The caller supplies the browser primitives
// (indexedDB, createImageBitmap, a canvas factory) so the rules run in Node.
(() => {
  "use strict";

  const DATABASE = "mocop.visual-assets.v1";
  const STORE = "assets";
  const KEY = "background";
  const MAX_BYTES = 8 * 1024 * 1024;
  const MAX_SOURCE_BYTES = 32 * 1024 * 1024;
  const MAX_DIMENSION = 8192;
  const MAX_PIXELS = 32_000_000;
  const MAX_COMPRESSED_DIMENSION = 4096;
  const MAX_COMPRESSED_PIXELS = 12_000_000;
  const TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/avif"]);
  const MISMATCH = "图片内容与文件格式不匹配";

  function asciiAt(bytes, offset, value) {
    if (offset < 0 || offset + value.length > bytes.length) return false;
    for (let index = 0; index < value.length; index += 1) {
      if (bytes[offset + index] !== value.charCodeAt(index)) return false;
    }
    return true;
  }

  function uint32BigEndian(bytes, offset) {
    if (offset + 4 > bytes.length) return null;
    return (
      bytes[offset] * 0x1000000
      + bytes[offset + 1] * 0x10000
      + bytes[offset + 2] * 0x100
      + bytes[offset + 3]
    );
  }

  function uint32LittleEndian(bytes, offset) {
    if (offset + 4 > bytes.length) return null;
    return (
      bytes[offset]
      + bytes[offset + 1] * 0x100
      + bytes[offset + 2] * 0x10000
      + bytes[offset + 3] * 0x1000000
    );
  }

  // Container markers are checked before decode so a selected background
  // cannot animate; a body that does not match its declared type is refused.
  async function isAnimatedImage(blob) {
    const bytes = new Uint8Array(await blob.arrayBuffer());
    if (blob.type === "image/jpeg") {
      if (bytes[0] !== 0xff || bytes[1] !== 0xd8 || bytes[2] !== 0xff) {
        throw new Error(MISMATCH);
      }
      return false;
    }
    if (blob.type === "image/png") {
      if (
        bytes[0] !== 0x89
        || !asciiAt(bytes, 1, "PNG")
        || bytes[4] !== 0x0d
        || bytes[5] !== 0x0a
        || bytes[6] !== 0x1a
        || bytes[7] !== 0x0a
      ) {
        throw new Error(MISMATCH);
      }
      for (let offset = 8; offset + 12 <= bytes.length;) {
        const length = uint32BigEndian(bytes, offset);
        if (length == null || length > bytes.length - offset - 12) return false;
        if (asciiAt(bytes, offset + 4, "acTL")) return true;
        if (asciiAt(bytes, offset + 4, "IEND")) return false;
        offset += length + 12;
      }
      return false;
    }
    if (blob.type === "image/webp") {
      if (!asciiAt(bytes, 0, "RIFF") || !asciiAt(bytes, 8, "WEBP")) {
        throw new Error(MISMATCH);
      }
      for (let offset = 12; offset + 8 <= bytes.length;) {
        const length = uint32LittleEndian(bytes, offset + 4);
        if (length == null || length > bytes.length - offset - 8) return false;
        if (asciiAt(bytes, offset, "ANIM") || asciiAt(bytes, offset, "ANMF")) return true;
        offset += 8 + length + (length % 2);
      }
      return false;
    }
    if (blob.type === "image/avif") {
      const boxLength = uint32BigEndian(bytes, 0);
      if (
        boxLength == null
        || (boxLength > 1 && boxLength > bytes.length)
        || !asciiAt(bytes, 4, "ftyp")
      ) {
        throw new Error(MISMATCH);
      }
      const headerLimit = Math.min(
        bytes.length,
        boxLength === 0 ? bytes.length : boxLength === 1 ? 256 : boxLength,
      );
      let hasAvifBrand = false;
      let animated = false;
      for (let offset = 8; offset + 4 <= headerLimit; offset += 1) {
        hasAvifBrand ||= asciiAt(bytes, offset, "avif") || asciiAt(bytes, offset, "avis");
        animated ||= asciiAt(bytes, offset, "avis");
      }
      if (!hasAvifBrand) throw new Error(MISMATCH);
      return animated;
    }
    return false;
  }

  function create({ indexedDB, createImageBitmap, createCanvas }) {
    function openDatabase() {
      return new Promise((resolve, reject) => {
        const request = indexedDB.open(DATABASE, 1);
        let settled = false;
        const finish = (callback, value) => {
          if (settled) {
            if (value && typeof value.close === "function") value.close();
            return;
          }
          settled = true;
          callback(value);
        };
        request.onupgradeneeded = () => {
          if (!request.result.objectStoreNames.contains(STORE)) {
            request.result.createObjectStore(STORE);
          }
        };
        request.onsuccess = () => finish(resolve, request.result);
        request.onerror = () => finish(
          reject,
          request.error || new Error("Unable to open browser storage"),
        );
        request.onblocked = () => finish(reject, new Error("Browser storage is blocked"));
      });
    }

    async function transact(mode, operation) {
      const database = await openDatabase();
      return new Promise((resolve, reject) => {
        let result;
        let settled = false;
        const finish = (callback, value) => {
          if (settled) return;
          settled = true;
          database.close();
          callback(value);
        };
        let transaction;
        let request;
        try {
          transaction = database.transaction(STORE, mode);
          request = operation(transaction.objectStore(STORE));
        } catch (error) {
          finish(reject, error);
          return;
        }
        request.onsuccess = () => { result = request.result; };
        transaction.oncomplete = () => finish(resolve, result);
        transaction.onerror = () => finish(
          reject,
          transaction.error || request.error || new Error("Browser storage transaction failed"),
        );
        transaction.onabort = transaction.onerror;
      });
    }

    async function decodeImage(blob) {
      const bitmap = await createImageBitmap(blob);
      return {
        source: bitmap,
        width: bitmap.width,
        height: bitmap.height,
        release: () => bitmap.close(),
      };
    }

    async function decodeImageSize(blob) {
      const decoded = await decodeImage(blob);
      try {
        return { width: decoded.width, height: decoded.height };
      } finally {
        decoded.release();
      }
    }

    async function validate(blob, maxBytes = MAX_BYTES) {
      if (!(blob instanceof Blob) || !TYPES.has(blob.type)) {
        throw new Error("仅支持 PNG、JPEG、WebP 或 AVIF 图片");
      }
      if (blob.size <= 0) {
        throw new Error("图片内容不能为空");
      }
      if (blob.size > maxBytes) {
        const limit = maxBytes === MAX_BYTES ? 8 : 32;
        throw new Error(`图片大小不能超过 ${limit} MiB`);
      }
      if (await isAnimatedImage(blob)) {
        throw new Error("不支持动态图片，请选择静态背景");
      }
      let dimensions;
      try {
        dimensions = await decodeImageSize(blob);
      } catch (_error) {
        throw new Error("图片已损坏或当前浏览器不支持该格式");
      }
      if (
        dimensions.width <= 0
        || dimensions.height <= 0
        || dimensions.width > MAX_DIMENSION
        || dimensions.height > MAX_DIMENSION
        || dimensions.width * dimensions.height > MAX_PIXELS
      ) {
        throw new Error("图片尺寸不能超过 8192 像素或 32 百万像素");
      }
      return dimensions;
    }

    function canvasToWebp(canvas, quality) {
      return new Promise((resolve, reject) => {
        canvas.toBlob((blob) => {
          if (!(blob instanceof Blob) || blob.size <= 0 || blob.type !== "image/webp") {
            reject(new Error("当前浏览器不支持 WebP 图片压缩"));
            return;
          }
          resolve(blob);
        }, "image/webp", quality);
      });
    }

    // Quality 0.9 when it fits; otherwise bisect between 0.5 and 0.9 for the
    // best quality under the cap, or report that even 0.5 is too large.
    async function encodeWithinLimit(canvas) {
      const highQuality = await canvasToWebp(canvas, 0.9);
      if (highQuality.size <= MAX_BYTES) {
        return { blob: highQuality, smallestSize: highQuality.size };
      }
      const lowQuality = await canvasToWebp(canvas, 0.5);
      if (lowQuality.size > MAX_BYTES) {
        return { blob: null, smallestSize: lowQuality.size };
      }
      let best = lowQuality;
      let lowerQuality = 0.5;
      let upperQuality = 0.9;
      for (let attempt = 0; attempt < 4; attempt += 1) {
        const quality = (lowerQuality + upperQuality) / 2;
        const candidate = await canvasToWebp(canvas, quality);
        if (candidate.size <= MAX_BYTES) {
          best = candidate;
          lowerQuality = quality;
        } else {
          upperQuality = quality;
        }
      }
      return { blob: best, smallestSize: best.size };
    }

    async function compress(blob, dimensions) {
      const decoded = await decodeImage(blob);
      const canvas = createCanvas();
      const initialScale = Math.min(
        1,
        MAX_COMPRESSED_DIMENSION / dimensions.width,
        MAX_COMPRESSED_DIMENSION / dimensions.height,
        Math.sqrt(MAX_COMPRESSED_PIXELS / (dimensions.width * dimensions.height)),
      );
      let width = Math.max(1, Math.floor(dimensions.width * initialScale));
      let height = Math.max(1, Math.floor(dimensions.height * initialScale));
      try {
        for (let attempt = 0; attempt < 5; attempt += 1) {
          canvas.width = width;
          canvas.height = height;
          const context = canvas.getContext("2d");
          if (!context) throw new Error("当前浏览器无法处理这张图片");
          context.imageSmoothingEnabled = true;
          context.imageSmoothingQuality = "high";
          context.drawImage(decoded.source, 0, 0, width, height);
          const encoded = await encodeWithinLimit(canvas);
          if (encoded.blob) return encoded.blob;
          // Shrink by the byte overshoot with a safety margin, never by less
          // than 18 % per attempt, and stop once a step would change nothing.
          const shrink = Math.min(
            0.82,
            Math.sqrt(MAX_BYTES / encoded.smallestSize) * 0.92,
          );
          const nextWidth = Math.max(1, Math.floor(width * shrink));
          const nextHeight = Math.max(1, Math.floor(height * shrink));
          if (nextWidth === width && nextHeight === height) break;
          width = nextWidth;
          height = nextHeight;
        }
      } finally {
        decoded.release();
        canvas.width = 1;
        canvas.height = 1;
      }
      throw new Error("无法在安全限制内压缩这张图片");
    }

    // Validate a selected file against the source cap, then re-encode when it
    // exceeds the storage cap; the result always satisfies the storage rules.
    async function prepare(blob) {
      const dimensions = await validate(blob, MAX_SOURCE_BYTES);
      if (blob.size <= MAX_BYTES) {
        return { blob, dimensions, compressed: false };
      }
      const compressed = await compress(blob, dimensions);
      return {
        blob: compressed,
        dimensions: await validate(compressed),
        compressed: true,
      };
    }

    return Object.freeze({
      MAX_BYTES,
      readStored: () => transact("readonly", (store) => store.get(KEY)),
      writeStored: (blob) => transact("readwrite", (store) => store.put(blob, KEY)),
      deleteStored: () => transact("readwrite", (store) => store.delete(KEY)),
      validate,
      prepare,
    });
  }

  globalThis.MocopBackgroundAsset = Object.freeze({ create, isAnimatedImage });
})();
