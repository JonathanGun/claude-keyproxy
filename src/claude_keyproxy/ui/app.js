const WIB = { timeZone: "Asia/Jakarta", hourCycle: "h23" };
const clock = new Intl.DateTimeFormat("en-GB", { ...WIB, hour: "2-digit", minute: "2-digit", second: "2-digit" });
const hm = new Intl.DateTimeFormat("en-GB", { ...WIB, hour: "2-digit", minute: "2-digit" });
const day = new Intl.DateTimeFormat("en-GB", { ...WIB, weekday: "short" });
const date = new Intl.DateTimeFormat("en-CA", WIB);

// Poll every 3 minutes, only while the tab is visible and focused. On refocus, poll at once
// unless the last poll was under a minute ago. Never two polls at a time.
const POLL_MS = 3 * 60 * 1000;
const COOLDOWN_MS = 60 * 1000;

function when(sec) {
  const d = new Date(sec * 1000);
  const today = date.format(d) === date.format(new Date());
  return `${today ? "" : day.format(d) + " "}${hm.format(d)} WIB`;
}

// seconds as 45s, 12 min, 3 h 20 min or 22 days
function duration(s) {
  if (s < 90) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
  return `${Math.round(h / 24)} days`;
}

// the first field shown in the dialog, else its main button
function firstFocusable(dlg) {
  const shown = (el) => el.getClientRects().length > 0;
  return [...dlg.querySelectorAll("input")].find(shown) || dlg.querySelector("footer button:last-child");
}

const API = "/_keyproxy/api";

