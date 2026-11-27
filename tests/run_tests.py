"""Tests for the battle log. Builds the live and Claude versions into a temporary folder, serves them, and drives them in
Chromium (Playwright) against a campaign backup, with a stand-in database (fbmock.js) for the saving tests.

  python3 tests/run_tests.py <backup.json> [test names...]

The backup is any file from the Backup button; it is only read. Needs: pip install playwright && playwright install chromium.
Names of the tests: see TESTS at the bottom. With none given, all run."""
import functools, http.server, json, os, re, shutil, subprocess, sys, tempfile, threading, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MOCK = os.path.join(HERE, 'fbmock.js')

if len(sys.argv) < 2 or not os.path.exists(sys.argv[1]):
    sys.exit(__doc__)
BACKUP = json.load(open(sys.argv[1], encoding='utf-8'))
ONLY = set(sys.argv[2:])
DATA = {k: BACKUP.get(k) or {} for k in ('gangs', 'battles', 'territories', 'config')}

from playwright.sync_api import sync_playwright  # noqa: E402

# ---------- build and serve ----------
TMP = tempfile.mkdtemp(prefix='lsl-tests-')
for script, sub in (('build_hosted.py', 'hosted'), ('build_claude_copy.py', 'claude')):
    subprocess.run([sys.executable, os.path.join(ROOT, 'tools', script), os.path.join(TMP, sub)], check=True, stdout=subprocess.DEVNULL)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def guess_type(self, path):  # say UTF-8, as the real hosts do
        t = super().guess_type(path)
        return t + '; charset=utf-8' if t in ('text/html', 'application/javascript', 'text/plain') else t


def serve(folder):
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=folder))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return 'http://127.0.0.1:%d/index.html' % srv.server_address[1]


HOSTED_URL, CLAUDE_URL = serve(os.path.join(TMP, 'hosted')), serve(os.path.join(TMP, 'claude'))
PW = sync_playwright().start()
BROWSER = PW.chromium.launch()


def store_js(data):
    store = {f'{k}/{i}': v for k in ('gangs', 'battles', 'territories') for i, v in (data.get(k) or {}).items()}
    return 'Object.assign(window.__store, %s);' % json.dumps(store)


def page(mode='local', data=None, width=1400, height=900, lat=30, extra=''):
    """mode: local (browser storage), hosted (live build, stand-in Firebase) or claude (Claude copy, stand-in Claude database)."""
    data = DATA if data is None else data
    ctx = BROWSER.new_context(viewport={'width': width, 'height': height})
    ctx.route('**/fonts.googleapis.com/**', lambda r: r.fulfill(body='', content_type='text/css'))
    ctx.route('**/firebasejs/**', lambda r: r.fulfill(body='', content_type='application/javascript'))
    if mode == 'local':
        ctx.add_init_script('localStorage.setItem("long-salvage-log-v1", %s)' % json.dumps(json.dumps(data)))
    else:
        ctx.add_init_script(path=MOCK)
        init = store_js(data) + f'window.__lat={lat};'
        if mode == 'claude':
            init += ('const __fs = window.firebase.firestore(); delete window.firebase;'
                     'window.claude = { use: async n => n === "db" ? __fs : n === "user" ? { id: async () => "u1", profiles: async ids => Object.fromEntries(ids.map(i => [i, { name: "Tester" }])) } : null };')
        ctx.add_init_script(init + extra)
    pg = ctx.new_page()
    pg.errors = []
    pg.on('pageerror', lambda e: pg.errors.append(str(e)))
    pg.on('console', lambda m: m.type == 'error' and 'fonts.googleapis' not in m.text and 'ERR_' not in m.text and pg.errors.append('console: ' + m.text[:300]))
    pg.goto(HOSTED_URL if mode == 'hosted' else CLAUDE_URL)
    pg.wait_for_timeout(1300)
    return pg


def close(pg):
    pg.context.close()


def all_views(pg, each):
    """Open every tab, gang page, battle and post-cycle gang tab, calling each(label) after each one."""
    for t in pg.eval_on_selector_all('[data-tab]', 'els => els.map(e => e.dataset.tab)'):
        pg.click(f'[data-tab="{t}"]'); pg.wait_for_timeout(200); each('tab ' + t)
        if t in ('gangs', 'npcs'):
            for v in pg.eval_on_selector_all('[data-act="gangView"]', 'els => els.map(e => e.dataset.id)'):
                pg.click(f'[data-act="gangView"][data-id="{v}"]'); pg.wait_for_timeout(100); each(f'{t}/{v}')
        if t == 'rules':
            for o in pg.eval_on_selector_all('#rules-pick option', 'els => els.map(e => e.value)'):
                pg.select_option('#rules-pick', o); pg.wait_for_timeout(60)
    pg.click('[data-tab="battles"]')
    for i in pg.evaluate('Object.keys(S.battles)'):
        pg.evaluate('id => { S.sel = id; render(); }', i); pg.wait_for_timeout(100); each('battle ' + i)
        for s in pg.eval_on_selector_all('[data-act="pcGang"]', 'els => els.map(e => e.dataset.id)'):
            pg.evaluate('s => document.querySelector(`[data-act="pcGang"][data-id="${s}"]`)?.click()', s); pg.wait_for_timeout(80); each(f'{i}/{s}')


# ---------- tests: each returns a list of problems (empty = pass) ----------
def t_syntax():
    src = open(os.path.join(ROOT, 'src', 'long-salvage-log.html'), encoding='utf-8').read()
    js = max(re.findall(r'<script[^>]*>(.*?)</script>', src, re.S), key=len)
    if not shutil.which('node'):
        return []
    f = os.path.join(TMP, 'main.js'); open(f, 'w', encoding='utf-8').write(js)
    r = subprocess.run(['node', '--check', f], capture_output=True, text=True)
    return [r.stderr.strip()[:400]] if r.returncode else []


def t_equipment():
    out = []
    r = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'equipment.py'), 'check'], capture_output=True, text=True)
    if r.returncode: out.append(r.stdout.strip())
    pg = page()
    out += pg.evaluate("""() => { const out = [], ids = new Set();
      for (const it of Object.values(EQUIPMENT.items)) { if (ids.has(it.id)) out.push('duplicate item id ' + it.id); ids.add(it.id);
        if (it.optionOf && !eqItem(it.optionOf)) out.push('optionOf points nowhere: ' + it.id); }
      for (const sec of EQUIPMENT.tradingPost) for (const g of sec.groups) for (const id of g.items) if (!eqItem(id)) out.push('Trading Post lists a missing item: ' + id);
      for (const gl of EQUIPMENT.gangLists) for (const part of ['ranged', 'close', 'wargear']) for (const g of gl.list?.[part] || []) for (const e of g.entries) {
        if (!eqItem(e.item)) out.push(gl.title + ': list entry for a missing item ' + e.item);
        if (e.credits === undefined || e.credits === '' || isNaN(+e.credits)) out.push(gl.title + ': price is not a number for ' + e.item); }
      for (const g of ['delaque', 'gsc', 'kyn', 'fangs']) if (!buyOptions(g, 'list').length) out.push('no Equipment List items for ' + g);
      if (!buyOptions('kyn', 'tp').length) out.push('no Trading Post items');
      return out; }""")
    out += pg.errors; close(pg)
    return out


