/* Original-pixel lasso rasterization. Loaded before app.js; no bank mutation. */
"use strict";
(function (global) {
  function polygon(points, width, height) {
    const result = [];
    for (const point of points) {
      if (
        !Array.isArray(point) ||
        point.length !== 2 ||
        !point.every(Number.isFinite)
      ) {
        throw new Error("选区坐标无效，请重新圈选。");
      }
      const p = [
        Math.max(0, Math.min(width, Math.round(point[0]))),
        Math.max(0, Math.min(height, Math.round(point[1]))),
      ];
      const previous = result.at(-1);
      if (!previous || p[0] !== previous[0] || p[1] !== previous[1])
        result.push(p);
    }
    if (
      result.length > 1 &&
      result[0][0] === result.at(-1)[0] &&
      result[0][1] === result.at(-1)[1]
    )
      result.pop();
    if (result.length < 3)
      throw new Error("请沿轮廓圈出一块区域，松手自动闭合。");
    const bounds = result.reduce(
      (b, p) => [
        Math.min(b[0], p[0]),
        Math.min(b[1], p[1]),
        Math.max(b[2], p[0]),
        Math.max(b[3], p[1]),
      ],
      [width, height, 0, 0],
    );
    let twiceArea = 0;
    result.forEach((p, i) => {
      const next = result[(i + 1) % result.length];
      twiceArea += p[0] * next[1] - next[0] * p[1];
    });
    if (
      bounds[2] - bounds[0] < 4 ||
      bounds[3] - bounds[1] < 4 ||
      Math.abs(twiceArea) < 32
    ) {
      throw new Error("选区太小或轮廓重叠，请重新圈选。");
    }
    return { points: result, bounds };
  }

  function crc32(bytes) {
    let crc = 0xffffffff;
    for (const byte of bytes) {
      crc ^= byte;
      for (let bit = 0; bit < 8; bit++)
        crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
    return (crc ^ 0xffffffff) >>> 0;
  }

  // Keep provenance in the immutable uploaded asset, without changing the image
  // binding schema or claiming the lasso is a rectangular source crop.
  function withProvenance(raw, trace) {
    const text = JSON.stringify(trace).replace(
      /[\u007f-\uffff]/g,
      (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`,
    );
    const data = new TextEncoder().encode(`qb_source_capture\0${text}`);
    const chunk = new Uint8Array(12 + data.length);
    const view = new DataView(chunk.buffer);
    view.setUint32(0, data.length);
    chunk.set([116, 69, 88, 116], 4); // tEXt
    chunk.set(data, 8);
    view.setUint32(
      chunk.length - 4,
      crc32(chunk.subarray(4, chunk.length - 4)),
    );
    // Canvas PNG starts with the fixed 25-byte IHDR chunk after its signature.
    const input = new Uint8Array(raw),
      output = new Uint8Array(input.length + chunk.length);
    output.set(input.subarray(0, 33));
    output.set(chunk, 33);
    output.set(input.subarray(33), 33 + chunk.length);
    return output;
  }

  async function renderLasso(image, points, provenance) {
    const sourceSize = [image.naturalWidth, image.naturalHeight];
    const selection = polygon(points, ...sourceSize);
    const [left, top, right, bottom] = selection.bounds;
    const canvas = document.createElement("canvas");
    canvas.width = right - left;
    canvas.height = bottom - top;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("无法生成截图，请重试。");
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.beginPath();
    selection.points.forEach(([x, y], i) => {
      if (i === 0) ctx.moveTo(x - left, y - top);
      else ctx.lineTo(x - left, y - top);
    });
    ctx.closePath();
    ctx.clip("evenodd");
    // The source is the existing full-resolution display raster (including the
    // server's high-bit normalization), copied 1:1 with no scaling or JPEG loss.
    ctx.drawImage(image, -left, -top);
    const blob = await new Promise((resolve) =>
      canvas.toBlob(resolve, "image/png"),
    );
    if (!blob) throw new Error("截图编码失败，请重试。");
    const trace = {
      schema: "qb_source_capture/v1",
      mode: "lasso",
      actor: "human",
      ...provenance,
      source_size_px: sourceSize,
      polygon_px: selection.points,
      bounds_px: selection.bounds,
      fill: "white",
      fill_rule: "evenodd",
    };
    return {
      bytes: withProvenance(await blob.arrayBuffer(), trace),
      selection,
    };
  }

  global.SourceCapture = Object.freeze({
    polygon,
    renderLasso,
    withProvenance,
  });
})(globalThis);
