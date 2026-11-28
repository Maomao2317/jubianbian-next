const fs = require("node:fs");
const assert = require("node:assert/strict");
const path = require("node:path");

const root = __dirname;
const source = fs.readFileSync(path.join(root, "js/app.js"), "utf8");
const styles = fs.readFileSync(path.join(root, "css/app.css"), "utf8");
const index = fs.readFileSync(path.join(root, "index.html"), "utf8");

for (const value of [
  "campaignTimer",
  "campaignSlide",
  "double11Offer",
  "campaignPackage",
  "campaignSlidesMarkup",
  "setupCampaignCarousel",
  "campaign-slide",
  "campaign-carousel",
]) {
  assert.doesNotMatch(source, new RegExp(value), `app.js still contains ${value}`);
}

assert.doesNotMatch(styles, /\.campaign-[a-z-]+/, "app.css still contains campaign styles");
assert.doesNotMatch(index, /double11-carousel|campaign-carousel/, "index.html still references campaign assets");
assert.match(index, /css\/app\.css\?v=[^"\s]+/, "stylesheet requires a cache-busting version");
assert.match(index, /js\/app\.js\?v=[^"\s]+/, "application script requires a cache-busting version");

assert.match(source, /class="studio-plan-card"/);
assert.match(source, /data-action="show-pricing"/);
assert.match(source, /cfg\.rechargeOffers/);

console.log("Double11 carousel removal checks passed");
