const path = require("node:path");

module.exports = require(
  process.env.CLASSROOM_PLAYWRIGHT_MODULE
    || process.env.PLAYWRIGHT_MODULE
    || path.join(__dirname, "../frontend/node_modules/playwright")
);