FAULT = "couldn't be shown"


def t_every_view():
    out = []
    for w, h, tag in ((1400, 900, 'desktop'), (390, 844, 'phone'), (320, 640, 'small phone')):
        pg = page(width=w, height=h)
        def check(label):
            wide = pg.evaluate('() => document.documentElement.scrollWidth - innerWidth')
            if wide > 2: out.append(f'{tag} {label}: page is {wide}px wider than the screen')
            if FAULT in pg.inner_text('#view'): out.append(f'{tag} {label}: the view showed the data-fault message')
        all_views(pg, check)
        out += [f'{tag}: {e}' for e in pg.errors]; close(pg)
    return out


def t_injection():
    pay = '"\'><img src=x onerror="(window.__xss=window.__xss||[]).push(1)">'
    keep = {'id', 'updated', 'kind', 'role', 'status', 'gang', 'fighter', 'victim', 'by', 'target', 'action', 'result', 'date', 'owner', 'rival',
            'sel', 'table', 'method', 'priority', 'visibilityMode', 'winner', 'scenario', 'cycle', 'house', 'type', 'item', 'source', 'claimT', 'tid'}
    def poison(v, k=''):
        if isinstance(v, dict): return {a: (b if a in keep else poison(b, a)) for a, b in v.items()}
        if isinstance(v, list): return [poison(x, k) for x in v]
        return v + pay if isinstance(v, str) and len(v) > 2 and not re.fullmatch(r'[\d-]+', v) else v
    data = {k: poison(DATA[k]) for k in ('gangs', 'battles', 'territories')}; data['config'] = DATA['config']
    pg = page(data=data); hits = []
    all_views(pg, lambda label: hits.append(label) if pg.evaluate('(window.__xss || []).splice(0).length') or FAULT in pg.inner_text('#view') else None)
    out = [f'text typed into the log ran as code, or broke the view, on {h}' for h in hits] + pg.errors; close(pg)
    return out


def type_during_save(mode, wait):
    pg = page(mode, lat=400 if mode == 'hosted' else 300)
    bid = pg.evaluate('Object.keys(S.battles).find(id => !isPC(S.battles[id]))')
    pg.evaluate('id => { S.sel = id; render(); }', bid); pg.wait_for_timeout(300)
    box = pg.locator('[data-bind="b.pre"]').first
    box.scroll_into_view_if_needed(); box.click(); pg.keyboard.press('Control+End')
    pg.keyboard.type(' AAAA', delay=20); pg.wait_for_timeout(wait); pg.keyboard.type(' BBBB', delay=20); pg.wait_for_timeout(5000)
    got = [box.input_value(), pg.evaluate('id => S.battles[id].pre', bid), pg.evaluate('id => window.__store["battles/" + id].pre', bid)]
    out = [f'{mode}, typing again {wait} ms into a save: {where} ends "{v[-10:]}"' for where, v in zip(('the box', 'the log', 'the database'), got) if not v.endswith('AAAA BBBB')]
    out += pg.errors; close(pg)
    return out


def t_typing_during_save():
    return type_during_save('hosted', 800) + type_during_save('hosted', 1100) + type_during_save('hosted', 1250) + type_during_save('claude', 800) + type_during_save('claude', 1000)


def two_editors(mode, field, at, echo_first=False):
    pg = page(mode, lat=400, extra='window.__echoFirst = true;' if echo_first else '')
    pg.evaluate('() => { window.__toasts = []; const o = toast; window.toast = m => { window.__toasts.push(m); o(m); }; }')
    bid = pg.evaluate('Object.keys(S.battles).find(id => !isPC(S.battles[id]))')
    pg.evaluate('id => { S.sel = id; render(); }', bid); pg.wait_for_timeout(300)
    box = pg.locator('[data-bind="b.pre"]').first
    box.scroll_into_view_if_needed(); box.click(); pg.keyboard.press('Control+End')
    pg.keyboard.type(' AAAA', delay=20); pg.wait_for_timeout(at)
    pg.evaluate("""([id, f]) => { const d = JSON.parse(JSON.stringify(window.__store['battles/' + id])); d[f] = (d[f] || '') + ' REMOTE'; window.__remoteWrite('battles/' + id, d); }""", [bid, field])
    pg.wait_for_timeout(max(50, 1100 - at)); pg.keyboard.type(' BBBB', delay=20); pg.wait_for_timeout(5000)
    S = pg.evaluate('id => S.battles[id]', bid); D = pg.evaluate('id => window.__store["battles/" + id]', bid); toasts = pg.evaluate('window.__toasts')
    label = f'{mode}{" (echo first)" if echo_first else ""}, other person edits {field} at {at} ms'
    out = []
    for where, rec in (('log', S), ('database', D)):
        if not (rec.get('pre') or '').endswith('AAAA BBBB'): out.append(f'{label}: my typing lost in the {where}')
        if field != 'pre' and not (rec.get(field) or '').endswith('REMOTE'): out.append(f'{label}: their change lost in the {where}')
    clash = any('same time' in t for t in toasts)
    if field == 'pre' and not clash: out.append(f'{label}: no warning that we both changed the same thing')
    if field != 'pre' and clash: out.append(f'{label}: false "edited at the same time" warning')
    out += pg.errors; close(pg)
    return out


def t_two_editors():
    out = []
    for mode in ('hosted', 'claude'):
        out += two_editors(mode, 'post', 300) + two_editors(mode, 'post', 900) + two_editors(mode, 'treachResult', 1150) + two_editors(mode, 'pre', 300)
    return out + two_editors('claude', 'post', 900, echo_first=True) + two_editors('claude', 'pre', 300, echo_first=True)


