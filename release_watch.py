#!/usr/bin/env python3
"""release-watch: Telegram alerts about new releases of the node software you run.

    release_watch.py [-c config.toml] [--dry-run]           hourly check
    release_watch.py [-c config.toml] [--dry-run] --daily   24h report

Hourly run:
  * new GitHub/GitLab releases of every [[watch]] source (first run records them silently
    and sends one summary instead of flooding the chat);
  * on-chain upgrade plans of Cosmos SDK nodes ([[plan]]), with a reminder shortly before;
  * new forum posts ([[forum]], Discourse search) and changed doc pages ([[doc]]).
Daily run: a report of the last 24h plus installed versions ([[node]], probed over SSH,
optionally inside Proxmox LXC containers) compared with the latest releases.

Standard library only (Python 3.11+). Secrets come from the environment or env_file:
TG_TOKEN, TG_CHAT and optional GITHUB_TOKEN.
"""
import argparse, base64, difflib, hashlib, json, os, re, subprocess, sys, time, tomllib, \
    urllib.parse, urllib.request, zoneinfo
from datetime import datetime, timezone

UA = {"User-Agent": "release-watch/1.0"}
DEFAULT_IMPORTANT = (r"mandatory|upgrade height|hard[ -]?fork|network upgrade|must upgrade|required upgrade|"
                     r"(consensus|state)[ -]breaking|breaking change")

# Shell helpers available to every [[node]] probe and [[plan]] check.
SH = r"""
sb(){ b=$(systemctl show -p ExecStart --value "$1" | grep -oE 'path=[^ ;]+' | head -1 | cut -d= -f2); shift; "$b" "$@" 2>&1 | head -3; }
cport(){ h=$(systemctl show -p Environment --value "$1" | tr ' ' '\n' | sed -n 's/^DAEMON_HOME=//p'); h=${h:-$2}; sed -n '/^\[rpc\]/,/^\[/p' "$h/config/config.toml" | grep -E '^laddr' | grep -oE ':[0-9]+"' | tr -d ':"'; }
cv(){ curl -s -m10 "localhost:$(cport "$@")/abci_info" | grep -oE '"version":"[^"]*"'; }
w3(){ curl -s -m10 -X POST -H content-type:application/json -d '{"jsonrpc":"2.0","id":1,"method":"web3_clientVersion"}' "localhost:$1"; }
img(){ docker inspect -f '{{.Config.Image}}' "$1"; }
plan(){ q="localhost:$(cport "$@")"; s=$(curl -s -m10 "$q/status" | tr -d '\n'); echo "STATUS $s"
  h=$(echo "$s" | grep -oE '"latest_block_height":"[0-9]+"' | grep -oE '[0-9]+')
  echo "PLAN $(curl -s -m10 "$q/abci_query?path=%22/cosmos.upgrade.v1beta1.Query/CurrentPlan%22" | tr -d '\n')"
  echo "OLD $(curl -s -m10 "$q/blockchain?minHeight=$((h-1000))&maxHeight=$((h-1000))" | tr -d '\n')"; }
"""


