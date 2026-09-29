const http = require("http");
const QRCode = require("qrcode");
const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");

const port = Number(process.env.PORT || 3000);
const token = process.env.WHATSAPP_SERVICE_TOKEN || "";
const dataPath = process.env.WHATSAPP_SESSION_PATH || "/data/whatsapp";
const chromiumPath = process.env.PUPPETEER_EXECUTABLE_PATH || "/usr/bin/chromium";
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

async function startClient() {
  state = "initializing";
  qrDataUrl = null;
  account = null;
  lastError = null;
  const nextClient = new Client({
    authStrategy: new LocalAuth({ clientId: "ecopark", dataPath }),
    puppeteer: {
      executablePath: chromiumPath,
      headless: true,
      args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
    },
  });
  client = nextClient;

  nextClient.on("qr", async (qr) => {
    if (client !== nextClient) return;
    state = "qr";
    account = null;
    lastError = null;
    qrDataUrl = await QRCode.toDataURL(qr, { width: 360, margin: 1 });
  });
  nextClient.on("authenticated", () => {
    if (client !== nextClient) return;
    state = "authenticated";
    qrDataUrl = null;
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
  });
  nextClient.on("auth_failure", (message) => {
    if (client !== nextClient) return;
    state = "auth_failure";
    lastError = String(message || "Authentication failed");
  });
  nextClient.on("disconnected", (reason) => {
    if (client !== nextClient) return;
    state = "disconnected";
    account = null;
    lastError = String(reason || "Disconnected");
  });

  try {
    await nextClient.initialize();
  } catch (error) {
    if (client === nextClient) {
      state = "error";
      lastError = error.message;
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
    response(res, 200, { state, qr_data_url: qrDataUrl, account, error: lastError });
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