def t_critical_injury():
    pg = page()
    r = pg.evaluate("""() => { const out = {}; const g = S.gangs.kyn, f = g.fighters.find(x => x.role === 'ganger' && !isGone(x) && x.status !== 'retired');
      const st = () => { AVAIL = null; const x = availability().now[f.id]; return x ? x.s : 'fine'; };
      const battle = (id, date, gangs, inj) => ({ kind: 'conflict', avail: true, injuryTable: 'n26', id, date, scenario: id, gangs, attacker: [], defender: [], events: [], injuries: inj, rewards: [], participants: [] });
      S.battles['b-t1'] = battle('b-t1', '2099-01-01', ['kyn', 'gsc'], [{ id: 'i1', victim: f.id, roll: '63' }]); out.afterBattle = st();
      S.battles['pc-t'] = { type: 'postcycle', avail: true, id: 'pc-t', date: '2099-01-03', cycle: 'T', sheets: {} }; out.postCycleStarted = st(); out.inGraveyard = inGraveyard(f);
      S.battles['b-t2'] = battle('b-t2', '2099-01-10', ['gsc', 'fangs'], []); out.nextBattleNoEscort = st();
      const leader = g.fighters.find(x => x.role === 'leader')?.id;
      S.battles['pc-t'].sheets = { kyn: { actions: [{ id: 'm1', kind: 'medical', fighter: leader, target: f.id, pay: true, extra: '', d6: '6', d6b: '' }] } }; out.escortSaved = st();
      S.battles['pc-t'].sheets.kyn.actions[0].d6 = '1'; out.escortComplications = st();
      return out; }""")
    want = {'afterBattle': 'critical', 'postCycleStarted': 'critical', 'inGraveyard': False, 'nextBattleNoEscort': 'dead', 'escortSaved': 'recovery', 'escortComplications': 'dead'}
    out = [f'{k}: got {r[k]}, expected {v}' for k, v in want.items() if r[k] != v] + pg.errors; close(pg)
    return out


def t_map_setup():
    delay_real = """(() => { const fs = window.firebase.firestore(), orig = fs.collection; let first = true;
      const terr = Object.fromEntries(Object.entries(window.__store).filter(([k]) => k.startsWith('territories/')));
      fs.collection = c => { const q = orig(c); if (c !== 'territories' || !first) return q; first = false;
        return { ...q, onSnapshot: fn => { setTimeout(() => fn({ metadata: { fromCache: true }, docChanges: () => [] }), 5);
          setTimeout(() => fn({ metadata: { fromCache: false }, docChanges: () => Object.entries(terr).map(([k, v]) => ({ type: 'added', doc: { id: k.split('/')[1], exists: true, data: () => JSON.parse(JSON.stringify(v)) } })) }), 900);
          return () => {}; } }; };
      window.firebase.firestore = () => fs; })();"""
    empty = """(() => { for (const k of Object.keys(window.__store)) if (k.startsWith('territories/')) delete window.__store[k];
      const fs = window.firebase.firestore(), orig = fs.collection; let first = true;
      fs.collection = c => { const q = orig(c); if (c !== 'territories' || !first) return q; first = false;
        return { ...q, onSnapshot: fn => { setTimeout(() => fn({ metadata: { fromCache: true }, docChanges: () => [] }), 5); return () => {}; } }; };
      window.firebase.firestore = () => fs; })();"""
    out = []
    for label, extra, want_seed in (('empty cache, real map in the database', delay_real, False), ('empty cache, empty database', empty, True), ('normal load', '', False)):
        pg = page('hosted', extra=extra); pg.wait_for_timeout(2000)
        seeded = pg.evaluate('!!(window.__store["territories/_meta"] || {}).seeded && window.__store["territories/_meta"].seeded.slice(0, 4) >= "2026" && window.__writes.filter(w => w[0].startsWith("territories/")).length > 5')
        if seeded != want_seed: out.append(f'{label}: fresh map {"set up" if seeded else "not set up"}')
        out += pg.errors; close(pg)
    return out


def t_missing_gang():
    out = []
    for gid in [g for g in DATA['gangs']]:
        data = json.loads(json.dumps(DATA)); data['gangs'].pop(gid)
        pg = page(data=data); all_views(pg, lambda label: None)
        out += [f'without {gid}: {e}' for e in pg.errors]
        if pg.evaluate('document.body.innerText.includes("couldn\'t be shown")'): out.append(f'without {gid}: a view showed the data-fault message')
        close(pg)
    return out


def t_log_record_purchases():
    pg = page('claude')
    pc = pg.evaluate('Object.keys(S.battles).find(id => isPC(S.battles[id]))')
    if not pc:
        close(pg); return []
    r = pg.evaluate("""pcid => { const pc = S.battles[pcid]; const gid = Object.keys(pc.sheets || {})[0] || 'kyn'; const sh = ((pc.sheets ||= {})[gid] ||= {}); sh.actions ||= [];
      let a = sh.actions.find(x => x.kind === 'trade'); if (!a) { a = { id: 'pcaT', kind: 'trade', fighter: '', lines: [] }; sh.actions.push(a); } a.lines ||= [];
      const snap = () => JSON.parse(JSON.stringify(pc)), out = {};
      let p0 = snap(); a.lines.push({ id: 'tlX', type: 'buy', source: '', item: '', name: '', credits: '', tp: '', qty: 1 }); out.empty = actDiff('battles', p0, snap()).length;
      p0 = snap(); Object.assign(a.lines.at(-1), { source: 'tp', item: 'autogun', name: 'Autogun', credits: '15', tp: '1' }); out.bought = actDiff('battles', p0, snap()).map(x => x[1]).join(' ');
      p0 = snap(); a.lines.at(-1).credits = '20'; out.changed = actDiff('battles', p0, snap()).map(x => x[1]).join(' ');
      p0 = snap(); a.lines.pop(); out.removed = actDiff('battles', p0, snap()).map(x => x[1]).join(' ');
      return out; }""", pc)
    out = []
    if r['empty']: out.append('an empty purchase line was logged')
    if 'bought Autogun' not in r['bought']: out.append('buying was not logged: ' + r['bought'])
    if 'changed' not in r['changed'] or '20 credits' not in r['changed']: out.append('a price change was not logged: ' + r['changed'])
    if 'removed purchase' not in r['removed']: out.append('removing a purchase was not logged: ' + r['removed'])
    out += pg.errors; close(pg)
    return out


def t_laptop_build():
    f = os.path.join(TMP, 'laptop.html')
    subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'build_laptop.py'), sys.argv[1], f], check=True, stdout=subprocess.DEVNULL)
    ctx = BROWSER.new_context(); ctx.route('**/fonts.googleapis.com/**', lambda r: r.fulfill(body='', content_type='text/css'))
    pg = ctx.new_page(); errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.goto('file://' + f); pg.wait_for_timeout(1200)
    n = pg.evaluate('[S.mode, Object.keys(S.gangs).length, Object.keys(S.battles).length]')
    out = [] if n == ['local', len(DATA['gangs']), len(DATA['battles'])] else [f'laptop build opened with {n}']
    ctx.close()
    return out + errs