class Watch:
    def __init__(self, path, dry_run):
        with open(path, "rb") as f:
            self.cfg = tomllib.load(f)
        s = self.cfg.get("settings", {})
        self.state_file = s.get("state_file", "/var/lib/release-watch/state.json")
        self.tz = zoneinfo.ZoneInfo(s.get("timezone", "UTC"))
        self.tz_name = s.get("timezone", "UTC")
        self.remind_h = s.get("plan_remind_hours", 3)
        self.announce_h = s.get("announce_every_hours", 23)
        self.important = re.compile(s.get("important", DEFAULT_IMPORTANT), re.I)
        self.env = load_env(s.get("env_file", "/etc/release-watch.env"))
        self.dry_run = dry_run
        self.watch = self.cfg.get("watch", [])
        self.hosts = self.cfg.get("hosts", {})
        self.nodes = self.cfg.get("node", [])
        self.plans = self.cfg.get("plan", [])
        if not dry_run and not (self.env.get("TG_TOKEN") and self.env.get("TG_CHAT")):
            sys.exit("TG_TOKEN and TG_CHAT must be set (environment or env_file); use --dry-run to test")

    # ---- output ----

    def send(self, text):
        if self.dry_run:
            print(text, end="\n\n")
            return
        data = urllib.parse.urlencode({"chat_id": self.env["TG_CHAT"], "text": text, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.env['TG_TOKEN']}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()

    def load_state(self):
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def save_state(self, state):
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, self.state_file)

    def local_time(self, ts):
        return datetime.fromtimestamp(ts, self.tz).strftime("%d.%m %H:%M") + f" {self.tz_name}"

    # ---- remote probes ----

    def run_on_hosts(self, jobs_by_host):
        """{host: [(id, ct or None, shell)]} -> ({id: output}, [errors]); one SSH session per host."""
        results, errors = {}, []
        for host, jobs in jobs_by_host.items():
            try:
                results.update(self.remote(host, jobs))
            except Exception as e:
                errors.append(f"{host}: {e}")
        return results, errors

    def remote(self, host, jobs):
        h = self.hosts.get(host)
        if h is None:
            raise RuntimeError("not defined in [hosts]")
        script = []
        for jid, ct, cmd in jobs:
            b64 = base64.b64encode((SH + cmd).encode()).decode()
            inner = f'bash -c "$(echo {b64} | base64 -d)"'
            run = f"pct exec {ct} -- {inner}" if ct else inner
            script.append(f'echo "@@{jid}"; timeout 60 {run} 2>&1 | head -c 4000; echo')
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", *h.get("ssh_opts", []), h["ssh"],
                "bash -s"] if h.get("ssh") else ["bash", "-s"]
        p = subprocess.run(argv, input="\n".join(script), capture_output=True, text=True, timeout=900)
        out, cur = {}, None
        for line in p.stdout.splitlines():
            if line.startswith("@@"):
                cur = line[2:]
                out[cur] = []
            elif cur is not None and "setlocale" not in line:
                out[cur].append(line)
        if not out and p.returncode:
            raise RuntimeError(p.stderr.strip()[:200])
        return {k: "\n".join(v).strip() for k, v in out.items()}

    # ---- releases ----

    def check_releases(self, state, errors, sent, summary):
        for w in self.watch:
            label, source, repo, tag_re = w["label"], w.get("source", "github"), w["repo"], w.get("tag_filter")
            key = f"{source}:{repo}:{tag_re or ''}"
            try:
                rels = fetch(source, repo, self.env.get("GITHUB_TOKEN"))
            except Exception as e:
                errors.append(f"{label}: {e}")
                continue
            if tag_re:
                rels = [r for r in rels if re.search(tag_re, r["tag"])]
            if w.get("branch_regex"):
                br = lambda r: int(m.group(1)) if (m := re.search(w["branch_regex"], r["tag"])) else 0
                top = max((br(r) for r in rels if not r["pre"]), default=0)
                rels = [r for r in rels if br(r) >= top]
            seen = set(state.get(key, []))
            if key in state:
                for r in reversed([r for r in rels if r["tag"] not in seen]):
                    flag = " ⚠️ <b>important</b>" if self.important.search(r["body"]) else ""
                    kind = "pre-release" if r["pre"] else "stable"
                    nets = f"\nNetworks: {esc(w['networks'])}" if w.get("networks") else ""
                    self.send(f"🆕 <b>{esc(label)}</b> {esc(r['tag'])} ({kind}){flag}{nets}\n"
                              f"Date: {r['date']}\n{r['url']}")
                    sent.append(f"{label} {r['tag']}{' ⚠️' if flag else ''}")
            elif rels:
                stable = next((r for r in rels if not r["pre"]), rels[0])
                summary.append(f"• {esc(label)}: {esc(stable['tag'])} ({stable['date']})")
            state[key] = sorted(seen | {r["tag"] for r in rels})
            latest = state.setdefault("_latest", {})
            latest[label] = latest_releases(w, rels)
            for a in self.cfg.get("alias", []):
                if a["base"] == label:
                    latest[a["name"]] = latest_releases(a, [r for r in rels if re.search(a["tag_filter"], r["tag"])])

    # ---- cosmos upgrade plans ----

    def check_plans(self, state, errors):
        if not self.plans:
            return
        jobs = {}
        for p in self.plans:
            jobs.setdefault(p["host"], []).append((p["network"], p.get("ct"),
                                                   f"plan {p['service']} {p.get('home', '')}"))
        results, errs = self.run_on_hosts(jobs)
        errors += [f"plans {e}" for e in errs]
        known = state.setdefault("_plans", {})
        for net in (p["network"] for p in self.plans):
            if net not in results:
                continue
            try:
                p = read_plan(results[net])
            except Exception as e:
                errors.append(f"plan {net}: {e}")
                continue
            if not p:
                known.pop(net, None)
                continue
            old = known.get(net, {})
            same = old.get("name") == p["name"] and old.get("height") == p["height"]
            p["reminded"] = old.get("reminded", False) if same else False
            when = f"{self.local_time(p['eta'])}, in {fmt_left(p['eta'] - time.time())}" if p["eta"] else "time unknown"
            text = (f"<b>{esc(net)}</b>: upgrade <b>{esc(p['name'])}</b> at height {p['height']}\n"
                    f"Current height {p['cur']}, expected {when}")
            if not same:
                self.send("⏫ Upgrade scheduled\n" + text)
            elif not p["reminded"] and p["eta"] and p["eta"] - time.time() < self.remind_h * 3600:
                self.send(f"⏰ Upgrade in less than {self.remind_h} h\n" + text)
                p["reminded"] = True
            known[net] = p

    # ---- forum posts and doc pages ----

    def check_announce(self, state, errors):
        known = state.setdefault("_announce", {})
        for f in self.cfg.get("forum", []):
            label, base, q = f["label"], f["url"].rstrip("/"), f["query"]
            time.sleep(15)  # Discourse rate-limits anonymous search (429)
            key = f"forum:{base}:{q}"
            try:
                d = get_json(f"{base}/search.json?q={urllib.parse.quote(q + ' order:latest')}", {})
            except Exception as e:
                errors.append(f"{label} forum: {e}")
                continue
            topics = {t["id"]: t for t in d.get("topics", [])}
            posts = d.get("posts", [])
            if key in known:
                seen = set(known[key])
                for p in reversed([p for p in posts if p["id"] not in seen]):
                    t = topics.get(p["topic_id"], {})
                    self.send(f"📣 <b>{esc(label)}</b>: new forum post\n<b>{esc(t.get('title', ''))}</b>\n"
                              f"{esc(p['username'])}, {p['created_at'][:10]}: {esc(p.get('blurb', '')[:300])}\n"
                              f"{base}/t/{t.get('slug', 'x')}/{p['topic_id']}/{p.get('post_number', 1)}")
            known[key] = sorted(set(known.get(key, [])) | {p["id"] for p in posts})
        for d in self.cfg.get("doc", []):
            label, url = d["label"], d["url"]
            key = f"doc:{url}"
            try:
                text = get_text(url)
            except Exception as e:
                errors.append(f"{label} docs: {e}")
                continue
            h = hashlib.sha256(text.encode()).hexdigest()
            old = known.get(key)
            if old and old["hash"] != h:
                added = [l[1:].strip() for l in difflib.unified_diff(old["text"].splitlines(), text.splitlines(),
                                                                       lineterm="", n=0)
                         if l.startswith("+") and not l.startswith("+++") and l[1:].strip()]
                body = "\n".join(f"+ {esc(l[:200])}" for l in added[:12]) or "(only removals)"
                self.send(f"📣 <b>{esc(label)}</b>: documentation page changed\n{body}\n{d.get('page', url)}")
            known[key] = {"hash": h, "text": text}

    # ---- runs ----

    def hourly(self):
        state = self.load_state()
        first_run = not state
        summary, errors, sent = [], [], []
        self.check_releases(state, errors, sent, summary)
        try:
            self.check_plans(state, errors)
        except Exception as e:
            errors.append(f"plans: {e}")
        if self.cfg.get("forum") or self.cfg.get("doc"):
            try:
                if time.time() - state.get("_announce", {}).get("_t", 0) > self.announce_h * 3600:
                    self.check_announce(state, errors)
                    state["_announce"]["_t"] = time.time()
            except Exception as e:
                errors.append(f"announce: {e}")
        now = time.time()
        state["_log"] = [e for e in state.get("_log", []) if e["t"] > now - 3 * 86400] + \
                        [{"t": now, "sent": sent, "errors": [e.split(":", 1)[0] for e in errors]}]
        self.save_state(state)
        if summary:
            head = "👀 Release watch started. Latest stable versions:\n" if first_run else "👀 Now watching:\n"
            self.send(head + "\n".join(summary))
        if errors:
            print("\n".join(errors), file=sys.stderr)
            sys.exit(1)

    def daily(self):
        state = self.load_state()
        day = [e for e in state.get("_log", []) if e["t"] > time.time() - 86400]
        sent = [r for e in day for r in e["sent"]]
        failed = [e for e in day if e["errors"]]
        lines = ["📋 <b>Daily report</b>", f"Checks: {len(day)}, sources: {len(self.watch)}"]
        if sent:
            lines.append(f"New releases ({len(sent)}):")
            lines += [f"• {esc(r)}" for r in sent]
        else:
            lines.append("No new releases")
        if not day:
            lines.append("⚠️ No checks in the last 24 h — the hourly timer is not running")
        elif failed:
            names = sorted({n for e in failed for n in e["errors"]})
            lines.append(f"⚠️ Checks with errors: {len(failed)} ({esc(', '.join(names))})")
        else:
            lines.append("✅ All checks passed")
        plans = state.get("_plans", {})
        if plans:
            lines += ["", "⏫ <b>Scheduled upgrades</b>:"]
            for net, p in sorted(plans.items()):
                when = f"{self.local_time(p['eta'])}, in {fmt_left(p['eta'] - time.time())}" if p.get("eta") \
                    else "time unknown"
                lines.append(f"• {esc(net)}: {esc(p['name'])} at height {p['height']} ({when})")
        if self.nodes:
            lines.append("")
            try:
                lines += self.version_report(state)
            except Exception as e:
                lines.append(f"❓ Version check failed: {esc(str(e))[:200]}")
        text = "\n".join(lines)
        self.send(text if len(text) < 4000 else text[:3990] + "\n…")

    def version_report(self, state):
        jobs = {}
        for i, n in enumerate(self.nodes):
            jobs.setdefault(n["host"], []).append((str(i), n.get("ct"), n["probe"]))
        results, errs = self.run_on_hosts(jobs)
        latest = state.get("_latest", {})
        behind, pre_only, unknown, ok = [], [], list(errs), 0
        for i, n in enumerate(self.nodes):
            label, net = n["label"], n["network"]
            out = results.get(str(i), "")
            m = re.search(n["regex"], out) if n.get("regex") else None
            inst = parse_ver(m.group(1) if m else out)
            rel = latest.get(label, {})
            if not inst or not rel:
                unknown.append(f"{net} ({label})")
                continue
            stable, newest = rel.get("stable"), rel.get("any")
            st_v = parse_ver(stable[1]) if stable else None
            any_v = parse_ver(newest[1]) if newest else None
            if st_v and inst[0] < st_v[0]:
                age = ""
                if stable[2]:
                    days = (time.time() - datetime.fromisoformat(stable[2]).replace(tzinfo=timezone.utc).timestamp()) // 86400
                    age = f", released {stable[2]}, {days:.0f} days ago"
                behind.append(f"• {esc(net)} — {esc(label)}: running {esc(inst[1])} → {esc(stable[1])}{age}")
            elif n.get("testnet") and any_v and inst[0] < any_v[0] and (not st_v or any_v[0] > st_v[0]):
                pre_only.append(f"• {esc(net)} — {esc(label)}: running {esc(inst[1])}, "
                                f"pre-release {esc(newest[1])} ({newest[2]})")
            else:
                ok += 1
        lines = [f"🔎 <b>Installed versions</b>: up to date {ok} of {len(self.nodes)}"]
        if behind:
            lines += ["⬆️ Behind the latest stable release:"] + behind
        if pre_only:
            lines += ["🧪 Testnets with a newer pre-release:"] + pre_only
        if unknown:
            lines.append(f"❓ Could not read the version: {esc(', '.join(unknown))}")
        return lines


