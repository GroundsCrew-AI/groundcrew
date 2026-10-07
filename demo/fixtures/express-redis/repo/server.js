const express = require("express");
const { createClient } = require("redis");

const app = express();
const redis = createClient({ url: process.env.REDIS_URL });

app.get("/healthz", (req, res) => res.json({ ok: true }));

app.get("/count", async (req, res) => {
  if (!redis.isOpen) await redis.connect();
  const count = await redis.incr("visits");
  res.json({ count });
});

const port = process.env.PORT || 8080;
app.listen(port, () => console.log(`listening on ${port}`));
