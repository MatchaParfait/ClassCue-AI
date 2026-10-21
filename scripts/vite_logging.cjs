// Keep Vite's output bounded in its own Node process.
const fs = require("node:fs");
const path = require("node:path");

const root = process.env.CLASSROOM_LOG_DIR;
if (root) {
  const maxBytes = 2 * 1024 * 1024;
  const backups = 3;
  fs.mkdirSync(root, { recursive: true });
  const secrets = Object.entries(process.env)
    .filter(([name, value]) => /(?:api[_-]?key|secret|token|password)/i.test(name) && value && value.length >= 4)
    .map(([, value]) => value);

  function redact(text) {
    for (const secret of secrets) text = text.split(secret).join("[REDACTED]");
    return text
      .replace(/(Bearer\s+)[A-Za-z0-9._~+\/=-]+/gi, "$1[REDACTED]")
      .replace(/((?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*["']?)[^\s"',;]+/gi, "$1[REDACTED]");
  }

  for (const [stream, suffix] of [[process.stdout, "stdout"], [process.stderr, "stderr"]]) {
    const file = path.join(root, `vite.${suffix}.log`);
    let size = fs.existsSync(file) ? fs.statSync(file).size : 0;
    let pending = "";
    const append = (text) => {
      let data = Buffer.from(redact(text), "utf8");
      while (data.length) {
        if (size >= maxBytes) {
          fs.rmSync(`${file}.${backups}`, { force: true });
          for (let index = backups - 1; index >= 1; index--) {
            if (fs.existsSync(`${file}.${index}`)) fs.renameSync(`${file}.${index}`, `${file}.${index + 1}`);
          }
          if (fs.existsSync(file)) fs.renameSync(file, `${file}.1`);
          size = 0;
        }
        const count = Math.min(maxBytes - size, data.length);
        fs.appendFileSync(file, data.subarray(0, count));
        data = data.subarray(count);
        size += count;
      }
    };
    stream.write = (chunk, encoding, callback) => {
      if (typeof encoding === "function") callback = encoding;
      pending += Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk);
      let newline;
      while ((newline = pending.indexOf("\n")) !== -1) {
        append(pending.slice(0, newline + 1));
        pending = pending.slice(newline + 1);
      }
      while (pending.length >= 16384) {
        pending = redact(pending);
        if (pending.length < 16384) break;
        append(pending.slice(0, 8192));
        pending = pending.slice(8192);
      }
      if (callback) process.nextTick(callback);
      return true;
    };
    process.on("exit", () => {
      if (pending) append(pending);
    });
  }
}