def t_change_back():
    """I save a record; someone else changes it, then changes it back to exactly what I saved: I must see their change-back,
    and my next edit must not undo it."""
    out = []
    for mode in ('hosted', 'claude'):
        pg = page(mode)
        bid = pg.evaluate('Object.keys(S.battles).find(id => !isPC(S.battles[id]))')
        pg.evaluate('id => { S.battles[id].post = "MINE A"; save("battles", id); }', bid); pg.wait_for_timeout(1500)
        theirs = """([id, v]) => { const d = JSON.parse(JSON.stringify(window.__store['battles/' + id])); d.post = v; window.__remoteWrite('battles/' + id, d); }"""
        pg.evaluate(theirs, [bid, 'THEIRS B']); pg.wait_for_timeout(400)
        pg.evaluate(theirs, [bid, 'MINE A']); pg.wait_for_timeout(400)
        shown = pg.evaluate('id => S.battles[id].post', bid)
        pg.evaluate('id => { S.battles[id].pre = "edited"; save("battles", id); }', bid); pg.wait_for_timeout(1500)
        stored = pg.evaluate('id => window.__store["battles/" + id].post', bid)
        if shown != 'MINE A': out.append(f'{mode}: after their change-back the log still shows "{shown}"')
        if stored != 'MINE A': out.append(f'{mode}: my next edit wrote "{stored}" over their change-back')
        out += pg.errors; close(pg)
    return out


def t_restore_point():
    """Opening a restore point taken from the current data must show no differences, and must not change any record
    (the one-time fixes it has already had are not run again). Also for restore points saved before they listed their fixes."""
    out = []
    pg = page('hosted')
    pg.evaluate('async () => { rpMade = false; await makeRestorePoint(); }')
    rid = pg.evaluate('Object.keys(window.__store).find(k => k.startsWith("restorepoints/"))')
    if not rid:
        close(pg); return ['no restore point was made']
    rid = rid.split('/')[1]
    for label, prep in (('new restore point', ''), ('restore point without a fix list', f'() => {{ delete window.__store["restorepoints/{rid}"].applied; }}')):
        if prep: pg.evaluate(prep)
        pg.evaluate('async id => { pendingRestore = null; await loadRestorePoint(id); }', rid); pg.wait_for_timeout(300)
        r = pg.evaluate("""() => { const p = pendingRestore; if (!p) return null;
          const changed = []; for (const k of ['gangs', 'battles', 'territories']) for (const id of Object.keys(p.d[k] || {})) if (id !== '_meta' && stable(p.d[k][id]) !== stable(S[k][id])) changed.push(k + '/' + id);
          return { rows: p.rows.length, changed, heading: document.querySelector('#restore-confirm b')?.textContent || '' }; }""")
        if r is None: out.append(f'{label}: did not open'); continue
        if r['rows']: out.append(f'{label}: shows {r["rows"]} differences with the data it was just taken from')
        if r['changed']: out.append(f'{label}: opening it changed {", ".join(r["changed"][:5])}')
        if not r['heading'].startswith('Restore point'): out.append(f'{label}: heading says "{r["heading"]}"')
    # every one-time fix must leave up-to-date data alone when run again
    again = pg.evaluate("""() => { const keep = { gangs: S.gangs, battles: S.battles, territories: S.territories, config: S.config }, out = [];
      for (const p of ROSTER_PATCHES) {
        S.gangs = JSON.parse(JSON.stringify(keep.gangs)); S.battles = JSON.parse(JSON.stringify(keep.battles)); S.territories = JSON.parse(JSON.stringify(keep.territories)); S.config = { applied: ROSTER_PATCHES.filter(x => x !== p).map(x => x.id) };
        applyRosterPatches();
        for (const k of ['gangs', 'battles']) for (const id of Object.keys(keep[k])) if (stable(JSON.parse(JSON.stringify(S[k][id]))) !== stable(keep[k][id])) out.push(p.id + ' changes ' + k + '/' + id);
      }
      Object.assign(S, keep); reindex(); return out; }""")
    out += ['running a fix again: ' + x for x in again if 'injury-table' not in x]
    out += pg.errors; close(pg)
    return out


def t_munda_same_day():
    """Munda XP typed in, then a battle logged later the same day: Should show counts that battle's XP; a battle logged
    earlier that day doesn't count (Munda already had it)."""
    pg = page()
    r = pg.evaluate("""() => { const g = Object.values(S.gangs).find(x => !x.npc), f = g.fighters.find(x => canEarnXp(x.id) && !isGone(x) && !isVehicleRole(x.id) && !x.mundaSkip);
      const today = localDay(), mk = (id, n) => ({ kind: 'conflict', id, date: today, scenario: id, gangs: [g.id], attacker: [], defender: [], injuries: [], participants: [], rewards: [{ id: 'r' + id, kind: 'xp', fighter: f.id, gang: g.id, amount: n }], events: [] });
      S.battles['b-early'] = mk('b-early', 5);           // logged before the Munda XP was typed (no time in its id: treated as earlier)
      AVAIL = null; setMundaXp(f.id, 40);
      S.battles[uid('b-')] = mk('later', 3);              // logged afterwards, same day
      for (const [id, b] of Object.entries(S.battles)) b.id = id;
      AVAIL = null; return mundaXpOf(f, career()[f.id]); }""")
    out = [] if r.get('since') == 3 else [f'XP since the Munda value: got {r.get("since")}, expected 3 (only the battle logged afterwards)']
    out += pg.errors; close(pg)
    return out


def views_of(pg):
    """Every view: the tabs, each gang page, each battle, and each gang's sheet of each post-cycle."""
    v = [{'label': 'battle list', 'tab': 'battles'}] + [{'label': 'tab ' + t, 'tab': t} for t in ('territories', 'equipment', 'rules')]
    for g in pg.evaluate('Object.keys(S.gangs)'):
        npc = pg.evaluate('g => !!S.gangs[g].npc', g); v.append({'label': ('npcs/' if npc else 'gangs/') + g, 'tab': 'npcs' if npc else 'gangs', 'gangView': g})
    for bid in pg.evaluate('Object.keys(S.battles)'):
        if pg.evaluate('id => isPC(S.battles[id])', bid):
            for g in pg.evaluate('pcGangIds()'): v.append({'label': bid + '/' + g, 'tab': 'battles', 'sel': bid, 'pcGang': g})
        else: v.append({'label': bid, 'tab': 'battles', 'sel': bid})
    return v


