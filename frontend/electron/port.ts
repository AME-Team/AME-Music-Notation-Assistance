import { randomBytes } from "node:crypto";
import { createServer } from "node:net";

/** #77: ポートは固定せず、空いているものを動的に取得する。 */
export function getFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (address && typeof address === "object") {
        const port = address.port;
        server.close(() => resolve(port));
      } else {
        reject(new Error("failed to acquire a free port"));
      }
    });
  });
}

/** NFR-10′: 起動時にランダムなローカルトークンを生成する。 */
export function generateToken(): string {
  return randomBytes(32).toString("hex");
}
