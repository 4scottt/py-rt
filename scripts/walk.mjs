// Headless acceptance walk: a real browser walks the six goals of the
// comparison the way a person does them (docs/PLAN.md §13), in the goals'
// order and with their words, so this side is walked like the two RT
// sides. Steps land with their milestones; a step past WALK_STEPS is not
// run. Fails on any browser console error, page error, or own-origin
// sub-resource answering >= 400.
//
//   WALK_URL         http://localhost:8082 (default)
//   ROOT_PASSWORD    root's password (default password, matching scripts/dev.sh and compose)
//   WALK_GATE_USER / WALK_GATE   basic-auth credentials when walking through an oldbox gate
//   WALK_BROWSER     chrome (default: Chrome channel) | chromium (Playwright's own, for CI)
//   WALK_HEADED=1    show the browser
//   WALK_STEPS       how many steps to run (default: every step written so far)
//   PLAYWRIGHT_DIR   where `playwright` is installed (default: scripts/.walk, then a global resolve)
//
// Steps: 1 sign in · 2 create a queue · (M2) 3 a ticket · 4 reply · 5 comment ·
// (M4) 6 search · 7 a custom field · (M3) 8 a user · 9 a group · 10 rights ·
// (M5) 11 mail · (M2) 12 resolve · 13 logout — the last step always runs.
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
function loadPlaywright() {
  const dirs = [process.env.PLAYWRIGHT_DIR, path.join(here, ".walk"), here].filter(Boolean);
  for (const d of dirs) {
    try { return createRequire(path.join(d, "node_modules", "/"))("playwright"); } catch {}
  }
  return createRequire(import.meta.url)("playwright");
}
const { chromium } = loadPlaywright();

const WRITTEN = 12; // the highest step written so far; raise it as milestones land
const UNWRITTEN = new Set([6, 7, 9, 10, 11]); // steps whose milestone has not landed: skipped
const run = (n) => steps >= n && !UNWRITTEN.has(n);
const url = (process.env.WALK_URL || "http://localhost:8082").replace(/\/$/, "");
const rootPassword = process.env.ROOT_PASSWORD || "password";
const steps = Math.min(Number(process.env.WALK_STEPS || WRITTEN), WRITTEN);
const headed = process.env.WALK_HEADED === "1";
const stamp = new Date().toISOString().slice(11, 19).replace(/:/g, "");
const queue = `Support ${stamp}`;
const subject = "Printer on the third floor is jammed";
const tech = `tech1-${stamp}`;
let ticketUrl = "";
const problems = [];
const note = (s) => console.log(`  ${s}`);

async function expectText(page, text, what) {
  await page.waitForFunction((t) => document.body && document.body.innerText.includes(t), text, { timeout: 30_000 })
    .catch(() => { throw new Error(`${what}: the page does not say "${text}" (at ${page.url()})`); });
}