CONTROLS_JS = r"""
async ({ views, mode }) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const errs = []; let cur = '';
  window.addEventListener('error', e => errs.push(cur + ': ' + e.message));
  window.addEventListener('unhandledrejection', e => errs.push(cur + ': ' + (e.reason && e.reason.message || e.reason)));
  const ce = console.error; console.error = (...a) => { errs.push(cur + ': ' + a.map(String).join(' ').slice(0, 200)); ce(...a); };
  window.print = () => {}; window.confirm = () => true; window.alert = () => {}; window.prompt = () => null;
  const base = JSON.stringify({ gangs: S.gangs, battles: S.battles, territories: S.territories });
  const go = v => { S.tab = v.tab; S.sel = v.sel || null; if (v.gangView) { S.gangView = v.gangView; S.npcView = v.gangView; } if (v.pcGang) (S.pcTab ||= {})[v.sel] = v.pcGang; render(); };
  const reset = async v => { const d = JSON.parse(base); S.gangs = d.gangs; S.battles = d.battles; S.territories = d.territories; reindex(); AVAIL = null;
    S.confirmDel = null; S.copyText = ''; const o = document.querySelector('#print-overlay'); if (o) o.hidden = true; go(v); await sleep(10); };
  const pat = x => x.replace(/(^|\.)([a-z]+-)?[a-z0-9]*\d[a-z0-9-]*(?=\.|$)/gi, '$1*');
  const unsaved = () => { const out = [];
    for (const k of ['gangs', 'battles', 'territories']) for (const id of Object.keys(S[k])) {
      const a = stable(JSON.parse(JSON.stringify(S[k][id]))), b = window.__store[k + '/' + id] ? stable(window.__store[k + '/' + id]) : 'none'; if (a !== b) out.push(k + '/' + id); }
    return out; };
  const seen = new Set(), faults = [], notSaved = []; let done = 0;
  for (const v of views) {
    if (mode === 'click') {
      await reset(v);
      const sig = el => (v.sel ? (v.pcGang ? 'pc' : 'battle') : v.tab) + ':' + el.dataset.act + ':' + (el.dataset.kind || el.dataset.type || '');
      const n = document.querySelectorAll('#view [data-act]').length;
      for (let i = 0; i < n; i++) {
        await reset(v); const el = document.querySelectorAll('#view [data-act]')[i]; if (!el || el.dataset.act === 'loadBackup') continue;
        const k = sig(el); if (seen.has(k)) continue; seen.add(k); cur = v.label + ' → ' + el.dataset.act;
        el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); done++; await sleep(30);
        if (document.querySelector('#view').innerText.includes("couldn't be shown")) faults.push(cur);
      }
    } else {
      go(v); await sleep(300);
      const n = document.querySelectorAll('#view [data-bind]').length;
      for (let i = 0; i < n; i++) {
        const el = document.querySelectorAll('#view [data-bind]')[i]; if (!el) continue;
        const k = pat(el.dataset.bind) + ':' + (el.tagName === 'SELECT' ? 'sel' : el.type); if (seen.has(k)) continue; seen.add(k); cur = v.label + ' → ' + el.dataset.bind;
        if (el.tagName === 'SELECT') { const o = [...el.options].find(o => o.value && o.value !== el.value); if (!o) continue; el.value = o.value; el.dispatchEvent(new Event('change', { bubbles: true })); }
        else if (el.type === 'checkbox') { el.checked = !el.checked; el.dispatchEvent(new Event('change', { bubbles: true })); }
        else { el.value = el.type === 'number' ? '7' : el.type === 'date' ? '2026-11-20' : (el.value || '') + 'z'; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }
        done++; await sleep(1100);
        if (document.querySelector('#view').innerText.includes("couldn't be shown")) faults.push(cur);
        const u = unsaved(); if (u.length) notSaved.push(cur + ' (' + u.join(', ') + ')');
      }
    }
  }
  return { done, errs, faults, notSaved };
}"""


def t_every_button():
    """Press every kind of button in every view (data put back after each): nothing may error or break the view."""
    pg = page(); r = pg.evaluate(CONTROLS_JS, {'views': views_of(pg), 'mode': 'click'})
    out = [] if r['done'] >= 80 else [f'only {r["done"]} kinds of button found']
    out += list(dict.fromkeys(r['errs']))[:10] + ['data-fault message after ' + f for f in r['faults'][:10]] + pg.errors; close(pg)
    return out


def t_every_field():
    """Change every kind of field in every view (Claude copy, stand-in database): nothing may error, and the change
    must reach the database."""
    pg = page('claude'); r = pg.evaluate(CONTROLS_JS, {'views': views_of(pg), 'mode': 'fill'})
    out = [] if r['done'] > 150 else [f'only {r["done"]} kinds of field found']
    out += list(dict.fromkeys(r['errs']))[:10] + ['data-fault message after ' + f for f in r['faults'][:10]] + ['not saved: ' + f for f in r['notSaved'][:10]] + pg.errors; close(pg)
    return out


def t_report_text():
    """Every view, copied report and printout: no "undefined", "NaN", "[object Object]", "null", or a dash before a minus."""
    bad = re.compile(r'\bundefined\b|\bNaN\b|\[object Object\]|(?<![\w-])null(?![\w-])|– -\d')
    pg = page(); pg.evaluate('() => { window.print = () => {}; }'); out = []
    def scan(label, text):
        for m in bad.finditer(text or ''): out.append(f'{label}: …{text[max(0, m.start() - 40):m.end() + 20]}…'.replace('\n', ' | '))
    for v in views_of(pg):
        pg.evaluate("""v => { S.tab = v.tab; S.sel = v.sel || null; if (v.gangView) { S.gangView = v.gangView; S.npcView = v.gangView; } if (v.pcGang) (S.pcTab ||= {})[v.sel] = v.pcGang; S.copyText = ''; render(); }""", v)
        pg.wait_for_timeout(80)
        scan(v['label'], pg.evaluate("() => document.querySelector('#view').innerText + [...document.querySelectorAll('#view [title]')].map(e => e.title).join(' ')"))
        if pg.query_selector('#view [data-act="copyReport"]'):
            pg.evaluate("() => document.querySelector('#view [data-act=\"copyReport\"]').click()"); pg.wait_for_timeout(60)
            scan(v['label'] + ' copied text', pg.evaluate("S.copyText || ''"))
        for sel in ('#view [data-act="openPrint"]:not([data-all])', '#view [data-act="openPrint"][data-all="1"]'):
            if pg.query_selector(sel):
                pg.evaluate('s => document.querySelector(s).click()', sel); pg.wait_for_timeout(120)
                scan(v['label'] + ' printout', pg.evaluate("document.querySelector('#print-overlay')?.innerText || ''"))
                pg.evaluate("() => { const o = document.querySelector('#print-overlay'); if (o) o.hidden = true; }")
    out = list(dict.fromkeys(out))[:15] + pg.errors; close(pg)
    return out


FAIL_JS = """([code, times]) => { const od = db.doc.bind(db); let left = times;
  db.doc = p => { const r = od(p); if (p.startsWith('battles/')) { const s = r.set; r.set = async x => { if (left > 0) { left--; const e = new Error('x'); e.code = code; throw e; } return s(x); }; } return r; };
  if (db.runTransaction) { const rt = db.runTransaction.bind(db); db.runTransaction = async fn => { if (left > 0) { left--; const e = new Error('x'); e.code = code; throw e; } return rt(fn); }; } }"""


