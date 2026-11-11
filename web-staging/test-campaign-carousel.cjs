const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const path = require("node:path");

const context = vm.createContext({
  window: { addEventListener() {}, AppAPI: {}, matchMedia: () => ({ matches: false }) },
  document: { addEventListener() {} },
  location: { protocol: "http:" },
  Intl, Date, Map, Set, console, setInterval: () => 1, clearInterval() {},
});
context.window.location = context.location;
vm.runInContext(fs.readFileSync(path.join(__dirname, "js/config.js"), "utf8"), context);
const source = fs.readFileSync(path.join(__dirname, "js/app.js"), "utf8");
const styles = fs.readFileSync(path.join(__dirname, "css/app.css"), "utf8");
vm.runInContext(source.replace("  boot();", "  window.testUI = { double11Offer, campaignSlidesMarkup, state };"), context);

const ui = context.window.testUI;
assert.deepEqual(JSON.parse(JSON.stringify(ui.double11Offer({ price: 39, credits: 500 }))), { price: 39, credits: 500, campaignCredits: 555 });
const html = ui.campaignSlidesMarkup();
assert.match(html, /campaign-carousel.*campaign-overview.*campaign-personal.*campaign-enterprise/s);
assert.match(html, /额外加赠 11% 积分/);
for (const value of ["500", "555", "1,620", "1,802", "5,488", "6,008", "15,360", "16,768", "47,360", "51,584", "80,000", "87,040"]) {
  assert.ok(html.includes(value), `missing campaign value ${value}`);
}
assert.equal((html.match(/data-action="campaign-slide"/g) || []).length, 3);
assert.doesNotMatch(html, /data-action="campaign-prev"|data-action="campaign-next"|class="campaign-arrow"/);
assert.doesNotMatch(html, /联系客服|1元\s*=\s*12\.8|多得积分|原套餐积分/);
assert.match(styles, /\.campaign-package\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/);
assert.match(styles, /\.campaign-package\s*>\s*span\s*\{[^}]*text-decoration:\s*line-through/);
assert.match(source, /},\s*5000\);/);
console.log("Campaign carousel checks passed");
