/* A stand-in for Firebase (and for the Claude copy's database) used by the tests: an in-memory store with the same calls the log
   makes (collection, doc, onSnapshot, get, set, delete, runTransaction), a set delay per call (window.__lat, ms), and
   window.__remoteWrite(path, data) to play "someone else" saving. window.__echoFirst: the database echoes a save before it returns. */
(() => {
  const store = window.__store = {}; // path -> data
  const listeners = {}; // coll -> [fn]
  const started = {}; // coll -> how many listeners have been started on it
  const LAT = () => window.__lat ?? 30;
  const clone = v => v == null ? v : JSON.parse(JSON.stringify(v));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const emit = (coll, changes) => { const all = Object.entries(store).filter(([p]) => p.split('/')[0] === coll).map(([p, d]) => mkdoc(p.split('/')[1], d)); (listeners[coll] || []).forEach(fn => fn({ docChanges: () => changes, docs: all })); };
  const mkdoc = (id, data) => ({ id, exists: data != null, data: () => clone(data) });
  window.__writes = [];
  /* window.__endListeners(coll, code): end every live listener on a collection, as the database does when its
     connection stops answering ('unavailable') or access is withdrawn ('revoked'). */
  window.__listeners = {};
  window.__endListeners = (coll, code) => { for (const L of window.__listeners[coll] || []) if (!L.dead) { L.dead = true; L.err && L.err({ code, message: 'ended by test' }); } };
  const versions = {}; // path -> how many times it has been written
  const leases = {};   // path -> { holder, until }
  const write = (path, data) => {
    versions[path] = (versions[path] || 0) + 1;
    const [coll, id] = path.split('/'); const had = path in store;
    if (data == null) delete store[path]; else store[path] = clone(data);
    window.__writes.push([path, data == null ? null : clone(data)]);
    setTimeout(() => emit(coll, [{ type: data == null ? 'removed' : had ? 'modified' : 'added', doc: mkdoc(id, data) }]), 5);
  };
  window.__remoteWrite = write;
  /* "Someone else" saving the way another copy of the log does: change(data) edits the latest version of the record.
     Claude copy: take the lease, read, change, write. Live site: the same inside a transaction. Like the log, one save at
     a time per record. */
  const remoteChains = {}, otherLease = {};
  window.__remotePending = 0;
  window.__remoteSave = (path, change, how) => { window.__remotePending++;
    return (remoteChains[path] = (remoteChains[path] || Promise.resolve()).then(() => remoteSaveNow(path, change, how)).finally(() => { window.__remotePending--; })); };
  const remoteSaveNow = async (path, change, how) => {
    if (how === 'lease') { const ref = docRef(path), t0 = Date.now();
      if ((otherLease[path] || 0) > Date.now()) await sleep(otherLease[path] - Date.now() + 250 + Math.random() * 100);
      for (;;) { const r = await ref.acquire({ holder: 'other-player', ttlMs: 1000 }); if (r.acquired) { otherLease[path] = Date.parse(r.expiresAt); break; }
        if (Date.now() - t0 > 5000) break; await sleep(Math.max(50, Date.parse(r.expiresAt) - Date.now()) + Math.random() * 150); }
      await sleep(LAT()); const d = clone(store[path]); change(d); await sleep(LAT()); write(path, d); return; }
    await fs.runTransaction(async t => { const r = docRef(path); const snap = await t.get(r); const d = snap.data(); change(d); t.set(r, d); });
  };
  const docRef = path => ({
    get: async () => { await sleep(LAT()); return mkdoc(path.split('/')[1], store[path]); },
    set: async d => { await sleep(LAT()); write(path, d); if (window.__echoFirst) await sleep(60); },
    delete: async () => { await sleep(LAT()); write(path, null); },
    /* The Claude copy's database: a short lease on the record for one writer (set-if-not-busy). */
    acquire: async ({ holder, ttlMs }) => { await sleep(LAT()); const now = Date.now(), l = leases[path];
      if (l && l.until > now && l.holder !== holder) return { acquired: false, expiresAt: new Date(l.until).toISOString() };
      const ms = Math.min(600000, Math.max(1000, ttlMs || 30000)); leases[path] = { holder, until: now + ms };
      return { acquired: true, expiresAt: new Date(now + ms).toISOString(), holder }; },
    _path: path, path, id: path.split("/").pop()
  });
  const query = (coll, filters = [], order = null, lim = 1e9) => ({
    where: (f, op, v) => query(coll, [...filters, [f, op, v]], order, lim),
    orderBy: (f, dir) => query(coll, filters, [f, dir], lim),
    limit: n => query(coll, filters, order, n),
    get: async () => { await sleep(LAT());
      let docs = Object.entries(store).filter(([p]) => p.split('/')[0] === coll).map(([p, d]) => mkdoc(p.split('/')[1], d));
      for (const [f, op, v] of filters) docs = docs.filter(d => op === '<' ? d.data()[f] < v : op === '>' ? d.data()[f] > v : d.data()[f] === v);
      if (order) docs.sort((a, b) => (a.data()[order[0]] > b.data()[order[0]] ? 1 : -1) * (order[1] === 'desc' ? -1 : 1));
      { const ds = docs.slice(0, lim); return { docs: ds, empty: !ds.length, size: ds.length }; } },
    onSnapshot: (...args) => { const opts = typeof args[0] === 'function' ? {} : args.shift() || {}; const [fn, err] = args;
      const L = { fn: s => { if (!L.dead) fn(s); }, err }; (listeners[coll] ||= []).push(L.fn); (window.__listeners[coll] ||= []).push(L);
      const nth = started[coll] = (started[coll] || 0) + 1;
      setTimeout(() => { const all = Object.entries(store).filter(([p]) => p.split('/')[0] === coll).map(([p, d]) => mkdoc(p.split('/')[1], d));
        /* window.__firestoreCache: behave like Firebase keeping its copy in the browser. A listener started again hands back
           the browser's copy first (fromCache), and when the server finds nothing changed it only says so if the listener
           asked for includeMetadataChanges (as Firestore's event_manager.ts shouldRaiseEvent does). */
        if (window.__firestoreCache && nth > 1) {
          L.fn({ docs: all, metadata: { fromCache: true }, docChanges: () => [] });
          if (opts.includeMetadataChanges) setTimeout(() => L.fn({ docs: all, metadata: { fromCache: false }, docChanges: () => [] }), 300);
          return; }
        /* window.__pagedNext[coll]: this listener delivers in two pages, as the Claude database may: a partial first page
           flagged fromCache, then the rest. */
        if ((window.__pagedNext || {})[coll]) { delete window.__pagedNext[coll]; const half = all.slice(0, Math.ceil(all.length / 3)), rest = all.slice(half.length);
          L.fn({ docs: half, metadata: { fromCache: true }, docChanges: () => half.map(doc => ({ type: 'added', doc })) });
          setTimeout(() => L.fn({ docs: all, metadata: { fromCache: false }, docChanges: () => rest.map(doc => ({ type: 'added', doc })) }), 500);
          return; }
        L.fn({ docs: all, docChanges: () => all.map(doc => ({ type: 'added', doc })) }); }, 20);
      return () => { L.dead = true; }; }
  });
  const fs = {
    enablePersistence: async () => {},
    /* Signing out: record what was called (kept across the reload that follows). */
    terminate: async () => { const c = JSON.parse(sessionStorage.getItem('__fbcalls') || '[]'); c.push('terminate'); sessionStorage.setItem('__fbcalls', JSON.stringify(c)); },
    clearPersistence: async () => { const c = JSON.parse(sessionStorage.getItem('__fbcalls') || '[]'); c.push('clearPersistence'); sessionStorage.setItem('__fbcalls', JSON.stringify(c)); },
    collection: c => query(c),
    doc: p => docRef(p),
    /* Like Firebase's: if a record read in the transaction was written by someone else before it commits, run it again. */
    runTransaction: async fn => { for (let tries = 0; tries < 5; tries++) {
        const seen = {}; const t = { get: async r => { await sleep(LAT()); seen[r._path] = versions[r._path] || 0; return mkdoc(r._path.split('/')[1], store[r._path]); }, sets: [], set(r, d) { this.sets.push([r._path, d]); } };
        await fn(t); await sleep(LAT());
        if (Object.entries(seen).some(([p, v]) => (versions[p] || 0) !== v)) continue;
        for (const [p, d] of t.sets) write(p, d); return; }
      throw Object.assign(new Error('too much contention'), { code: 'aborted' }); }
  };
  const user = { displayName: 'Tester', email: 't@example.com' };
  const auth = { currentUser: user, onAuthStateChanged: cb => setTimeout(() => cb(user), 0), signInWithPopup: async () => {}, signOut: async () => { const c = JSON.parse(sessionStorage.getItem('__fbcalls') || '[]'); c.push('signOut'); sessionStorage.setItem('__fbcalls', JSON.stringify(c)); } };
  const authFn = () => auth; authFn.GoogleAuthProvider = function () { this.setCustomParameters = () => {}; };
  window.firebase = { initializeApp: () => {}, auth: authFn, firestore: () => fs };
})();
