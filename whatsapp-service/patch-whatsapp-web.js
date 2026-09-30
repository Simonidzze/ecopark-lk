"use strict";

const fs = require("fs");

const utilsPath = require.resolve("whatsapp-web.js/src/util/Injected/Utils");
let source = fs.readFileSync(utilsPath, "utf8");

function replaceOnce(name, original, replacement) {
  if (source.includes(replacement)) return;
  const first = source.indexOf(original);
  if (first === -1 || source.indexOf(original, first + original.length) !== -1) {
    throw new Error(`Cannot apply WhatsApp Web compatibility patch: ${name}`);
  }
  source = source.replace(original, replacement);
}

replaceOnce(
  "sendMessage id lookup",
  ".Msg.get(newMsgKey._serialized);",
  ".Msg.get(window.WWebJS.getMsgKeyId(newMsgKey));",
);

replaceOnce(
  "editMessage id lookup",
  "return window.require('WAWebCollections').Msg.get(msg.id._serialized);",
  "return window.require('WAWebCollections').Msg.get(window.WWebJS.getMsgKeyId(msg.id));",
);

replaceOnce(
  "message id normalization",
  `        if (typeof msg.id.remote === 'object') {
            msg.id = Object.assign({}, msg.id, {
                remote: msg.id.remote._serialized,
            });
        }

        delete msg.pendingAckUpdate;`,
  `        if (typeof msg.id.remote === 'object') {
            msg.id = Object.assign({}, msg.id, {
                remote: msg.id.remote._serialized,
            });
        }
        if (msg.id && msg.id._serialized == null) {
            const serializedId = window.WWebJS.getMsgKeyId(msg.id);
            if (serializedId) {
                msg.id = Object.assign({}, msg.id, {
                    _serialized: serializedId,
                });
            }
        }

        delete msg.pendingAckUpdate;`,
);

replaceOnce(
  "message id compatibility helper",
  "    window.WWebJS.getChats = async () => {",
  `    window.WWebJS.getMsgKeyId = (key) =>
        key?._serialized ?? key?.$1 ?? undefined;

    window.WWebJS.getChats = async () => {`,
);

replaceOnce(
  "media message id collision",
  `        };

        // Bot's won't reply if canonicalUrl is set (linking)`,
  `        };

        delete message.__x_id;

        // Bot's won't reply if canonicalUrl is set (linking)`,
);

const lastReceivedKey = "chat.lastReceivedKey._serialized";
const lastReceivedKeyFallback =
  "window.WWebJS.getMsgKeyId(chat.lastReceivedKey)";
if (!source.includes(lastReceivedKeyFallback)) {
  const occurrences = source.split(lastReceivedKey).length - 1;
  if (occurrences !== 2) {
    throw new Error(
      "Cannot apply WhatsApp Web compatibility patch: last received message id",
    );
  }
  source = source.split(lastReceivedKey).join(lastReceivedKeyFallback);
}

fs.writeFileSync(utilsPath, source);
console.log("Applied WhatsApp Web message ID compatibility patch");