def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"')
    except FileNotFoundError:
        pass
    for k in ("TG_TOKEN", "TG_CHAT", "GITHUB_TOKEN"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def get_json(url, headers):
    req = urllib.request.Request(url, headers={**UA, **headers})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def get_text(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def fetch(source, repo, gh_token):
    """Releases newest first as dicts: tag, pre, url, date, body."""
    if source == "github":
        headers = {"Accept": "application/vnd.github+json"}
        if gh_token:
            headers["Authorization"] = f"Bearer {gh_token}"
        data = get_json(f"https://api.github.com/repos/{repo}/releases?per_page=100", headers)
        return [{"tag": r["tag_name"], "pre": r["prerelease"], "url": r["html_url"],
                 "date": (r.get("published_at") or "")[:10], "body": r.get("body") or ""}
                for r in data if not r.get("draft")]
    if source == "gitlab":
        data = get_json(f"https://gitlab.com/api/v4/projects/{urllib.parse.quote(repo, safe='')}/releases?per_page=100", {})
        return [{"tag": r["tag_name"], "pre": bool(r.get("upcoming_release")),
                 "url": f"https://gitlab.com/{repo}/-/releases/{r['tag_name']}",
                 "date": (r.get("released_at") or "")[:10], "body": r.get("description") or ""}
                for r in data]
    raise ValueError(f"unknown source {source!r}")


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


VER_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-.~]?(rc|beta|alpha)(?:[.-]?(\d+))?)?", re.I)
PRE_RANK = {"alpha": 0, "beta": 1, "rc": 2}