def t_failed_save():
    """A save that fails is kept, retried by itself and by Retry now, and closing warns meanwhile; when it finally goes
    through on the live site it is merged, so someone else's change made in the meantime survives. Not being allowed
    to edit is not retried. Hiding the page starts a waiting save at once."""
    out = []
    warns = "(() => { const e = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(e); return e.defaultPrevented; })()"
    for mode in ('hosted', 'claude'):
        pg = page(mode)
        bid = pg.evaluate('Object.keys(S.battles).find(id => !isPC(S.battles[id]))'); dbv = lambda f='post': pg.evaluate('([id, f]) => window.__store["battles/" + id][f]', [bid, f])
        pg.evaluate(FAIL_JS, ['unavailable', 2])
        pg.evaluate('id => { S.battles[id].post = "MINE"; save("battles", id); }', bid)
        try: pg.wait_for_selector('#save-retry', state='visible', timeout=4000)
        except Exception: pass
        if not pg.is_visible('#save-retry'): out.append(f'{mode}: no Retry now button after a failed save')
        if not pg.evaluate(warns): out.append(f'{mode}: closing the page with an unsaved change does not warn')
        pg.evaluate("""id => { const d = JSON.parse(JSON.stringify(window.__store['battles/' + id])); d.pre = 'THEIRS'; window.__remoteWrite('battles/' + id, d); }""", bid)
        pg.wait_for_timeout(8000)
        if dbv() != 'MINE': out.append(f'{mode}: the failed save was not retried by itself')
        if mode == 'hosted' and dbv('pre') != 'THEIRS': out.append(f'{mode}: the retried save overwrote a change someone else made meanwhile')
        if pg.evaluate(warns): out.append(f'{mode}: closing still warns after everything saved')
        pg.evaluate(FAIL_JS, ['internal', 1])
        pg.evaluate('id => { S.battles[id].post = "AGAIN"; save("battles", id); }', bid); pg.wait_for_timeout(1200)
        if pg.is_visible('#save-retry'): pg.click('#save-retry'); pg.wait_for_timeout(800)
        if dbv() != 'AGAIN': out.append(f'{mode}: Retry now did not save')
        pg.evaluate(FAIL_JS, ['permission-denied', 1])
        pg.evaluate('id => { S.battles[id].post = "NOPE"; save("battles", id); }', bid); pg.wait_for_timeout(4500)
        if dbv() == 'NOPE' or pg.is_visible('#save-retry'): out.append(f'{mode}: a save the account isn\'t allowed to make was retried')
        pg.evaluate('id => { S.battles[id].post = "HIDDEN"; save("battles", id); Object.defineProperty(document, "hidden", { value: true, configurable: true }); document.dispatchEvent(new Event("visibilitychange")); }', bid)
        pg.wait_for_timeout(300)
        if dbv() != 'HIDDEN': out.append(f'{mode}: hiding the page did not start the waiting save')
        out += pg.errors; close(pg)
    return out


def t_reconnect():
    """When the database ends a live listener, the log says it isn't up to date (a later save mustn't hide that),
    reconnects by itself, then has everyone's changes, including a record deleted meanwhile; a save made while
    disconnected keeps the other person's change. Withdrawn access shows as read-only and isn't retried."""
    out = []
    for mode in ('claude', 'hosted'):
        pg = page(mode)
        ids = pg.evaluate('Object.keys(S.battles).filter(id => !isPC(S.battles[id]))'); a, gone = ids[0], ids[1]
        pg.evaluate('() => window.__endListeners("battles", "unavailable")'); pg.wait_for_timeout(200)
        if 'reconnecting' not in pg.inner_text('#save-state'): out.append(f'{mode}: status after the listener ended: "{pg.inner_text("#save-state")}"')
        theirs = """([id, f, v]) => { const d = JSON.parse(JSON.stringify(window.__store['battles/' + id])); d[f] = v; window.__remoteWrite('battles/' + id, d); }"""
        pg.evaluate(theirs, [a, 'post', 'THEIRS'])
        pg.evaluate('id => { delete window.__store["battles/" + id]; }', gone)
        pg.evaluate('id => { S.battles[id].pre = "MINE"; save("battles", id); }', a); pg.wait_for_timeout(1000)
        if 'reconnecting' not in pg.inner_text('#save-state'): out.append(f'{mode}: a save while disconnected made the status say "{pg.inner_text("#save-state")}"')
        if pg.evaluate('id => window.__store["battles/" + id].post', a) != 'THEIRS': out.append(f'{mode}: a save while disconnected overwrote the other person’s change')
        pg.wait_for_timeout(3000)
        st = pg.evaluate('id => [S.battles[id].post, S.battles[id].pre]', a)
        if st != ['THEIRS', 'MINE']: out.append(f'{mode}: after reconnecting the log shows {st}, expected their post and my pre')
        if pg.evaluate('id => !!S.battles[id]', gone): out.append(f'{mode}: a battle deleted while disconnected is still shown')
        if pg.inner_text('#save-state').strip() != 'All changes saved': out.append(f'{mode}: status after reconnecting: "{pg.inner_text("#save-state")}"')
        pg.evaluate(theirs, [a, 'post', 'LIVE AGAIN']); pg.wait_for_timeout(400)
        if pg.evaluate('id => S.battles[id].post', a) != 'LIVE AGAIN': out.append(f'{mode}: changes still don’t arrive after reconnecting')
        if mode == 'claude':
            # reconnecting when the new listener delivers in pages: nothing may vanish meanwhile, the open battle stays open
            pg.evaluate("id => { S.tab = 'battles'; S.sel = id; render(); window.__pagedNext = { battles: true }; window.__endListeners('battles', 'unavailable'); }", a)
            counts = []
            for _ in range(14): pg.wait_for_timeout(250); counts.append(pg.evaluate('Object.keys(S.battles).length'))
            if min(counts) < max(counts): out.append(f'{mode}: while reconnecting page by page the battle list shrank to {min(counts)} of {max(counts)}')
            if pg.evaluate('S.sel') != a: out.append(f'{mode}: reconnecting page by page closed the battle being viewed')
            if pg.inner_text('#save-state').strip() != 'All changes saved': out.append(f'{mode}: after reconnecting page by page the status says "{pg.inner_text("#save-state")}"')
            pg.evaluate('() => window.__endListeners("gangs", "revoked")'); pg.wait_for_timeout(2500)
            if 'Read-only' not in pg.inner_text('#save-state'): out.append(f'{mode}: withdrawn access shows "{pg.inner_text("#save-state")}"')
            n = pg.evaluate('() => (window.__listeners.gangs || []).length')
            if n != 1: out.append(f'{mode}: after access was withdrawn the log subscribed again ({n} listeners)')
        out += pg.errors; close(pg)
    return out


