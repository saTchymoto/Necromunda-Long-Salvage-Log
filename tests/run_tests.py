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
        return { ...q, onSnapshot: (...a) => { const fn = a.find(x => typeof x === 'function'); setTimeout(() => fn({ metadata: { fromCache: true }, docChanges: () => [] }), 5);
          setTimeout(() => fn({ metadata: { fromCache: false }, docChanges: () => Object.entries(terr).map(([k, v]) => ({ type: 'added', doc: { id: k.split('/')[1], exists: true, data: () => JSON.parse(JSON.stringify(v)) } })) }), 900);
          return () => {}; } }; };
      window.firebase.firestore = () => fs; })();"""
    empty = """(() => { for (const k of Object.keys(window.__store)) if (k.startsWith('territories/')) delete window.__store[k];
      const fs = window.firebase.firestore(), orig = fs.collection; let first = true;
      fs.collection = c => { const q = orig(c); if (c !== 'territories' || !first) return q; first = false;
        return { ...q, onSnapshot: (...a) => { const fn = a.find(x => typeof x === 'function'); setTimeout(() => fn({ metadata: { fromCache: true }, docChanges: () => [] }), 5); return () => {}; } }; };
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
        if pg.is_visible('#save-retry'): pg.click('#save-retry'); pg.wait_for_timeout(2500)  # Claude copy: may wait for its own lease to lapse
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
    # Load backup must ask for the file picker; the file is then handed to it directly (headless Chromium sometimes doesn't
    # report the picker opening, though the page asked for it correctly).
    asked = pg.evaluate("() => { const fi = document.getElementById('backup-file'); let n = 0; fi.click = () => { n++; }; document.querySelector('[data-act=loadBackup]').click(); return n; }")
    if asked != 1: out.append('odd backup: Load backup did not open the file picker')
    pg.set_input_files('#backup-file', path); pg.wait_for_timeout(400)
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


def t_messages():
    """Messages at the bottom stay up long enough to read; a long one isn't replaced by the next message straight away (the next
    follows it); clicking dismisses; each message is also put where screen readers read it out."""
    out = []
    pg = page('claude')
    clash = "Someone else edited this at the same time. Their other changes were merged in; where you both changed the same thing, yours was kept."
    if pg.evaluate('m => toastMs(m)', clash) < 6000: out.append('the merge message is up for less than 6 s')
    if pg.evaluate('toastMs("Saved")') < 2200: out.append('short messages are up for less than 2.2 s')
    pg.evaluate('m => toast(m)', clash); pg.wait_for_timeout(600)
    pg.evaluate('toast("Kyn: +1 XP")'); pg.wait_for_timeout(100)
    if pg.inner_text('#toast') != clash: out.append('the long message was replaced straight away')
    if 'merged in' not in pg.inner_text('#sr-status'): out.append('the long message was not put where screen readers read it')
    pg.wait_for_timeout(4000)
    if not pg.is_visible('#toast') or pg.inner_text('#toast') != clash: out.append('the long message went before 4.5 s')
    pg.wait_for_timeout(2600)
    if pg.inner_text('#toast') != 'Kyn: +1 XP' or not pg.is_visible('#toast'): out.append(f'the waiting message did not follow: {pg.inner_text("#toast")!r}')
    if pg.inner_text('#sr-status').strip() != 'Kyn: +1 XP': out.append('the following message was not read out')
    pg.evaluate('m => toast(m)', clash); pg.wait_for_timeout(200); pg.click('#toast'); pg.wait_for_timeout(100)
    if pg.is_visible('#toast'): out.append('clicking did not dismiss the message')
    pg.evaluate('toast("One")'); pg.wait_for_timeout(100); pg.evaluate('toast("Two")'); pg.wait_for_timeout(100)
    if pg.inner_text('#toast') != 'Two': out.append('a short message held up the next one')
    pg.wait_for_timeout(2500)
    if pg.is_visible('#toast'): out.append('a short message stayed up too long')
    out += pg.errors; close(pg)
    return out


def t_panels_keyboard():
    """Log record, Restore points, the backup compare screen and the print preview: opening one moves keyboard focus into it,
    Escape closes it (a backup: same as Cancel), and focus goes back to the button it was opened from. Tab stays inside the
    print preview."""
    import copy
    out = []
    pg = page('hosted')
    inside = "sel => { const p = document.querySelector(sel); return !!p && (p === document.activeElement || p.contains(document.activeElement)); }"
    focused = "() => { const a = document.activeElement; return a ? (a.id || a.dataset.act || a.tagName) : ''; }"
    for btn, label in (('#act-btn', 'Log record'), ('#rp-btn', 'Restore points')):
        pg.wait_for_selector(btn, state='visible', timeout=12000)
        pg.focus(btn); pg.keyboard.press('Enter'); pg.wait_for_timeout(600)
        if not pg.evaluate(inside, '#restore-confirm'): out.append(f'{label}: focus did not move into the panel ({pg.evaluate(focused)})')
        pg.keyboard.press('Escape'); pg.wait_for_timeout(100)
        if pg.is_visible('#restore-confirm'): out.append(f'{label}: Escape did not close it')
        if pg.evaluate(focused) != btn[1:]: out.append(f'{label}: focus went to {pg.evaluate(focused)!r}, not back to its button')
    # backup compare: Escape is Cancel; Apply puts focus back too
    bk = copy.deepcopy(DATA); bk['app'] = 'long-salvage-log'; bk['version'] = 1; bk['saved'] = '2026-11-20T10:00:00Z'
    bk['gangs']['kyn']['credits'] = 999; bk['gangs']['kyn']['updated'] = '2099-01-01T00:00:00Z'
    path = '/tmp/kb-backup.json'; open(path, 'w').write(json.dumps(bk))
    for how in ('Escape', 'apply'):
        # Enter on Load backup must ask for the file picker; the file is then handed to it directly (headless Chromium
        # sometimes doesn't report the picker opening, though the page asked for it correctly).
        pg.evaluate("() => { const fi = document.getElementById('backup-file'); if (fi.__asked !== undefined) return; fi.__asked = 0; const oc = fi.click.bind(fi); fi.click = () => { fi.__asked++; if (navigator.userActivation.isActive) oc(); }; }")
        pg.focus('[data-act=loadBackup]'); before = pg.evaluate("document.getElementById('backup-file').__asked")
        pg.keyboard.press('Enter'); pg.wait_for_timeout(200)
        if pg.evaluate("document.getElementById('backup-file').__asked") != before + 1: out.append(f'backup ({how}): Enter on Load backup did not open the file picker')
        pg.set_input_files('#backup-file', path); pg.wait_for_timeout(500)
        if not pg.evaluate(inside, '#restore-confirm'): out.append(f'backup ({how}): focus did not move into the compare screen')
        if how == 'Escape':
            pg.keyboard.press('Escape'); pg.wait_for_timeout(100)
            if pg.evaluate('pendingRestore !== null') or pg.is_visible('#restore-confirm'): out.append('backup: Escape did not cancel')
        else:
            pg.focus('#restore-confirm [data-act=confirmRestore]'); pg.keyboard.press('Enter'); pg.wait_for_timeout(300)
        if pg.evaluate(focused) != 'loadBackup': out.append(f'backup ({how}): focus went to {pg.evaluate(focused)!r}, not back to Load backup')
    # print preview
    bid = pg.evaluate('Object.keys(S.battles).find(id => !isPC(S.battles[id]))')
    pg.evaluate('id => { S.tab = "battles"; S.sel = id; render(); }', bid); pg.wait_for_timeout(200)
    pg.focus('#view [data-act=openPrint]'); pg.keyboard.press('Enter'); pg.wait_for_timeout(300)
    for k in ['Tab'] * 5 + ['Shift+Tab'] * 5:
        pg.keyboard.press(k)
        if not pg.evaluate(inside, '#print-overlay'): out.append(f'print: {k} left the preview ({pg.evaluate(focused)})'); break
    pg.keyboard.press('Escape'); pg.wait_for_timeout(100)
    if pg.is_visible('#print-overlay'): out.append('print: Escape did not close it')
    if pg.evaluate(focused) != 'openPrint': out.append(f'print: focus went to {pg.evaluate(focused)!r}, not back to Print report')
    out += pg.errors; close(pg)
    return out


ON_TOP = "sel => { const t = document.querySelector(sel); if (!t || !t.getClientRects().length) return 'not shown'; const r = t.getBoundingClientRect(); const e = document.elementFromPoint(r.left + r.width / 2, Math.min(r.top + r.height / 2, innerHeight - 2)); return e === t || t.contains(e) ? 'on top' : 'hidden behind ' + ((e && (e.id || e.className)) || '?'); }"


def t_print_feedback():
    """From the print preview: messages show on top of it, and when the browser blocks copying, Copy as text shows the report
    text, selected, inside the preview. Reopening the preview starts without it. Printing leaves it out."""
    out = []
    for mode in ('claude', 'hosted'):
        pg = page(mode)
        bid = pg.evaluate('Object.keys(S.battles).find(id => !isPC(S.battles[id]))')
        pg.evaluate('id => { S.tab = "battles"; S.sel = id; render(); }', bid); pg.wait_for_timeout(200)
        pg.click('#view [data-act=openPrint]'); pg.wait_for_timeout(200)
        pg.evaluate('toast("A message")'); pg.wait_for_timeout(100)
        if pg.evaluate(ON_TOP, '#toast') != 'on top': out.append(f'{mode}: a message is {pg.evaluate(ON_TOP, "#toast")} over the print preview')
        pg.evaluate("() => { navigator.clipboard.writeText = () => Promise.reject(new Error('blocked')); }")
        pg.click('#print-overlay [data-act=copyReport]'); pg.wait_for_timeout(300)
        where = pg.evaluate(ON_TOP, '#print-copybox')
        if where != 'on top': out.append(f'{mode}: copying blocked, the report text is {where}')
        else:
            if pg.evaluate("document.activeElement.id") != 'print-copybox': out.append(f'{mode}: the report text box was not focused')
            sel = pg.evaluate("(() => { const t = document.getElementById('print-copybox'); return t.selectionEnd - t.selectionStart; })()")
            if sel < 50 or sel != pg.evaluate("document.getElementById('print-copybox').value.length"): out.append(f'{mode}: the report text was not all selected ({sel})')
            if 'PRE BATTLE' not in pg.evaluate("document.getElementById('print-copybox').value"): out.append(f'{mode}: the box does not hold the report text')
        pg.emulate_media(media='print')
        if pg.evaluate("(() => { const e = document.getElementById('print-copy'); return e.getClientRects().length > 0; })()"): out.append(f'{mode}: the copy box would be printed')
        pg.emulate_media(media='screen')
        pg.keyboard.press('Escape'); pg.wait_for_timeout(100)
        pg.click('#view [data-act=openPrint]'); pg.wait_for_timeout(200)
        if pg.is_visible('#print-copy'): out.append(f'{mode}: the copy box was still there when the preview was reopened')
        out += pg.errors; close(pg)
    return out


def t_message_size():
    """A long message uses the screen's width on a phone (not half of it) and stays a readable width on a large screen."""
    out = []
    clash = "Someone else edited this at the same time. Their other changes were merged in; where you both changed the same thing, yours was kept."
    for w, h in ((320, 640), (390, 844), (1400, 900)):
        pg = page('claude', width=w, height=h)
        pg.evaluate('m => toast(m)', clash); pg.wait_for_timeout(100)
        r = pg.evaluate("(() => { const r = document.getElementById('toast').getBoundingClientRect(); return [r.left, r.right, r.width, r.height]; })()")
        if r[0] < 8 or r[1] > w - 8: out.append(f'{w}px: the message runs off the screen ({r})')
        if w < 600 and r[2] < w - 60: out.append(f'{w}px: the message is only {round(r[2])}px wide')
        if w < 600 and r[3] > 120: out.append(f'{w}px: the message is {round(r[3])}px tall')
        if w >= 600 and r[2] > 600: out.append(f'{w}px: the message is {round(r[2])}px wide, too wide to read comfortably')
        out += pg.errors; close(pg)
    return out


def t_sign_out():
    """Live site: signing out (and Use a different account) clears the copy of the log the browser keeps, then signs out."""
    out = []
    pg = page('hosted')
    pg.evaluate("sessionStorage.removeItem('__fbcalls')")
    pg.evaluate("document.querySelector('[data-act=signOut]').click()"); pg.wait_for_timeout(1500)
    calls = pg.evaluate("JSON.parse(sessionStorage.getItem('__fbcalls') || '[]')")
    if calls[:3] != ['terminate', 'clearPersistence', 'signOut']: out.append(f'Sign out did: {calls}')
    pg.evaluate("sessionStorage.removeItem('__fbcalls')")
    pg.evaluate("document.getElementById('gate-other').onclick()"); pg.wait_for_timeout(1500)
    calls = pg.evaluate("JSON.parse(sessionStorage.getItem('__fbcalls') || '[]')")
    if calls[:3] != ['terminate', 'clearPersistence', 'signOut']: out.append(f'Use a different account did: {calls}')
    # clearing fails (another tab still open): signing out still happens
    pg.evaluate("sessionStorage.removeItem('__fbcalls')")
    pg.evaluate("() => { const fs = window.firebase.firestore(); fs.clearPersistence = async () => { throw Object.assign(new Error('open elsewhere'), { code: 'failed-precondition' }); }; document.querySelector('[data-act=signOut]').click(); }"); pg.wait_for_timeout(1500)
    calls = pg.evaluate("JSON.parse(sessionStorage.getItem('__fbcalls') || '[]')")
    if 'signOut' not in calls: out.append(f'when clearing failed, signing out did not happen: {calls}')
    out += [e for e in pg.errors if 'open elsewhere' not in e]; close(pg)
    return out


def t_claude_no_db():
    """Claude copy that can't reach its database (not signed in, not allowed, or no answer in time): a clear screen with a
    Reload button, never an empty log that saves in this browser. The live site's own offline screen has Reload too."""
    out = []
    for label, use in (('database refused', 'async n => null'), ('no answer for a while', 'n => new Promise(ok => setTimeout(() => ok(null), 1500))'),
                       ('asking failed', 'async n => { throw new Error("not served"); }')):
        pg = page('claude', extra=f'window.claude = {{ use: {use} }};'); pg.wait_for_timeout(1800)
        if not pg.is_visible('#gate'): out.append(f'{label}: no screen saying the log can\'t be reached (mode {pg.evaluate("S.mode")})')
        else:
            t = pg.inner_text('#gate-text')
            if 'shared database' not in t or 'signed in to Claude' not in t: out.append(f'{label}: the screen says {t!r}')
            if not pg.is_visible('#gate-reload'): out.append(f'{label}: no Reload button')
            if pg.is_visible('#gate-signin') or pg.is_visible('#gate-other'): out.append(f'{label}: Google sign-in buttons shown')
        if pg.evaluate('S.mode') == 'local' or pg.evaluate('localStorage.getItem("long-salvage-log-v1")'): out.append(f'{label}: fell back to saving in this browser')
        if label == 'database refused' and pg.is_visible('#gate-reload'):
            pg.evaluate("sessionStorage.setItem('reloaded', '1')"); pg.click('#gate-reload'); pg.wait_for_timeout(800)
            if pg.evaluate("performance.getEntriesByType('navigation')[0].type") != 'reload': out.append('Reload did not reload the page')
        out += pg.errors; close(pg)
    # the laptop copy (no Claude, no Firebase) still works from the browser
    pg = page('local')
    if pg.is_visible('#gate') or pg.evaluate('S.mode') != 'local' or not pg.evaluate('Object.keys(S.battles).length'): out.append('the laptop copy no longer opens on its own')
    out += pg.errors; close(pg)
    # live site offline screen offers Reload
    pg = page('hosted', extra='window.firebase.firestore = () => { throw new Error("offline"); };'); pg.wait_for_timeout(500)
    if not pg.is_visible('#gate'): out.append('live site without its database: no screen')
    elif not pg.is_visible('#gate-reload') or pg.is_visible('#gate-signin'): out.append('live site offline screen: Reload missing or sign-in shown')
    close(pg)
    return out


def t_no_access():
    """Claude copy refused access before the log loaded: a screen saying so with Reload, not "Loading…" for ever. Losing access
    once the log is showing keeps it on screen, marked read-only."""
    out = []
    refuse = """const __c = __fs.collection.bind(__fs); __fs.collection = n => { const q = __c(n); q.onSnapshot = (ok, bad) => { setTimeout(() => bad(Object.assign(new Error('no'), { code: '%s' })), 50); return () => {}; }; return q; };"""
    for code in ('not_granted', 'revoked'):
        pg = page('claude', extra=refuse % code); pg.wait_for_timeout(800)
        if not pg.is_visible('#gate'): out.append(f'{code} on opening: no screen ({pg.inner_text("#view")[:40]!r}, status {pg.inner_text("#save-text")!r})')
        else:
            if "don't have access" not in pg.inner_text('#gate-text'): out.append(f'{code}: the screen says {pg.inner_text("#gate-text")!r}')
            if not pg.is_visible('#gate-reload') or pg.is_visible('#gate-signin'): out.append(f'{code}: Reload missing or sign-in shown')
        out += pg.errors; close(pg)
    pg = page('claude'); pg.evaluate("window.__endListeners('battles', 'revoked')"); pg.wait_for_timeout(500)
    if pg.is_visible('#gate'): out.append('losing access part-way through hid the log')
    if 'Read-only' not in pg.inner_text('#save-text'): out.append(f'losing access part-way through: status {pg.inner_text("#save-text")!r}')
    out += pg.errors; close(pg)
    return out


def t_tab_keys():
    """The tab bar behaves like tabs: Left/Right (wrapping) and Home/End move between tabs and open them, only the selected tab
    is a Tab stop, and the content is labelled by the selected tab. Clicking still works."""
    out = []
    pg = page('claude')
    names = pg.evaluate("[...document.querySelectorAll('#tabs [role=tab]')].map(b => b.dataset.tab)")
    state = lambda: pg.evaluate("[S.tab, document.activeElement.dataset.tab, document.getElementById('view').getAttribute('aria-labelledby'), [...document.querySelectorAll('#tabs [role=tab]')].filter(b => b.tabIndex === 0).map(b => b.dataset.tab), document.querySelector('#tabs [aria-selected=true]').dataset.tab]")
    def expect(t, how):
        st = state()
        if st != [t, t, 'tab-' + t, [t], t]: out.append(f'{how}: expected {t}, got {st}')
    pg.evaluate("openTab('battles')"); pg.focus('#tab-battles')
    pg.keyboard.press('ArrowRight'); expect(names[1], 'Right')
    pg.keyboard.press('ArrowLeft'); expect(names[0], 'Left')
    pg.keyboard.press('ArrowLeft'); expect(names[-1], 'Left from the first wraps')
    pg.keyboard.press('ArrowRight'); expect(names[0], 'Right from the last wraps')
    pg.keyboard.press('End'); expect(names[-1], 'End')
    pg.keyboard.press('Home'); expect(names[0], 'Home')
    if pg.evaluate("document.getElementById('view').getAttribute('role')") != 'tabpanel': out.append('the content is not marked as the tab panel')
    if pg.evaluate("[...document.querySelectorAll('#tabs [role=tab]')].some(b => b.getAttribute('aria-controls') !== 'view' || !document.getElementById(b.id) || b.id !== 'tab-' + b.dataset.tab)"): out.append('a tab is not linked to the content')
    pg.click('#tab-territories'); pg.wait_for_timeout(100)
    if pg.evaluate('S.tab') != 'territories' or pg.evaluate("document.getElementById('view').getAttribute('aria-labelledby')") != 'tab-territories': out.append('clicking a tab no longer opens it')
    # arrow keys inside the content don't move tabs
    pg.evaluate("openTab('gangs')"); pg.focus('#view input, #view textarea, #view select'); pg.keyboard.press('ArrowRight')
    if pg.evaluate('S.tab') != 'gangs': out.append('arrow keys in a field changed the tab')
    out += pg.errors; close(pg)
    return out


def t_firestore_restart():
    """Live site, with the stand-in behaving like Firebase keeping its copy in the browser: after a live connection is cut off
    and started again with nothing changed meanwhile, the status goes back to "All changes saved"; changes still arrive and
    saves still work. The Claude copy is unaffected."""
    out = []
    pg = page('hosted', extra='window.__firestoreCache = true;')
    bid = pg.evaluate('Object.keys(S.battles).find(id => !isPC(S.battles[id]))')
    for coll in ('battles', 'gangs'):
        pg.evaluate('c => window.__endListeners(c, "internal")', coll); pg.wait_for_timeout(300)
        if 'Not up to date' not in pg.inner_text('#save-text'): out.append(f'{coll}: cut off, the status says {pg.inner_text("#save-text")!r}')
        pg.wait_for_timeout(4000)
        if pg.inner_text('#save-text') != 'All changes saved' or not pg.evaluate('c => live[c]', coll): out.append(f'{coll}: 4 s after starting again the status says {pg.inner_text("#save-text")!r}')
    pg.evaluate("""id => { const d = JSON.parse(JSON.stringify(window.__store['battles/' + id])); d.pre = 'THEIRS-AFTER'; window.__remoteWrite('battles/' + id, d); }""", bid); pg.wait_for_timeout(300)
    if pg.evaluate('id => S.battles[id].pre', bid) != 'THEIRS-AFTER': out.append("someone else's change didn't arrive after starting again")
    pg.evaluate('id => { S.battles[id].post = "MINE-AFTER"; save("battles", id); }', bid); pg.wait_for_timeout(1500)
    if pg.evaluate("id => window.__store['battles/' + id].post", bid) != 'MINE-AFTER': out.append("a save after starting again didn't reach the database")
    if pg.inner_text('#save-text') != 'All changes saved': out.append(f'after saving: {pg.inner_text("#save-text")!r}')
    out += pg.errors; close(pg)
    return out


def t_old_safari_restore():
    """A browser that can't unpack restore points (Safari before 16.4) says so plainly instead of a bare "Couldn't open"."""
    out = []
    pg = page('hosted')
    pg.evaluate('async () => { rpMade = false; await makeRestorePoint(); }')
    rid = pg.evaluate('Object.keys(window.__store).find(k => k.startsWith("restorepoints/"))')
    if not rid or 'gz' not in pg.evaluate('k => window.__store[k]', rid): out.append('no packed restore point to try'); close(pg); return out
    pg.evaluate('() => { delete window.DecompressionStream; }')
    pg.evaluate('showRestorePoints()'); pg.wait_for_timeout(500)
    pg.click('#restore-confirm [data-act=rpLoad]'); pg.wait_for_timeout(400)
    t = pg.inner_text('#toast')
    if 'too old to open restore points' not in t: out.append(f'the message was {t!r}')
    if pg.evaluate('pendingRestore !== null'): out.append('a restore was set up anyway')
    out += pg.errors; close(pg)
    return out


def t_stale_browser_copy():
    """Live site opened on a slow connection with an out-of-date copy in the browser: the log shows straight away (Log record
    and Restore points buttons too), but the day's restore point and one-time data fixes wait for the database's own version."""
    import gzip, base64
    out = []
    extra = """
    const __fsx = window.firebase.firestore(), __col = __fsx.collection.bind(__fsx); window.__serverAfter = 12000;
    __fsx.collection = n => { const q = __col(n); if (!['gangs','battles','territories'].includes(n)) return q;
      q.onSnapshot = (...a) => { const fn = a.find(x => typeof x === 'function');
        const docs = () => Object.entries(window.__store).filter(([p]) => p.split('/')[0] === n).map(([p, d]) => ({ id: p.split('/')[1], exists: true, data: () => { const c = JSON.parse(JSON.stringify(d)); if (p === 'battles/b-2026-03-08' && !window.__serverIn) c.post = 'OLD COPY IN THE BROWSER'; return c; } }));
        setTimeout(() => { const all = docs(); fn({ docs: all, metadata: { fromCache: true }, docChanges: () => all.map(doc => ({ type: 'added', doc })) }); }, 20);
        setTimeout(() => { window.__serverIn = true; const all = docs(); fn({ docs: all, metadata: { fromCache: false }, docChanges: () => all.filter(d => d.id === 'b-2026-03-08').map(doc => ({ type: 'modified', doc })) }); }, window.__serverAfter);
        return () => {}; };
      return q; };
    """
    pg = page('hosted', extra=extra)
    if not pg.evaluate('S.loaded'): out.append('the log did not show from the browser copy')
    if not pg.is_visible('#act-btn') or not pg.is_visible('#rp-btn'): out.append('Log record / Restore points buttons waited for the database')
    pg.wait_for_timeout(10000)
    if pg.evaluate("Object.keys(window.__store).some(k => k.startsWith('restorepoints/'))"): out.append('a restore point was made before the database answered')
    if pg.evaluate('onlinePatched'): out.append('one-time data fixes ran on the browser copy')
    pg.wait_for_timeout(11000)
    if not pg.evaluate('onlinePatched'): out.append('one-time data fixes never ran after the database answered')
    rp = pg.evaluate("Object.entries(window.__store).filter(([k]) => k.startsWith('restorepoints/')).map(([k, v]) => v)")
    if not rp: out.append('no restore point after the database answered')
    else:
        r = rp[0]; data = json.loads(gzip.decompress(base64.b64decode(r['gz'])) if r.get('gz') else r['json'])
        if data['battles']['b-2026-03-08'].get('post') != DATA['battles']['b-2026-03-08'].get('post'): out.append(f"the restore point holds {data['battles']['b-2026-03-08'].get('post')!r}")
    out += pg.errors; close(pg)
    return out


def t_random_edits():
    """Two people editing the same battles at once, at random moments and with a slow database: my edits and theirs (to
    different fields) all survive, in the database and on screen, on the live site and in the Claude copy. "They" save the
    way another copy of the log does (a transaction on the live site, a lease in the Claude copy)."""
    import random
    out = []
    MINE, THEIRS = ['post', 'pre'], ['notes', 'treachResult']
    for mode in ('hosted', 'claude'):
        for seed in (10, 11, 12):
            rnd = random.Random(seed); pg = page(mode); pg.evaluate(f'window.__lat = {rnd.choice([200, 400])}')
            bids = pg.evaluate('Object.keys(S.battles).filter(id => !isPC(S.battles[id])).slice(0, 3)')
            want = {}
            for step in range(36):
                b = rnd.choice(bids)
                if rnd.random() < 0.5:
                    f = rnd.choice(MINE); want[(b, f)] = v = f'L{step}'
                    pg.evaluate('([b, f, v]) => { S.battles[b][f] = v; save("battles", b); }', [b, f, v])
                else:
                    f = rnd.choice(THEIRS); want[(b, f)] = v = f'R{step}'
                    pg.evaluate('([b, f, v, how]) => { window.__remoteSave("battles/" + b, d => { d[f] = v; d.updated = new Date().toISOString(); }, how); }', [b, f, v, 'lease' if mode == 'claude' else 'tx'])
                pg.wait_for_timeout(rnd.choice([0, 5, 30, 120, 400, 700]))
            # let both sides finish saving (the test saves far faster than people type, so queues build up)
            for _ in range(60):
                if pg.evaluate('window.__remotePending === 0 && !Object.values(dirty).some(Boolean)'): break
                pg.wait_for_timeout(1000)
            pg.wait_for_timeout(1500)
            lost = []
            for (b, f), v in want.items():
                got_db = pg.evaluate('([b, f]) => window.__store["battles/" + b][f]', [b, f]); got_screen = pg.evaluate('([b, f]) => S.battles[b][f]', [b, f])
                if got_db != v or got_screen != v: lost.append(f'{b}.{f} wanted {v}: database {got_db}, screen {got_screen}')
            if lost: out.append(f'{mode} seed {seed}: {len(lost)} edit(s) lost: {lost[:3]}')
            if pg.inner_text('#save-text') != 'All changes saved': out.append(f'{mode} seed {seed}: status {pg.inner_text("#save-text")!r}')
            out += pg.errors; close(pg)
    # Claude copy where leases aren't offered: saving still works, as a plain write; a brand-new record is written straight away
    pg = page('claude', extra='const __d = __fs.doc.bind(__fs); __fs.doc = p => { const r = __d(p); delete r.acquire; return r; };')
    bid = pg.evaluate('Object.keys(S.battles).find(id => !isPC(S.battles[id]))')
    pg.evaluate('id => { S.battles[id].post = "NO LEASE"; save("battles", id); }', bid); pg.wait_for_timeout(1500)
    if pg.evaluate('id => window.__store["battles/" + id].post', bid) != 'NO LEASE': out.append('without leases, a save did not reach the database')
    out += pg.errors; close(pg)
    # hiding the page right after a save still saves at once (no waiting for this browser's own lease to lapse)
    pg = page('claude')
    bid = pg.evaluate('Object.keys(S.battles).find(id => !isPC(S.battles[id]))')
    pg.evaluate('id => { S.battles[id].post = "FIRST"; save("battles", id); }', bid); pg.wait_for_timeout(900)
    pg.evaluate('id => { S.battles[id].post = "THEN HIDDEN"; save("battles", id); Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true }); Object.defineProperty(document, "hidden", { value: true, configurable: true }); document.dispatchEvent(new Event("visibilitychange")); }', bid)
    pg.wait_for_timeout(500)
    if pg.evaluate('id => window.__store["battles/" + id].post', bid) != 'THEN HIDDEN': out.append('hiding the page right after a save did not save at once')
    out += pg.errors; close(pg)
    pg = page('claude')
    pg.evaluate('() => { S.battles["b-brand-new"] = { id: "b-brand-new", date: "2026-11-27", gangs: ["kyn", "fangs"], events: [] }; save("battles", "b-brand-new"); }'); pg.wait_for_timeout(1500)
    if not pg.evaluate('!!window.__store["battles/b-brand-new"]'): out.append('a new battle was not saved')
    out += pg.errors; close(pg)
    return out


def t_lease_clock():
    """Claude copy on a device whose clock is 10 minutes behind or ahead of the database's: saves to the same battle, one
    after another and with someone else saving it too, each go through within a couple of seconds."""
    out = []
    for label, shift in (('device clock 10 min behind', 600000), ('device clock 10 min ahead', -600000)):
        extra = "const __d = __fs.doc.bind(__fs); __fs.doc = p => { const r = __d(p); const a = r.acquire; r.acquire = async o => { const x = await a(o); if (x.expiresAt) x.expiresAt = new Date(Date.parse(x.expiresAt) + %d).toISOString(); return x; }; return r; };" % shift
        pg = page('claude', extra=extra)
        bid = pg.evaluate('Object.keys(S.battles).find(id => !isPC(S.battles[id]))')
        for i, v in enumerate(('ONE', 'TWO', 'THREE')):
            if i == 2: pg.evaluate('id => { window.__remoteSave("battles/" + id, d => { d.notes = "THEIRS"; }, "lease"); }', bid)
            pg.evaluate('([id, v]) => { S.battles[id].post = v; save("battles", id); }', [bid, v]); pg.wait_for_timeout(2500)
            if pg.evaluate('id => window.__store["battles/" + id].post', bid) != v: out.append(f'{label}: save {v} still waiting after 2.5 s ({pg.inner_text("#save-text")!r})')
        pg.wait_for_timeout(1500)
        if pg.evaluate('id => window.__store["battles/" + id].notes', bid) != 'THEIRS': out.append(f"{label}: someone else's save was lost")
        out += pg.errors; close(pg)
    return out


CONTRAST_JS = r"""() => {
  const parse = c => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p[3] ?? 1]; };
  const lum = ([r, g, b]) => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const bgOf = el => { let layers = []; for (let e = el; e; e = e.parentElement) { const s = getComputedStyle(e); if (s.backgroundImage !== 'none') return null; const c = parse(s.backgroundColor); if (c && c[3] > 0) { layers.push(c); if (c[3] >= 1) break; } }
    let out = [255, 255, 255]; for (const c of layers.reverse()) out = out.map((v, i) => v * (1 - c[3]) + c[i] * c[3]); return out; };
  const res = {};
  for (const el of document.querySelectorAll('#view *, header *, nav *')) {
    if (!el.offsetParent) continue;
    const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()); if (!own) continue;
    const s = getComputedStyle(el); const fg = parse(s.color); const bg = bgOf(el); if (!fg || !bg) continue;
    const f = fg.slice(0, 3).map((v, i) => v * fg[3] + bg[i] * (1 - fg[3]));
    const L1 = lum(f), L2 = lum(bg), ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
    const size = parseFloat(s.fontSize), bold = parseInt(s.fontWeight) >= 700, large = size >= 24 || (bold && size >= 18.66);
    const need = large ? 3 : 4.5;
    if (ratio < need) { const k = (el.className && typeof el.className === 'string' ? el.tagName.toLowerCase() + '.' + el.className.split(' ')[0] : el.tagName.toLowerCase()) + ' ' + s.color + ' on rgb(' + bg.map(Math.round).join(',') + ')';
      res[k] ||= { ratio: Math.round(ratio * 100) / 100, need, n: 0, eg: el.textContent.trim().slice(0, 40), size }; res[k].n++; }
  }
  return res;
}"""


def t_contrast():
    """Every piece of visible text on every tab, a battle and a post-cycle meets the usual contrast standard against its
    background (4.5 to 1, or 3 to 1 for large text), in the light and the dark theme."""
    out = []
    for scheme in ('light', 'dark'):
        pg = page('claude'); pg.emulate_media(color_scheme=scheme); pg.evaluate('render()')
        views = pg.evaluate("() => { const v = []; for (const t of ['battles','gangs','npcs','territories','equipment','rules']) v.push([t, null]); for (const id of Object.keys(S.battles)) v.push(['battles', id]); return v; }")
        seen = {}
        for t, sel in views:
            pg.evaluate('([t, s]) => { S.tab = t; S.sel = s; render(); }', [t, sel]); pg.wait_for_timeout(60)
            for k, v in pg.evaluate(CONTRAST_JS).items(): seen.setdefault(k, dict(v, where=f'{t}/{sel}'))
        for k, v in sorted(seen.items(), key=lambda kv: kv[1]['ratio']):
            out.append(f"{scheme}: {k}: contrast {v['ratio']} (needs {v['need']}), e.g. {v['eg']!r} on {v['where']}")
        out += pg.errors; close(pg)
    return out


TESTS = {'syntax': t_syntax, 'equipment': t_equipment, 'every-view': t_every_view, 'injection': t_injection,
         'typing-during-save': t_typing_during_save, 'two-editors': t_two_editors, 'critical-injury': t_critical_injury,
         'map-setup': t_map_setup, 'missing-gang': t_missing_gang, 'log-record-purchases': t_log_record_purchases, 'laptop-build': t_laptop_build,
         'change-back': t_change_back, 'restore-point': t_restore_point, 'munda-same-day': t_munda_same_day,
         'failed-save': t_failed_save, 'report-text': t_report_text, 'every-button': t_every_button, 'every-field': t_every_field,
         'reconnect': t_reconnect, 'save-error-codes': t_save_error_codes, 'redraw-speed': t_redraw_speed,
         'status-while-saving': t_status_while_saving,
         'odd-records': t_odd_records, 'activity-owner': t_activity_owner,
         'messages': t_messages, 'panels-keyboard': t_panels_keyboard,
         'print-feedback': t_print_feedback, 'message-size': t_message_size, 'sign-out': t_sign_out,
         'claude-no-db': t_claude_no_db, 'no-access': t_no_access, 'tab-keys': t_tab_keys,
         'firestore-restart': t_firestore_restart, 'old-safari-restore': t_old_safari_restore,
         'stale-browser-copy': t_stale_browser_copy, 'random-edits': t_random_edits,
         'lease-clock': t_lease_clock, 'contrast': t_contrast}

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