def parse_ver(s):
    """'v29.1.0-rc0' -> ((29, 1, 0, 0, 2, 0), '29.1.0-rc0'); stable versions sort above their rc."""
    m = VER_RE.search(s or "")
    if not m:
        return None
    nums = tuple(int(x or 0) for x in m.group(1, 2, 3, 4))
    pre = m.group(5)
    rank = (PRE_RANK[pre.lower()], int(m.group(6) or 0)) if pre else (3, 0)
    return nums + rank, m.group(0)


def latest_releases(w, rels):
    """Newest stable and newest overall release by version number: {"stable": [tag, ver, date], "any": [...]}."""
    best = {}
    for r in rels:
        if w.get("version_tag") and not re.search(w["version_tag"], r["tag"]):
            continue
        src = r["tag"]
        if w.get("version_body"):
            m = re.search(w["version_body"], r["body"])
            if not m:
                continue
            src = m.group(1)
        v = parse_ver(src)
        if not v:
            continue
        for kind in ("any",) if r["pre"] or v[0][4] < 3 else ("any", "stable"):
            if kind not in best or v[0] > best[kind][0]:
                best[kind] = (v[0], [r["tag"], v[1], r["date"]])
    return {k: v[1] for k, v in best.items()}


def pb_fields(b):
    """Minimal protobuf reader: {field number: last value} (varint -> int, length-delimited -> bytes)."""
    def varint():
        nonlocal i
        val, shift = 0, 0
        while True:
            c = b[i]; i += 1
            val |= (c & 0x7F) << shift; shift += 7
            if c < 0x80:
                return val
    out, i = {}, 0
    while i < len(b):
        key = varint()
        fn, wt = key >> 3, key & 7
        if wt == 0:
            val = varint()
        elif wt == 2:
            n = varint()
            val = b[i:i + n]; i += n
        elif wt == 1:
            val = b[i:i + 8]; i += 8
        elif wt == 5:
            val = b[i:i + 4]; i += 4
        else:
            raise ValueError(f"wire type {wt}")
        out[fn] = val
    return out