def t_save_error_codes():
    """Each kind of save error: the ones that can never succeed aren't retried and say what's wrong; busy, full and
    connection errors are retried and say which it is."""
    out = []
    cases = [('revoked', False, 'access to this log changed'), ('not_granted', False, "isn't allowed to save"), ('capability_disabled', False, "isn't available"),
             ('transform_error', False, "couldn't be prepared"), ('invalid_argument', False, 'view this log but not edit'), ('unauthenticated', False, 'signed out'),
             ('resource_exhausted', True, 'too many saves at once'), ('quota_exceeded', True, 'storage is full'), ('resource-exhausted', True, 'daily limit'), ('unavailable', True, 'connection problem')]
    pg = page('claude')
    bid = pg.evaluate('Object.keys(S.battles).find(id => !isPC(S.battles[id]))')
    for code, retried, words in cases:
        pg.evaluate(FAIL_JS, [code, 3])
        pg.evaluate('([id, v]) => { S.battles[id].post = v; save("battles", id); }', [bid, 'try ' + code])
        try: pg.wait_for_function('() => document.querySelector("#save-text").textContent.startsWith("Not saved")', timeout=5000)
        except Exception: pass
        text, shown = pg.inner_text('#save-text'), pg.is_visible('#save-retry')
        if words not in text: out.append(f'{code}: status says "{text}"')
        if shown != retried: out.append(f'{code}: {"no retry scheduled" if retried else "retried although it can never succeed"}')
        pg.evaluate('() => { for (const k of Object.keys(retryT)) { clearTimeout(retryT[k]); delete retryT[k]; } for (const k of Object.keys(dirty)) dirty[k] = false; }')
        pg.evaluate(FAIL_JS, [code, 0])
    out += pg.errors; close(pg)
    return out


def t_redraw_speed():
    """Sizing the notes boxes after a redraw happens in one go: on a phone-speed processor (4× slower) it takes under
    25 ms for every box on the Gangs tab and on a battle (one box at a time took 130–210 ms), and every box gets exactly
    the height that sizing them one at a time gives."""
    out = []
    pg = page(width=390, height=844)
    cdp = pg.context.new_cdp_session(pg); cdp.send('Emulation.setCPUThrottlingRate', {'rate': 4})
    bid = pg.evaluate('Object.values(S.battles).filter(b => !isPC(b)).sort((a, b) => b.date.localeCompare(a.date))[0].id')
    for label, setup in (('Gangs tab', "S.tab = 'gangs'"), ('newest battle', f"S.tab = 'battles'; S.sel = '{bid}'")):
        r = pg.evaluate(f"""() => {{ {setup}; render(); const v = document.querySelector('#view'), tas = [...v.querySelectorAll('textarea')];
          const one = tas.map(t => {{ t.style.height = ''; fitTextarea(t); return t.style.height; }});
          tas.forEach(t => t.style.height = ''); document.body.offsetHeight;
          const t0 = performance.now(); fitTextareas(tas, () => 0); const ms = performance.now() - t0;
          const differ = tas.filter((t, i) => t.style.height !== one[i]).length;
          return {{ ms, differ, n: tas.length }}; }}""")
        if r['ms'] > 25: out.append(f'{label}: sizing {r["n"]} notes boxes takes {round(r["ms"])} ms on a phone-speed processor')
        if r['differ']: out.append(f'{label}: {r["differ"]} of {r["n"]} notes boxes get a different height than before')
    cdp.send('Emulation.setCPUThrottlingRate', {'rate': 1})
    out += pg.errors; close(pg)
    return out


def t_status_while_saving():
    """The status line never says "All changes saved" while a change here is still waiting to save or being retried,
    even when other people's changes arrive meanwhile; it does once the change has saved."""
    out = []
    theirs = """([coll, id]) => { const d = JSON.parse(JSON.stringify(window.__store[coll + '/' + id])); d.note = 'THEIRS ' + Date.now(); window.__remoteWrite(coll + '/' + id, d); }"""
    for mode in ('hosted', 'claude'):
        pg = page(mode)
        bid = pg.evaluate('Object.keys(S.battles).find(id => !isPC(S.battles[id]))')
        other = pg.evaluate('id => Object.keys(S.battles).find(x => x !== id && !isPC(S.battles[x]))', bid)
        gid = pg.evaluate('Object.keys(S.gangs)[0]')
        txt = lambda: pg.inner_text('#save-text')
        # a change waiting out the pause before it saves
        pg.evaluate('id => { S.battles[id].post = "MINE"; save("battles", id); }', bid)
        pg.evaluate(theirs, ['battles', other]); pg.wait_for_timeout(100)
        if txt() == 'All changes saved': out.append(f'{mode}: says all saved while a change is waiting to save')
        pg.wait_for_timeout(1500)
        if txt() != 'All changes saved': out.append(f'{mode}: after the change saved the status says {txt()!r}')
        # a save that fails and is being retried
        pg.evaluate(FAIL_JS, ['unavailable', 50])
        pg.evaluate('id => { S.battles[id].post = "AGAIN"; save("battles", id); }', bid)
        try: pg.wait_for_selector('#save-retry', state='visible', timeout=6000)
        except Exception: out.append(f'{mode}: the failing save never showed Retry now')
        for coll, id in (('battles', other), ('gangs', gid)):
            pg.evaluate(theirs, [coll, id]); pg.wait_for_timeout(300)
            if txt() == 'All changes saved': out.append(f'{mode}: says all saved while a failed save is being retried (after a {coll} change arrived)')
        out += pg.errors; close(pg)
    return out


ODD_RECORDS = [
    ('battles', {'id': 'b-odd-gangs', 'date': '2026-11-02', 'gangs': 'kyn'}),
    ('battles', {'id': 'b-odd-events', 'date': '2026-11-02', 'gangs': ['kyn', 'fangs'], 'events': {}}),
    ('battles', {'id': 'b-odd-rewards', 'date': '2026-11-02', 'gangs': ['kyn', 'fangs'], 'rewards': [None]}),
    ('battles', {'id': 'b-odd-xp', 'date': '2026-11-02', 'gangs': ['kyn', 'fangs'], 'events': [None, {'id': 'e1', 'by': 'ghost', 'action': 'zzz'}], 'injuries': [None]}),
    ('battles', {'id': 'pc-odd', 'type': 'postcycle', 'date': '2026-11-03', 'sheets': {'kyn': {'actions': [{'id': 'a1', 'kind': 'nonsense', 'crew': None}, None]}, 'zzz': None}}),
    ('gangs', {'id': 'g-odd', 'name': 'Odd', 'fighters': [None, {'name': 'no id'}, {'id': 'g-odd-a', 'injuries': 'Hobbled'}]}),
]


