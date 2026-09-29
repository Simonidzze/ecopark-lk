FROM node:22-bookworm-slim

ENV NODE_ENV=production \
    PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data/whatsapp \
    && chown -R node:node /data/whatsapp

COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund

COPY --chown=node:node index.js ./

USER node
EXPOSE 3000
CMD ["node", "index.js"]
