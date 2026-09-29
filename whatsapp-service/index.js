const http = require("http");
const fs = require("fs");
const net = require("net");
const path = require("path");
const QRCode = require("qrcode");
const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");

const port = Number(process.env.PORT || 3000);
const token = process.env.WHATSAPP_SERVICE_TOKEN || "";
const dataPath = process.env.WHATSAPP_SESSION_PATH || "/data/whatsapp";
const chromiumPath = process.env.PUPPETEER_EXECUTABLE_PATH || "/usr/bin/chromium";
const sessionId = "ecopark";
const gatewayIp = String(process.env.WHATSAPP_GATEWAY_IP || "").trim();
const gatewayPort = Number(process.env.WHATSAPP_GATEWAY_PORT || "443");
const gatewayHosts = String(process.env.WHATSAPP_GATEWAY_HOSTS || "web.whatsapp.com")
  .split(",")
  .map((host) => host.trim().toLowerCase())
  .filter((host) => /^[a-z0-9.-]+$/.test(host));
const maxBodyBytes = 30 * 1024 * 1024;

let client = null;
let state = "initializing";
let qrDataUrl = null;
let account = null;
let lastError = null;

function response(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
  });
  res.end(body);
}

function authorized(req) {
  if (!token) return true;
  return req.headers.authorization === `Bearer ${token}`;
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > maxBodyBytes) {
        reject(new Error("Request body is too large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"));
      } catch (_error) {
        reject(new Error("Invalid JSON"));
      }
    });
    req.on("error", reject);
  });
}

function removeStaleChromiumProfileLocks() {
  const profilePath = path.join(dataPath, `session-${sessionId}`);
  for (const name of ["SingletonCookie", "SingletonLock", "SingletonSocket"]) {
    const lockPath = path.join(profilePath, name);
    try {
      // lstat also detects dangling symlinks left after a Docker container exits.
      fs.lstatSync(lockPath);
    } catch (error) {
      if (error.code === "ENOENT") continue;
      throw error;
    }

    // Only Chromium's process-lock artifacts are removed; the WhatsApp profile
    // and its authenticated session remain intact.
    fs.rmSync(lockPath, { force: true, recursive: true });
    console.log(`Removed stale Chromium profile lock: ${name}`);
  }
}

async function startClient() {
  if (gatewayIp && net.isIP(gatewayIp) === 0) {
    state = "error";
    lastError = "WHATSAPP_GATEWAY_IP должен содержать корректный IPv4 или IPv6 адрес";
    console.error(lastError);
    return;
  }
  if (gatewayIp && gatewayHosts.length === 0) {
    state = "error";
    lastError = "WHATSAPP_GATEWAY_HOSTS не содержит корректных имён хостов";
    console.error(lastError);
    return;
  }
  if (gatewayIp && (!Number.isInteger(gatewayPort) || gatewayPort < 1 || gatewayPort > 65535)) {
    state = "error";
    lastError = "WHATSAPP_GATEWAY_PORT должен быть целым числом от 1 до 65535";
    console.error(lastError);
    return;
  }
  try {
    removeStaleChromiumProfileLocks();
  } catch (error) {
    state = "error";
    lastError = `Не удалось удалить блокировку профиля Chromium: ${error.message}`;
    console.error(lastError);
    return;
  }

  const browserArgs = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"];
  if (gatewayIp) {
    const gatewayAddress = net.isIP(gatewayIp) === 6
      ? `[${gatewayIp}]:${gatewayPort}`
      : `${gatewayIp}:${gatewayPort}`;
    const rules = gatewayHosts.map((host) => `MAP ${host}:443 ${gatewayAddress}`).join(",");
    browserArgs.push(`--host-resolver-rules=${rules}`);
  }
  console.log(
    `Starting WhatsApp Web; gateway=${gatewayIp ? `${gatewayIp}:${gatewayPort} for ${gatewayHosts.join(",")}` : "direct"}`,
  );
  state = "initializing";
  qrDataUrl = null;
  account = null;
  lastError = null;
  const nextClient = new Client({
    authStrategy: new LocalAuth({ clientId: sessionId, dataPath }),
    puppeteer: {
      executablePath: chromiumPath,
      headless: true,
      args: browserArgs,
    },
  });
  client = nextClient;

  nextClient.on("qr", async (qr) => {
    if (client !== nextClient) return;
    state = "qr";
    account = null;
    lastError = null;
    qrDataUrl = await QRCode.toDataURL(qr, { width: 360, margin: 1 });
    console.log("WhatsApp QR code received");
  });
  nextClient.on("authenticated", () => {
    if (client !== nextClient) return;
    state = "authenticated";
    qrDataUrl = null;
    console.log("WhatsApp authenticated");
  });
  nextClient.on("ready", () => {
    if (client !== nextClient) return;
    state = "ready";
    qrDataUrl = null;
    const info = nextClient.info || {};
    account = {
      phone: info.wid ? info.wid.user : null,
      name: info.pushname || null,
      platform: info.platform || null,
    };
    console.log(`WhatsApp ready; account=${account.phone || "unknown"}`);
  });
  nextClient.on("auth_failure", (message) => {
    if (client !== nextClient) return;
    state = "auth_failure";
    lastError = String(message || "Authentication failed");
    console.error(`WhatsApp authentication failed: ${lastError}`);
  });
  nextClient.on("disconnected", (reason) => {
    if (client !== nextClient) return;
    state = "disconnected";
    account = null;
    lastError = String(reason || "Disconnected");
    console.error(`WhatsApp disconnected: ${lastError}`);
  });

  try {
    await nextClient.initialize();
  } catch (error) {
    if (client === nextClient) {
      state = "error";
      lastError = error.message;
      console.error("WhatsApp initialization failed", error);
    }
  }
}