def t_odd_records():
    """One record of an unexpected shape (a list stored as text or an object, empty entries in a list) never stops the log
    showing: every tab and battle still shows, whether the record is there on opening, arrives from someone else, or comes in
    a backup file. If the gang strip itself fails, the rest of the log still shows. Records of the right shape are untouched."""
    import copy
    out = []
    views = "() => { const v = []; for (const t of ['battles','gangs','npcs','territories','stats','equipment','rules']) v.push([t, null]); for (const id of Object.keys(S.battles)) v.push(['battles', id]); return v; }"
    def check(pg, label):
        if pg.evaluate('!S.loaded') or 'Loading the campaign log' in pg.inner_text('#view'): out.append(f'{label}: stuck on Loading'); return
        for t, sel in pg.evaluate(views):
            err = pg.evaluate("([t, s]) => { try { S.tab = t; S.sel = s; render(); return ''; } catch (e) { return String(e); } }", [t, sel])
            txt = pg.inner_text('#view')
            if err or "couldn't be shown" in txt: out.append(f'{label}: {t}/{sel} failed {err or txt[:120]!r}'); return
    for kind, rec in ODD_RECORDS:
        for mode in ('local', 'claude', 'hosted'):
            d = copy.deepcopy(DATA); d[kind][rec['id']] = copy.deepcopy(rec)
            pg = page(mode, data=d); pg.wait_for_timeout(300)
            check(pg, f'{mode}, {rec["id"]} there on opening')
            if mode != 'local': out += [f'{mode} {rec["id"]}: {e}' for e in pg.errors]
            close(pg)
        pg = page('claude')
        pg.evaluate('([p, d]) => window.__remoteWrite(p, d)', [f'{kind}/{rec["id"]}', rec]); pg.wait_for_timeout(400)
        check(pg, f'{rec["id"]} arriving from someone else'); out += [f'arriving {rec["id"]}: {e}' for e in pg.errors]; close(pg)
    # a backup file holding all of them, plus entries that aren't records at all
    bk = copy.deepcopy(DATA); bk['app'] = 'long-salvage-log'; bk['version'] = 1
    for kind, rec in ODD_RECORDS: bk[kind][rec['id']] = copy.deepcopy(rec)
    bk['battles']['b-null'] = None; bk['gangs']['g-text'] = 'just text'
    path = '/tmp/odd-backup.json'; open(path, 'w').write(json.dumps(bk))
    pg = page('local')
    with pg.expect_file_chooser() as fc: pg.evaluate("document.querySelector('[data-act=loadBackup]').click()")
    fc.value.set_files(path); pg.wait_for_timeout(400)
    if not pg.is_visible('#restore-confirm'): out.append('odd backup: the compare screen did not open')
    else:
        pg.click('[data-act=replaceRestore]'); pg.wait_for_timeout(400); check(pg, 'odd backup loaded')
        if pg.evaluate("'b-null' in S.battles || 'g-text' in S.gangs"): out.append('odd backup: entries that are not records were taken in')
    out += [f'backup: {e}' for e in pg.errors]; close(pg)
    # the gang strip failing on its own
    pg = page('claude')
    err = pg.evaluate("() => { window.battleGangDeltas = () => { throw new Error('strip test'); }; try { S.tab = 'gangs'; render(); return ''; } catch (e) { return String(e); } }"); pg.wait_for_timeout(100)
    if err: out.append(f'strip failing: drawing the page stopped ({err})')
    txt = pg.inner_text('#view')
    if "couldn't be shown" in txt or 'Loading' in txt or len(txt) < 200: out.append(f'strip failing: the Gangs tab did not show ({txt[:80]!r})')
    close(pg)
    # records of the right shape are left exactly as they are
    pg = page('local')
    same = pg.evaluate("d => { if (typeof tidyRec !== 'function') return ['(no tidying at all)']; const out = []; for (const k of ['gangs','battles','territories']) for (const [id, r] of Object.entries(d[k])) { const c = JSON.parse(JSON.stringify(r)); if (tidyRec(c) || JSON.stringify(c) !== JSON.stringify(r)) out.push(k + '/' + id); } return out; }", DATA)
    if same: out.append(f'tidying changed records that were fine: {same[:5]}')
    close(pg)
    return out


def t_activity_owner():
    """Live site: Log record entries carry the signed-in account's email (which the database rules check), and the example
    rules only let an entry be added under your own account and never change the account on it."""
    out = []
    pg = page('hosted')
    bid = pg.evaluate('Object.keys(S.battles).find(id => !isPC(S.battles[id]))')
    pg.evaluate('id => { S.battles[id].post = "logged"; save("battles", id); }', bid); pg.wait_for_timeout(1500)
    pg.evaluate('id => { S.battles[id].post = "logged again"; save("battles", id); }', bid); pg.wait_for_timeout(1500)
    acts = pg.evaluate("Object.entries(window.__store).filter(([p]) => p.startsWith('activity/')).map(([p, v]) => v)")
    if not acts: out.append('no Log record entry was written')
    for a in acts:
        if a.get('email') != 't@example.com': out.append(f'entry email is {a.get("email")!r}, not the signed-in account')
    out += pg.errors; close(pg)
    rules = open(os.path.join(ROOT, 'firestore.rules.example')).read()
    block = rules[rules.index('match /activity/'):]
    if 'allow create: if isPlayer() && request.resource.data.email == request.auth.token.email' not in block: out.append('rules: adding an entry is not limited to your own account')
    if 'request.resource.data.email == resource.data.email' not in block: out.append('rules: the account on an entry can be changed')
    if re.search(r'allow [^;]*\bcreate\b[^;]*: if isPlayer\(\);', block): out.append('rules: anyone may still add entries under any name')
    return out


TESTS = {'syntax': t_syntax, 'equipment': t_equipment, 'every-view': t_every_view, 'injection': t_injection,
         'typing-during-save': t_typing_during_save, 'two-editors': t_two_editors, 'critical-injury': t_critical_injury,
         'map-setup': t_map_setup, 'missing-gang': t_missing_gang, 'log-record-purchases': t_log_record_purchases, 'laptop-build': t_laptop_build,
         'change-back': t_change_back, 'restore-point': t_restore_point, 'munda-same-day': t_munda_same_day,
         'failed-save': t_failed_save, 'report-text': t_report_text, 'every-button': t_every_button, 'every-field': t_every_field,
         'reconnect': t_reconnect, 'save-error-codes': t_save_error_codes, 'redraw-speed': t_redraw_speed,
         'status-while-saving': t_status_while_saving,
         'odd-records': t_odd_records, 'activity-owner': t_activity_owner}

failed = 0
for name, fn in TESTS.items():
    if ONLY and name not in ONLY:
        continue
    try:
        problems = fn()
    except Exception:
        problems = ['test crashed: ' + traceback.format_exc(limit=3)]
    failed += bool(problems)
    print(('FAIL ' if problems else 'ok   ') + name)
    for p in problems[:15]:
        print('       - ' + p)
BROWSER.close(); PW.stop(); shutil.rmtree(TMP, ignore_errors=True)
print(f'\n{len([n for n in TESTS if not ONLY or n in ONLY]) - failed} passed, {failed} failed')
sys.exit(1 if failed else 0)