async function call(method, path, body) {
  const res = await fetch(API + path, {
    method,
    headers: body ? { "content-type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

const blank = (mode = null, name = "") =>
  ({ mode, name, step: "confirm", paste: false, busy: false, token: "", url: "", code: "", id: "", error: "" });

document.addEventListener("alpine:init", () => {
  Alpine.data("keys", () => ({
    keys: [],
    cooldown: null, // null while the proxy is not reachable
    usage: null, // {name: usage | null | "n/a"}, null while the proxy is not reachable
    updated: 0, // when usage last loaded
    msg: "", // the last error outside the dialog
    dlg: blank(), // the add / replace / delete dialog
    expanded: {}, // {name: true} for keys whose usage is open; phones hide it otherwise
    dragFrom: null, // index of the row being dragged, see grab()
    over: null, // index of the row it hovers
    busy: false,
    lastPoll: 0,
    timer: null,
    now: Date.now(), // refreshed every 30 s so the "x min ago" text updates

    init() {
      window.addEventListener("focus", () => this.wake());
      window.addEventListener("blur", () => this.schedule());
      document.addEventListener("visibilitychange", () => this.wake());
      setInterval(() => { this.now = Date.now(); }, 30000);
      void this.poll();
    },

    say(text) {
      this.msg = text;
    },

    async attempt(fn) {
      try { await fn(); } catch (err) { this.say(err.message); }
    },

    // the key the proxy picks next: the first by priority with a token that is not cooling down
    get current() {
      if (!this.cooldown) return null;
      return this.keys.find((k) => k.last4 && !this.cooldown[k.name])?.name ?? null;
    },

    state(k) {
      if (!k.last4) return { text: "key file missing", cls: "missing" };
      const left = this.cooldown?.[k.name];
      if (left) {  // a cooldown also follows a rejected token, which is not a rate limit
        const limited = this.usage?.[k.name]?.status === "rejected";
        return limited ? { text: "rate limited", cls: "limited" } : { text: "cooling down", cls: "cool" };
      }
      if (k.name === this.current) return { text: "in use", cls: "current" };
      return { text: this.cooldown ? "ready" : "", cls: "ready" };
    },

    quota(k) {
      const u = this.usage?.[k.name];
      if (u === "n/a") return { bars: [], note: "API key, no quota." };
      if (!u) return { bars: [], note: "No requests yet." };
      const bars = [["5h", "5-hour"], ["7d", "Weekly"]].map(([win, label]) => {
        const w = u[win];
        if (!w) return { label, pct: "?", reset: "" };
        if (w.reset * 1000 < Date.now()) {
          return { label, pct: "0%", reset: `reset ${when(w.reset)}` };
        }
        const n = Math.round(w.utilization * 100);
        return { label, n, pct: `${n}%`, reset: `resets ${when(w.reset)}` };
      });
      // a response with a status but no 5-hour or weekly window: the key has no subscription limits
      if (!u["5h"] && !u["7d"]) return { bars: [], note: "Not a subscription key" };
      return { bars, note: "" };
    },

    // the chips after a key's name: its state, Anthropic's warning, and the time until its limit resets
    chips(k) {
      const out = [this.state(k)];
      if (this.usage?.[k.name]?.status === "allowed_warning") out.push({ text: "near the limit", cls: "near" });
      const left = this.cooldown?.[k.name] || this.resetIn(k); // a cooling key waits for its cooldown instead
      if (left) out.push({ time: duration(left), text: " until reset", cls: "" });
      return out.filter((c) => c.text);
    },

    // seconds until the key's 5-hour window resets, 0 when unknown or already past
    resetIn(k) {
      const w = this.usage?.[k.name]?.["5h"];
      return w ? Math.max(0, Math.round(w.reset - this.now / 1000)) : 0;
    },

    // "5h 42% · 7d 61%", beside the name on phones while the usage is folded
    mini(k) {
      return this.quota(k).bars.map((b, i) => `${["5h", "7d"][i]} ${b.pct}`).join(" · ");
    },

    openDialog(mode, name = "") {
      this.dlg = blank(mode, name);
      this.$refs.dlg.showModal();
      this.focusDialog();
    },

    // the first field shown, else the main button; the dialog itself would pick the close button.
    // x-show reveals elements on the next animation frame, so wait for that.
    focusDialog() {
      this.$nextTick(() => requestAnimationFrame(() => firstFocusable(this.$refs.dlg).focus()));
    },

    get dlgTitle() {
      const { mode, name } = this.dlg;
      return { add: "Add a key", replace: `New token for ${name}`, delete: `Delete ${name}?`,
               probe: "Check usage of all keys?", reset: "Reset cache?" }[mode] || "";
    },

    get dlgText() {
      return {
        delete: "Removes the key and its token file.",
        reset: "Resets the cooling down status and the usage, so every key is ready.",
        probe: "Sends a 1-token request with every key to read its usage.",
      }[this.dlg.mode] || "";
    },

    // the replaced key's token, masked but for its last 4 characters
    get dlgToken() {
      const last4 = this.keys.find((k) => k.name === this.dlg.name)?.last4;
      return last4 ? "********" + last4 : "";
    },

    // the token file opens in the user's text editor; the token never comes through the API
    async openFile() {
      try {
        await call("POST", `/keys/${encodeURIComponent(this.dlg.name)}/open`, {});
      } catch (err) {
        this.dlg.error = err.message;
      }
    },

    get signing() {
      return this.dlg.mode === "add" || this.dlg.mode === "replace";
    },

    get dlgAction() {
      const d = this.dlg;
      if (d.mode === "delete") return "Delete";
      if (d.mode === "reset") return "Reset";
      if (d.mode === "probe") return "Check usage";
      return d.paste || d.step === "code" ? "Save key" : "Sign in with Claude";
    },

    // one form for every step: delete, paste a token, start the sign-in, or send its code
    async submit() {
      const d = this.dlg;
      d.error = "";
      d.busy = true;
      try {
        if (d.mode === "delete") await this.remove();
        else if (d.mode === "reset") await this.reset();
        else if (d.mode === "probe") await this.checkUsage();
        else if (d.paste) await this.savePasted();
        else if (d.step === "confirm") await this.start();
        else await this.finish();
      } catch (err) {
        d.error = err.message;
      } finally {
        d.busy = false;
      }
    },

    // sign in with Claude: `claude setup-token` runs on this Mac, the sign-in link comes back
    async start() {
      const d = this.dlg;
      const r = await call("POST", "/setup/start", { name: d.name, replace: d.mode === "replace" });
      if (!this.$refs.dlg.open) return call("POST", "/setup/cancel", { id: r.id }); // closed while it started
      d.id = r.id;
      d.url = r.url;
      d.step = "code";
      this.focusDialog();
    },

    // the code from the sign-in page goes to `claude`, which returns the token, saved on this Mac
    async finish() {
      const d = this.dlg;
      try {
        this.keys = await call("POST", "/setup/finish", { id: d.id, code: d.code });
      } catch (err) {
        d.code = "";
        d.step = "confirm";
        throw err;
      } finally {
        d.id = ""; // the sign-in is over after one try
      }
      this.$refs.dlg.close();
    },

    async savePasted() {
      const d = this.dlg;
      this.keys = d.mode === "add"
        ? await call("POST", "/keys", { name: d.name, token: d.token })
        : await call("PUT", `/keys/${encodeURIComponent(d.name)}`, { token: d.token });
      this.$refs.dlg.close();
    },

    async remove() {
      this.keys = await call("DELETE", `/keys/${encodeURIComponent(this.dlg.name)}`);
      this.$refs.dlg.close();
    },

    // runs whenever the dialog closes: stop an unfinished sign-in and forget what was typed
    abort() {
      const id = this.dlg.id;
      this.dlg.id = this.dlg.token = this.dlg.code = "";
      if (id) call("POST", "/setup/cancel", { id }).catch(() => {});
    },

    async reset() {
      await call("POST", "/cache/reset", {});
      this.$refs.dlg.close();
      await this.poll();
    },

    async move(from, to, focus) {
      const order = this.keys.map((k) => k.name);
      order.splice(to, 0, ...order.splice(from, 1));
      await this.attempt(async () => {
        this.keys = await call("PUT", "/order", { order });
        if (focus) this.$nextTick(() => this.$root.querySelectorAll(".handle")[to].focus());
      });
    },

    // ArrowUp/ArrowDown on the drag handle is the keyboard way to reorder
    key(e, i) {
      const to = i + ({ ArrowUp: -1, ArrowDown: 1 }[e.key] || 0);
      if (to === i || to < 0 || to >= this.keys.length) return;
      e.preventDefault();
      void this.move(i, to, true);
    },

    // drag a row onto another to take its place: by the handle, or anywhere on the row with a mouse.
    // Pointer events, not HTML5 drag and drop, which phones do not fire.
    grab(e, i) {
      if (e.button !== 0 || e.target.closest(".actions")) return;
      if (e.pointerType !== "mouse" && !e.target.closest(".handle")) return; // a touch elsewhere scrolls
      e.preventDefault();
      this.dragFrom = i;
      const track = (ev) => {
        const to = document.elementFromPoint(ev.clientX, ev.clientY)?.closest("li[data-i]")?.dataset.i;
        this.over = to !== undefined && Number(to) !== i ? Number(to) : null;
      };
      const end = (ev) => {
        for (const [type, fn] of [["pointermove", track], ["pointerup", end], ["pointercancel", end]]) {
          window.removeEventListener(type, fn);
        }
        const to = this.over;
        this.dragFrom = this.over = null;
        if (ev.type === "pointerup" && to !== null) void this.move(i, to);
      };
      window.addEventListener("pointermove", track);
      window.addEventListener("pointerup", end);
      window.addEventListener("pointercancel", end);
    },

    // a 1-token request with every key; an error stays in the dialog
    async checkUsage() {
      try {
        await call("POST", "/usage/probe", {});
      } catch (err) { // a 404 is a proxy started before this page's version
        throw err.message === "not found"
          ? new Error("The running proxy is older than this page. Restart it with keyproxy-up --restart.") : err;
      }
      this.$refs.dlg.close();
      await this.poll(); // a probe can cool a key down too
    },

    async load() {
      try {
        [this.keys, this.cooldown, this.usage] = await Promise.all(
          ["/keys", "/cache", "/usage"].map((path) => call("GET", path)));
        if (this.msg.startsWith("Proxy not reachable")) this.say("");
        this.updated = this.now = Date.now();
      } catch (err) {
        this.say(`Proxy not reachable: ${err.message}. Start it with keyproxy-up.`);
      }
    },

    get updatedText() {
      if (this.busy) return "Updating…";
      if (!this.updated) return "Not updated yet";
      const ago = Math.floor((this.now - this.updated) / 60000); // reads now, so it recomputes every 30 s
      const since = ago > 0 ? ago + " min ago" : "just now";
      return `Updated ${clock.format(this.updated)} WIB, ${since}`;
    },

    active() {
      return document.visibilityState === "visible" && document.hasFocus();
    },

    schedule() {
      clearTimeout(this.timer);
      this.timer = this.active() ? setTimeout(() => void this.poll(), Math.max(0, this.lastPoll + POLL_MS - Date.now())) : null;
    },

    async poll() {
      if (this.busy) return;
      this.busy = true;
      try {
        await this.load();
      } finally {
        this.lastPoll = Date.now();
        this.busy = false;
        this.schedule();
      }
    },

    wake() {
      if (this.active() && Date.now() - this.lastPoll >= COOLDOWN_MS) void this.poll();
      else this.schedule();
    },
  }));
});