def parse_time(s):
    s = re.sub(r"(\.\d{6})\d*", r"\1", s).replace("Z", "+00:00")
    return datetime.fromisoformat(s).timestamp()


def fmt_left(sec):
    h = sec / 3600
    return f"~{sec / 60:.0f} min" if h < 1 else f"~{h:.1f} h" if h < 48 else f"~{h / 24:.1f} days"


def read_plan(out):
    """Parse the `plan` helper output -> dict(name, height, info, cur, eta), or None if nothing is scheduled.
    ETA is extrapolated from the average block time over the last 1000 blocks."""
    parts = {l.split(" ", 1)[0]: l.split(" ", 1)[1] for l in out.splitlines() if " " in l}
    status = json.loads(parts["STATUS"])["result"]["sync_info"]
    cur, now_t = int(status["latest_block_height"]), parse_time(status["latest_block_time"])
    value = json.loads(parts["PLAN"])["result"]["response"]["value"]
    if not value:
        return None
    plan = pb_fields(pb_fields(base64.b64decode(value))[1])
    height = int(plan.get(3, 0))
    try:
        old = json.loads(parts["OLD"])["result"]["block_metas"][0]["header"]
        bt = (now_t - parse_time(old["time"])) / (cur - int(old["height"]))
    except Exception:
        bt = None
    eta = now_t + (height - cur) * bt if bt and height > cur else None
    return {"name": plan.get(1, b"").decode(), "height": height, "cur": cur, "eta": eta,
            "info": plan.get(4, b"").decode(errors="replace")}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-c", "--config", default=os.environ.get("RELEASE_WATCH_CONFIG", "/etc/release-watch/config.toml"))
    ap.add_argument("--daily", action="store_true", help="send the 24h report instead of checking")
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending them")
    a = ap.parse_args()
    w = Watch(a.config, a.dry_run)
    w.daily() if a.daily else w.hourly()


if __name__ == "__main__":
    main()