const launchOpts = { headless: !headed, slowMo: headed ? 250 : 0 };
if ((process.env.WALK_BROWSER || "chrome") === "chrome") launchOpts.channel = "chrome";
const browser = await chromium.launch(launchOpts);
let failed = null;
try {
  const ctxOpts = {};
  if (process.env.WALK_GATE) ctxOpts.httpCredentials = { username: process.env.WALK_GATE_USER || "oldbox", password: process.env.WALK_GATE };
  const ctx = await browser.newContext(ctxOpts);
  const page = await ctx.newPage();
  const own = new URL(url).host;
  page.on("requestfailed", (r) => { if (new URL(r.url()).host === own) problems.push(`${r.failure()?.errorText || "failed"}: ${r.url()}`); });
  page.on("response", (r) => { if (r.status() >= 400 && r.request().resourceType() !== "document" && new URL(r.url()).host === own) problems.push(`${r.status()}: ${r.url()}`); });
  page.on("console", (m) => { if (m.type() === "error") problems.push(`console: ${m.text()}`); });
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));

  // 1. sign in as root: the home page shows the login form to a signed-out
  //    browser (fields user and pass, button Login); once in, "RT at a glance"
  console.log("1. sign in");
  await page.goto(`${url}/`);
  await page.fill('input[name="user"]', "root");
  await page.fill('input[name="pass"]', rootPassword);
  await page.click('button:has-text("Login")');
  await expectText(page, "RT at a glance", "sign in");
  note("RT at a glance");

  // 2. a queue: Admin › Queues › Create, the name, the defaults otherwise
  if (run(2)) {
    console.log("2. create a queue");
    await page.goto(`${url}/admin/queues`);
    await expectText(page, "Queues", "the queues list");
    await page.click('a:has-text("Create")');
    await expectText(page, "Create a queue", "the create form");
    await page.fill('input[name="name"]', queue);
    await page.click('button:has-text("Create")');
    await expectText(page, "Modify a queue", "after creating the queue");
    await expectText(page, queue, "the queue's name on its page");
    await page.goto(`${url}/admin/queues`);
    await expectText(page, queue, "the queue in the list");
    note(`queue "${queue}" created and listed`);
  }

  // 3. a ticket in that queue: Tickets › New ticket in › the queue; the
  //    goal's subject and a sentence of description
  if (run(3)) {
    console.log("3. create a ticket");
    await page.goto(`${url}/`);
    // the menus open on hover, as a person opens them: Tickets, then New ticket in
    await page.hover('#nav .menu-label:has-text("Tickets")');
    await page.hover('#nav .menu-label:has-text("New ticket in")');
    await page.click(`#nav a:has-text("${queue}")`);
    await expectText(page, "Create a ticket", "the create form");
    await page.fill('input[name="subject"]', subject);
    await page.fill('textarea[name="content"]', "The printer by the stairwell shows a paper jam and nobody can clear it.");
    await page.click('button:has-text("Create")');
    await expectText(page, `: ${subject}`, "the ticket page after creating");
    ticketUrl = page.url();
    await expectText(page, "Ticket created", "the Create transaction in the history");
    note(`ticket created at ${ticketUrl}`);
  }

  // 4. reply: Reply, not Comment; one sentence
  if (run(4)) {
    console.log("4. reply");
    await page.goto(ticketUrl);
    await page.click('a:has-text("Reply")');
    await expectText(page, "Update ticket #", "the update page");
    await page.check('input[name="UpdateType"][value="respond"]');
    await page.fill('textarea[name="content"]', "A technician is on the way to the third floor.");
    await page.click('button:has-text("Update Ticket")');
    await expectText(page, "Correspondence added", "the reply in the history");
    note("reply recorded");
  }

  // 5. comment: Comment, not Reply
  if (run(5)) {
    console.log("5. comment");
    await page.goto(ticketUrl);
    await page.click('a:has-text("Comment")');
    await expectText(page, "Update ticket #", "the update page");
    await page.check('input[name="UpdateType"][value="comment"]');
    await page.fill('textarea[name="content"]', "The printer model is unknown; ask the floor manager.");
    await page.click('button:has-text("Update Ticket")');
    await expectText(page, "Comment added", "the comment in the history");
    note("comment recorded");
  }

  // 6–7: search, a custom field — with M4

  // 8. a privileged user: Admin › Users › Create; the name, the real name,
  //    the email, the password left blank, privileged
  if (run(8)) {
    console.log("8. create a user");
    await page.goto(`${url}/admin/users`);
    await expectText(page, "Users", "the users list");
    await page.click('a:has-text("Create")');
    await expectText(page, "Create a user", "the create form");
    await page.fill('input[name="name"]', tech);
    await page.fill('input[name="real_name"]', "Terry Technician");
    await page.fill('input[name="email"]', `${tech}@example.com`);
    await page.check('input[name="privileged"]');
    await page.click('button:has-text("Create")');
    await expectText(page, "Modify a user", "after creating the user");
    await expectText(page, "User created", "the created message");
    note(`user ${tech} created, privileged`);
  }

  // 9–11: a group, rights, mail — with M3 and M5

  // 12. resolve the ticket, last in the workload
  if (run(12)) {
    console.log("12. resolve");
    await page.goto(ticketUrl);
    await page.click('a:has-text("Resolve")');
    await expectText(page, "Update ticket #", "the update page");
    const status = await page.inputValue('select[name="status"]');
    if (status !== "resolved") throw new Error(`resolve: the status select reads ${status}, not resolved`);
    await page.click('button:has-text("Update Ticket")');
    await expectText(page, "Status changed from open to resolved", "the status transaction in the history");
    note("resolved");
  }

  // last. logout: the home page shows the login form again
  console.log("13. logout");
  await page.goto(`${url}/logout`);
  await page.waitForSelector('input[name="user"]', { timeout: 30_000 });
  note("signed out");
} catch (e) {
  failed = e;
} finally {
  await browser.close();
}

if (problems.length) {
  console.error("problems:");
  for (const p of problems) console.error(`  ${p}`);
}
if (failed) {
  console.error(`FAILED: ${failed.message}`);
  process.exit(1);
}
if (problems.length) process.exit(1);
console.log(`walk green (${steps} step${steps === 1 ? "" : "s"} + logout)`);