async function logout() {
  const oldClient = client;
  client = null;
  state = "logged_out";
  qrDataUrl = null;
  account = null;
  lastError = null;
  if (oldClient) {
    try {
      await oldClient.logout();
    } catch (_error) {
      // A disconnected session can fail to log out; destroy still releases Chromium.
    }
    try {
      await oldClient.destroy();
    } catch (_error) {
      // The process remains available and starts a fresh session below.
    }
  }
  setTimeout(startClient, 500);
}

const server = http.createServer(async (req, res) => {
  if (!authorized(req)) {
    response(res, 401, { error: "Unauthorized" });
    return;
  }

  if (req.method === "GET" && req.url === "/status") {
    response(res, 200, {
      state,
      qr_data_url: qrDataUrl,
      account,
      error: lastError,
      gateway: { enabled: Boolean(gatewayIp), ip: gatewayIp || null, port: gatewayPort, hosts: gatewayHosts },
    });
    return;
  }

  if (req.method === "POST" && req.url === "/logout") {
    try {
      await logout();
      response(res, 200, { ok: true });
    } catch (error) {
      response(res, 500, { error: error.message });
    }
    return;
  }

  if (req.method === "POST" && req.url === "/send") {
    if (state !== "ready" || !client) {
      response(res, 409, { error: "WhatsApp is not ready" });
      return;
    }
    try {
      const body = await readJson(req);
      const phone = String(body.phone || "").replace(/\D/g, "");
      const filename = String(body.filename || "pretenziya.pdf").slice(0, 255);
      const caption = String(body.message || "").slice(0, 1000);
      const pdfBase64 = String(body.pdf_base64 || "");
      if (!/^\d{10,15}$/.test(phone) || !pdfBase64) {
        response(res, 400, { error: "phone and pdf_base64 are required" });
        return;
      }
      const contact = await client.getNumberId(phone);
      if (!contact) {
        response(res, 404, { error: "Номер не зарегистрирован в WhatsApp" });
        return;
      }
      const media = new MessageMedia("application/pdf", pdfBase64, filename);
      const message = await client.sendMessage(contact._serialized, media, {
        caption,
        sendMediaAsDocument: true,
        waitUntilMsgSent: true,
      });
      response(res, 200, {
        ok: true,
        message_id: message.id ? message.id._serialized : null,
      });
    } catch (error) {
      response(res, 500, { error: error.message });
    }
    return;
  }

  response(res, 404, { error: "Not found" });
});

server.listen(port, "0.0.0.0", () => {
  console.log(`WhatsApp service listening on ${port}`);
  startClient();
});

process.on("SIGTERM", async () => {
  try {
    if (client) await client.destroy();
  } finally {
    process.exit(0);
  }
});
