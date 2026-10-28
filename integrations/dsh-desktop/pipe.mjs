import net from "node:net";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { MAX_FRAME, PROTOCOL } from "./bridge.mjs";

export function servePipe(path, dispatch, token = randomBytes(32).toString("hex")) {
  const sockets = new Set();
  const server = net.createServer(socket => {
    sockets.add(socket);
    socket.setTimeout(15000, () => socket.destroy());
    socket.on("error", () => {});
    socket.on("close", () => sockets.delete(socket));
    let data = Buffer.alloc(0);
    let handling = false;
    socket.on("data", async chunk => {
      if (handling) return socket.destroy();
      data = Buffer.concat([data, chunk]);
      if (data.length > MAX_FRAME + 4) return socket.destroy();
      if (data.length < 4) return;
      const length = data.readUInt32BE(0);
      if (length > MAX_FRAME) return socket.destroy();
      if (data.length < length + 4) return;
      handling = true;
      let response;
      try {
        if (data.length !== length + 4) throw new Error("extra frame");
        const input = JSON.parse(data.subarray(4).toString("utf8"));
        const offered = Buffer.from(String(input.token ?? ""));
        const expected = Buffer.from(token);
        if (offered.length !== expected.length || !timingSafeEqual(offered, expected))
          return socket.destroy();
        if (input.protocol !== PROTOCOL) throw Object.assign(new Error("桥接协议不兼容"), { code: "protocol_incompatible" });
        response = { ok: true, value: await dispatch(input.method, input.params ?? {}) };
      } catch (error) {
        response = { ok: false, error: { code: error.code ?? "bridge_error",
          message: error.code ? error.message : "桥接操作失败，请检查桌面插件状态" } };
      }
      const body = Buffer.from(JSON.stringify(response));
      if (body.length > MAX_FRAME) return socket.destroy();
      const header = Buffer.alloc(4);
      header.writeUInt32BE(body.length);
      socket.end(Buffer.concat([header, body]));
    });
  });
  return {
    token, server,
    listen: () => new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(path, () => { server.removeListener("error", reject); resolve(); });
    }),
    close: () => {
      for (const socket of sockets) socket.destroy();
      return new Promise(resolve => server.close(resolve));
    },
  };
}
